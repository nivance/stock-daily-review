# -*- coding: utf-8 -*-
"""A股每日复盘 · 数据采集层

设计要点
  1. 默认绕过系统代理（本机/企业常见的 127.0.0.1:xxxx 本地代理会污染 requests；
     如需走代理设 ASHARE_REVIEW_TRUST_ENV=1）
  2. 全局限速（默认 1.6s/请求），避免把 IP 打进东财黑名单
  3. 请求失败自动重试 + 指数退避
  4. 多源降级：东财 push2 主域被封时自动切 push2test / push2delay；
     核心字段再兜底腾讯/新浪
  5. 输出结构化快照 <数据根>/data/history/<date>.json，供渲染层与历史均值计算复用

数据根（data/ 与 out/ 的存放处）
  $ASHARE_REVIEW_HOME  >  默认 ~/.ashare-review   （见 paths.py）

用法（与 cwd 无关，可用任意已装依赖的 Python + 绝对路径调用）
  python fetch_report_data.py --show-paths       # 运行环境自检（路径/依赖/Python），首次先跑这个
  python fetch_report_data.py                    # 自动取最近交易日
  python fetch_report_data.py --date 2026-09-30  # 指定交易日
  python fetch_report_data.py --no-news          # 跳过新闻（省时）
  python fetch_report_data.py --check-today      # 仅判断今天是否交易日 YES/NO
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

# 让 scripts/ 目录可 import（无论从哪个 cwd 启动、用哪个解释器）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from paths import (DATA_ROOT, CFG_PATH, HIST_DIR, HOLD_PATH,  # noqa: E402
                   ensure_deps, ensure_dirs)

# 依赖延迟校验：--show-paths / --doctor 属"环境自检"，**不依赖第三方包**也要能跑，
# 因此这里不做 import 期强校验，改在真正干活前（__main__ 里）调用 ensure_deps()。
try:
    import requests  # noqa: E402
except ImportError:  # 缺依赖时先降级为 None，让 --show-paths 仍可用
    requests = None

# ---------- 环境修补：默认绕过系统代理 ----------
# 本机与企业网络常配 127.0.0.1:xxxx 本地代理，会污染 requests 导致行情接口全挂，
# 故默认关闭 trust_env。如需走系统代理，设 ASHARE_REVIEW_TRUST_ENV=1。
if requests is not None and (os.environ.get("ASHARE_REVIEW_TRUST_ENV") or "").strip().lower() not in (
        "1", "true", "yes", "on"):
    _orig_sess_init = requests.Session.__init__

    def _patched_sess_init(self, *a, **k):
        _orig_sess_init(self, *a, **k)
        self.trust_env = False

    requests.Session.__init__ = _patched_sess_init

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HDR_EM = {"User-Agent": UA, "Accept": "*/*",
          "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
          "Referer": "https://quote.eastmoney.com/"}
HDR_TX = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}

CFG = json.load(open(CFG_PATH, encoding="utf-8"))
S = CFG["settings"]
EM_HOSTS = S["em_hosts"]
EM_HIS_HOSTS = S["em_his_hosts"]

_last_call = [0.0]
WARN = []
FORCE_AMOUNT = False


def _throttle():
    gap = time.time() - _last_call[0]
    if gap < S["min_interval_sec"]:
        time.sleep(S["min_interval_sec"] - gap)
    _last_call[0] = time.time()


def http_get(url, headers=None, tries=None, timeout=None, quiet=False):
    """带限速 + 重试 + 指数退避的 GET"""
    tries = tries or S["retries"]
    timeout = timeout or S["timeout_sec"]
    last_err = None
    for i in range(tries):
        _throttle()
        try:
            r = requests.get(url, headers=headers or HDR_EM, timeout=timeout)
            if r.status_code == 200 and r.text.strip():
                return r
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last_err = e
        if i < tries - 1:
            if not quiet:
                print(f"    [retry {i + 1}] {type(last_err).__name__}: {str(last_err)[:60]}")
            time.sleep(S["retry_wait_sec"] * (i + 1))
    raise RuntimeError(str(last_err))


def em_json(path, hosts=None, **kw):
    """按主机池依次尝试，返回解析后的 JSON"""
    hosts = hosts or EM_HOSTS
    errs = []
    for h in hosts:
        try:
            r = http_get(f"https://{h}{path}", **kw)
            return r.json()
        except Exception as e:  # noqa: BLE001
            errs.append(f"{h}:{type(e).__name__}")
    raise RuntimeError("所有主机均失败 -> " + ", ".join(errs))


def em_get(path, hosts=None, **kw):
    hosts = hosts or EM_HOSTS
    errs = []
    for h in hosts:
        try:
            r = http_get(f"https://{h}{path}", **kw)
            return r.json()
        except Exception as e:  # noqa: BLE001
            errs.append(f"{h}:{type(e).__name__}")
    raise RuntimeError("所有主机均失败 -> " + ", ".join(errs))


def note(msg):
    print(f"  ! {msg}")
    WARN.append(msg)


# ============================ 交易日 ============================
def latest_trade_date():
    r = http_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,5,",
                 headers=HDR_TX)
    j = r.json()["data"]["sh000001"]
    arr = j.get("day") or j.get("qfqday")
    return arr[-1][0].replace("-", "")


# ============================ 指数 ============================
def fetch_indices():
    secids = ",".join(x["secid"] for x in CFG["indices"])
    out = {}
    try:
        j = em_get(f"/api/qt/ulist.np/get?fltt=2&secids={secids}"
                   "&fields=f2,f3,f4,f6,f12,f14,f15,f16,f17,f18")
        for d in j["data"]["diff"]:
            out[str(d["f12"])] = d
        rows = []
        for it in CFG["indices"]:
            code = it["secid"].split(".")[1]
            d = out.get(code)
            if not d:
                continue
            rows.append({
                "name": d.get("f14") or it["name"],
                "close": d.get("f2"), "pct": d.get("f3"), "chg": d.get("f4"),
                "amount": d.get("f6"),  # 元
                "high": d.get("f15"), "low": d.get("f16"),
                "open": d.get("f17"), "prev_close": d.get("f18"),
                "source": "eastmoney",
            })
        if rows:
            return rows
    except Exception as e:  # noqa: BLE001
        note(f"指数(东财)失败 {type(e).__name__}，降级腾讯")
    return fetch_indices_tx()


def fetch_indices_tx():
    codes = ",".join(x["code"] for x in CFG["indices"])
    r = http_get(f"https://qt.gtimg.cn/q={codes}", headers=HDR_TX)
    try:
        txt = r.content.decode("gbk")
    except Exception:  # noqa: BLE001
        txt = r.text
    rows = []
    for line in txt.strip().splitlines():
        if "=" not in line:
            continue
        body = line.split("=", 1)[1].strip().strip('";')
        f = body.split("~")
        if len(f) < 40:
            continue
        def num(i):
            try:
                return float(f[i])
            except Exception:  # noqa: BLE001
                return None
        rows.append({
            "name": f[1], "close": num(3), "prev_close": num(4), "open": num(5),
            "chg": None if num(3) is None else round(num(3) - (num(4) or 0), 2),
            "pct": num(32), "high": num(33), "low": num(34),
            "amount": None if num(37) is None else num(37) * 10000,  # 万元 -> 元
            "source": "tencent",
        })
    return rows


# ============================ 涨跌分布 / 家数 ============================
BUCKET_LABEL = {
    "11": "涨停", "10": "9%~10%", "9": "8%~9%", "8": "7%~8%", "7": "6%~7%",
    "6": "5%~6%", "5": "4%~5%", "4": "3%~4%", "3": "2%~3%", "2": "1%~2%",
    "1": "0%~1%", "0": "平盘",
    "-1": "-1%~0", "-2": "-2%~-1%", "-3": "-3%~-2%", "-4": "-4%~-3%",
    "-5": "-5%~-4%", "-6": "-6%~-5%", "-7": "-7%~-6%", "-8": "-8%~-7%",
    "-9": "-9%~-8%", "-10": "-10%~-9%", "-11": "跌停",
}


def _limit_pct(code, name):
    """该股当日涨跌幅限制（百分点）。主板 10%，创业/科创 20%，北交所 30%

    注：主板 ST 股此前为 5%，实测 2026-09-30 有 *ST 股涨停于 9.98%，
    说明已与普通股一致（10%），故不再单列，避免把 ST 误判成涨停。
    """
    c = str(code)
    num = c[2:] if c[:2].isalpha() else c
    if c.startswith("bj") or num.startswith(("43", "83", "87", "88", "92")):
        return 30.0
    if num.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def _bucket_key(pct, is_zt, is_dt):
    """东财同款 23 档区间桶"""
    if is_zt:
        return "11"
    if is_dt:
        return "-11"
    if pct == 0:
        return "0"
    if pct > 0:
        n = int(pct)
        return "10" if n >= 9 else str(n + 1)
    n = int(-pct)
    return f"-{min(n + 1, 10)}"


def _fetch_breadth_em(date):
    """降级方案：东财 ZDFenBu（一次请求，但股票池口径不透明）"""
    ut = "7eea3edcaed734bea9cbfc24409ed989"
    try:
        j = http_get(f"https://push2ex.eastmoney.com/getTopicZDFenBu?ut={ut}"
                     f"&dpt=wz.ztzt&date={date}")
        data = j.json()["data"]
    except Exception as e:  # noqa: BLE001
        note(f"涨跌分布失败 {type(e).__name__}")
        return {"up": None, "down": None, "flat": None, "distribution": []}

    raw = {}
    for item in data["fenbu"]:
        for k, v in item.items():
            raw[str(k)] = v

    up = sum(v for k, v in raw.items() if not k.startswith("-") and k != "0")
    down = sum(v for k, v in raw.items() if k.startswith("-"))
    flat = raw.get("0", 0)
    order = ["11", "10", "9", "8", "7", "6", "5", "4", "3", "2", "1", "0",
             "-1", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9", "-10", "-11"]
    dist = [{"bucket": k, "label": BUCKET_LABEL.get(k, k), "count": raw.get(k, 0),
             "dir": "涨" if not k.startswith("-") and k != "0" else ("跌" if k.startswith("-") else "平")}
            for k in order if k in raw]
    return {
        "scope": "东财口径（股票池不透明）",
        "up": up if up else None, "down": down if down else None,
        "flat": flat, "limit_up": raw.get("11"), "limit_down": raw.get("-11"),
        "ratio": round(up / down, 2) if down else None,
        "distribution": dist, "source": "eastmoney:ZDFenBu",
    }


def fetch_breadth(date, pools=None):
    """涨跌家数 + 涨跌幅区间分布（逐股统计，口径透明可拆分市场）

    主口径 = **全市场（沪深京，含北交所）**，涨跌停按收盘封板价自算、**含 ST**（不剔除）。
    同时保留沪深两市口径、涨停池口径（沪深、剔 ST）与东财 ZDFenBu / 乐咕交叉校验值，
    便于与各类行情软件对账。
    """
    out = {"scope": "全市场（沪深京，含北交所）", "up": None, "down": None, "flat": None,
           "distribution": [], "source": "sina:stock_zh_a_spot"}
    try:
        import akshare as ak
        import pandas as pd

        df = ak.stock_zh_a_spot()
        df = df.rename(columns={"代码": "code", "名称": "name", "最新价": "price",
                                "昨收": "prev", "涨跌幅": "pct", "成交量": "vol",
                                "成交额": "amt", "最高": "high"})
        for c in ["price", "prev", "pct", "vol", "amt"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["code"] = df["code"].astype(str)
        # 剔除停牌（无价 / 无成交）
        live = df[(df["price"] > 0) & (df["vol"] > 0) & df["pct"].notna()].copy()
        live["mkt"] = live["code"].str[:2].map({"sh": "沪", "sz": "深", "bj": "京"}).fillna("其他")
        live["lim"] = [_limit_pct(c, n) for c, n in zip(live["code"], live["name"])]
        live["zt_px"] = (live["prev"] * (1 + live["lim"] / 100)).round(2)
        live["dt_px"] = (live["prev"] * (1 - live["lim"] / 100)).round(2)
        # 封板判定：收盘价贴上/跌停价；再加涨跌幅上界约束，排除上市首日等无涨跌幅限制个股
        live["is_zt"] = (live["price"] >= live["zt_px"] - 0.005) & (live["pct"] <= live["lim"] + 0.6)
        live["is_dt"] = (live["price"] <= live["dt_px"] + 0.005) & (live["pct"] >= -(live["lim"] + 0.6))
        # 涨停池口径（东财/同花顺池子：仅沪深、剔 ST）—— 仅作对照，不进主口径
        live["sym"] = live["code"].str[2:]
        zt_codes = {str(r.get("code")) for r in (pools or {}).get("zt", []) if str(r.get("code", "")).isdigit()}
        dt_codes = {str(r.get("code")) for r in (pools or {}).get("dt", []) if str(r.get("code", "")).isdigit()}
        live["is_zt_pool"] = live["sym"].isin(zt_codes) if zt_codes else live["is_zt"]
        live["is_dt_pool"] = live["sym"].isin(dt_codes) if dt_codes else live["is_dt"]
        # 主口径分桶：按自算封板判定（含北交所、含 ST）
        live["bucket"] = ["11" if z else ("-11" if d else _bucket_key(p, False, False))
                          for p, z, d in zip(live["pct"], live["is_zt"], live["is_dt"])]

        def agg(d):
            up = int((d["pct"] > 0).sum())
            dn = int((d["pct"] < 0).sum())
            return {"total": int(len(d)), "up": up, "down": dn,
                    "flat": int((d["pct"] == 0).sum()),
                    "limit_up": int(d["is_zt_pool"].sum()),
                    "limit_down": int(d["is_dt_pool"].sum()),
                    "limit_up_incl_st": int(d["is_zt"].sum()),
                    "limit_down_incl_st": int(d["is_dt"].sum()),
                    "ratio": round(up / dn, 2) if dn else None,
                    "amount": float(d["amt"].sum())}

        out["by_market"] = {m: agg(live[live["mkt"] == m]) for m in ["沪", "深", "京"]}
        hs = live[live["mkt"].isin(["沪", "深"])]
        out["scopes"] = {"沪深": agg(hs), "沪深京": agg(live)}
        allm = out["scopes"]["沪深京"]
        # 主口径：全市场（沪深京，含北交所）
        out.update({k: allm[k] for k in ["up", "down", "flat", "ratio"]})
        out["total"] = allm["total"]
        # 涨跌停各市场均计入（北交所 30% 涨跌幅按同规则判定）
        out["limit_up"] = allm["limit_up_incl_st"]
        out["limit_down"] = allm["limit_down_incl_st"]
        # 对照口径（供对账，不进报告正文）
        out["pool_scope"] = {"limit_up": allm["limit_up"], "limit_down": allm["limit_down"]}
        out["hs_only"] = {k: out["scopes"]["沪深"][k] for k in
                          ["up", "down", "flat", "limit_up_incl_st", "limit_down_incl_st", "ratio"]}
        out["by_market_zt"] = {m: {"limit_up": x["limit_up_incl_st"], "limit_down": x["limit_down_incl_st"]}
                               for m, x in out["by_market"].items()}
        cnt = live["bucket"].value_counts().to_dict()
        order = ["11", "10", "9", "8", "7", "6", "5", "4", "3", "2", "1", "0",
                 "-1", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9", "-10", "-11"]
        out["distribution"] = [
            {"bucket": k, "label": BUCKET_LABEL.get(k, k), "count": int(cnt.get(k, 0)),
             "dir": "涨" if not k.startswith("-") and k != "0" else ("跌" if k.startswith("-") else "平")}
            for k in order]
    except Exception as e:  # noqa: BLE001
        note(f"逐股统计失败 {type(e).__name__}: {str(e)[:60]}，降级东财 ZDFenBu")
        return _fetch_breadth_em(date)

    # 交叉校验（不进报告，仅存快照便于对账）
    cc = {}
    try:
        em = _fetch_breadth_em(date)
        cc["em_zdfenbu"] = {k: em.get(k) for k in ["up", "down", "flat", "limit_up", "limit_down"]}
    except Exception:  # noqa: BLE001
        pass
    try:
        import akshare as ak2
        df = ak2.stock_market_activity_legu()
        kv = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1].astype(str)))
        cc["legu"] = {k: kv.get(k) for k in ["上涨", "下跌", "平盘", "涨停", "跌停", "统计日期"]}
    except Exception as e:  # noqa: BLE001
        note(f"legulegu 校验跳过 {type(e).__name__}")
    out["cross_check"] = cc
    return out


# ============================ 成交额与历史均值 ============================
AMOUNT_FLOOR = S.get("amount_min_yi", 1000) * 1e8
AMOUNT_CACHE = os.path.join(DATA_ROOT, S.get("amount_cache", "data/cache/index_amount_series.json"))
# 北交所成交额（东财北证50 指数 0.899050），用于把成交额口径扩到全市场
BJ_SECID = S.get("bj_secid", "0.899050")
BJ_FLOOR = S.get("bj_amount_min_yi", 20) * 1e8
BJ_CACHE = os.path.join(DATA_ROOT, S.get("bj_amount_cache", "data/cache/bj_amount_series.json"))
BJ_HOSTS = S.get("bj_kline_hosts", ["push2his.eastmoney.com", "push2test.eastmoney.com"])
HDR_THS = {"User-Agent": UA, "Referer": "http://stockpage.10jqka.com.cn/"}
THS_SH = "hs_1A0001"   # 同花顺：上证指数
THS_SZ = "hs_399001"   # 同花顺：深证成指


def _ths_year(code, year):
    """同花顺年度日线。字段：日期,开,高,低,收,成交量,成交额,换手,..."""
    r = http_get(f"http://d.10jqka.com.cn/v6/line/{code}/01/{year}.js",
                 headers=HDR_THS, tries=3)
    t = r.text
    body = t[t.index("(") + 1: t.rindex(")")]
    j = json.loads(body)
    out = {}
    for row in (j.get("data") or "").split(";"):
        p = row.split(",")
        if len(p) >= 7:
            try:
                out[f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]}"] = float(p[6])
            except Exception:  # noqa: BLE001
                pass
    return out


def _kline_amount_em(secid, lmt=300):
    """东财 K 线成交额（push2his 会被 IP 级限流，仅作兜底）"""
    j = em_get(f"/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2&fields2=f51,f53,f57"
               f"&klt=101&fqt=1&end=20500101&lmt={lmt}",
               hosts=EM_HIS_HOSTS, tries=2, timeout=20)
    d = j.get("data") or {}
    series = {}
    for line in d.get("klines", []):
        p = line.split(",")
        if len(p) >= 3:
            try:
                series[p[0]] = float(p[2])
            except Exception:  # noqa: BLE001
                pass
    return series


def _load_amount_cache():
    if os.path.exists(AMOUNT_CACHE):
        try:
            return json.load(open(AMOUNT_CACHE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_amount_cache(d):
    os.makedirs(os.path.dirname(AMOUNT_CACHE), exist_ok=True)
    json.dump(d, open(AMOUNT_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _bump_today(cache, today_date, today_hs):
    """把「当日」沪深成交额锁定为实时口径。

    同花顺年度文件的**当日行可能尚未结算完整**（实测 2026-10-08：年度文件 8,301 亿，
    真实 16,822 亿，仅为 49%），而 refresh_amount_cache 每次都会用同花顺的值覆盖缓存，
    所以必须在合并之后再纠正一次，否则残缺值会被反复写回、污染后续 MA5/MA10/MA20。

    返回 (cache, changed)：偏差在容差内（或当日缺失）则原样返回。
    """
    if not (today_date and today_hs):
        return cache, False
    old = cache.get(today_date)
    tol = max(5e9, today_hs * 0.05)          # 50 亿 或 5%
    if old is not None and old >= AMOUNT_FLOOR and abs(old - today_hs) <= tol:
        return cache, False                  # 已结算完整，保留同花顺口径
    cache = dict(cache)
    cache[today_date] = today_hs
    return cache, True


def _protect_recent(merged, cache, days=3):
    """近 N 日的已存值不允许被同花顺临时行覆盖。

    同花顺年度文件对**最近几天**会给未结算的临时值（实测 2026-10-08 深证成指只有 189 亿），
    而 refresh_amount_cache 是无条件 `merged[d] = sh[d] + sz[d]`。若不设防，今天用实时口径
    校准好的当日值，会在明天的刷新里被同花顺的残缺行重新灌回去，从而污染 MA5/MA10。
    """
    today = dt.date.today()
    for d in list(merged):
        old = cache.get(d)
        if not old:
            continue
        try:
            age = (today - dt.date.fromisoformat(d)).days
        except Exception:  # noqa: BLE001
            continue
        if age <= days and abs(old - merged[d]) > max(5e9, old * 0.05):
            merged[d] = old
    return merged


def refresh_amount_cache(force=False, today_date=None, today_hs=None):
    """刷新两市成交额历史。优先同花顺（非东财、不易被封），东财兜底；脏数据丢弃后落本地缓存

    today_date/today_hs：当日日期 + 实时沪深成交额，用于纠正年度文件未结算完整的当日行。
    """
    cache = {} if force else _load_amount_cache()
    if cache and not force:
        newest = max(cache)
        age = (dt.date.today() - dt.date.fromisoformat(newest)).days
        if len(cache) >= 200 and age <= 7:
            cache, ch = _bump_today(cache, today_date, today_hs)
            if ch:
                note(f"当日（{today_date}）沪深成交额按实时口径 {today_hs / 1e8:,.0f} 亿修正"
                     "（年度文件当日行未结算完整），已回写缓存")
                _save_amount_cache(cache)
            return cache

    merged = dict(cache)
    src = None

    # --- 1) 同花顺 ---
    try:
        y = dt.date.today().year
        sh, sz = {}, {}
        for yy in (y, y - 1):
            sh.update(_ths_year(THS_SH, yy))
            sz.update(_ths_year(THS_SZ, yy))
        pairs = set(sh) & set(sz)
        if len(pairs) >= 20:
            for d in pairs:
                merged[d] = sh[d] + sz[d]
            src = f"ths({len(pairs)}天)"
    except Exception as e:  # noqa: BLE001
        note(f"同花顺成交额失败 {type(e).__name__}")

    # --- 2) 东财兜底 ---
    if src is None:
        try:
            sh = _kline_amount_em("1.000001")
            sz = _kline_amount_em("0.399001")
            pairs = set(sh) & set(sz)
            if len(pairs) >= 20:
                for d in pairs:
                    merged[d] = sh[d] + sz[d]
                src = f"eastmoney({len(pairs)}天)"
        except Exception as e:  # noqa: BLE001
            note(f"东财成交额兜底失败 {type(e).__name__}")

    if src is None:
        note(f"成交额历史刷新失败，沿用本地缓存 {len(cache)} 天")
        return cache

    bad = [d for d, v in merged.items() if v < AMOUNT_FLOOR]
    for d in bad:
        merged.pop(d, None)
    if bad:
        note(f"成交额序列丢弃 {len(bad)} 个异常值 (<{AMOUNT_FLOOR / 1e8:.0f}亿)")
    merged = _protect_recent(merged, cache)
    merged, ch = _bump_today(merged, today_date, today_hs)
    if ch:
        note(f"当日（{today_date}）沪深成交额按实时口径 {today_hs / 1e8:,.0f} 亿修正"
             "（年度文件当日行未结算完整），已回写缓存")
    _save_amount_cache(merged)
    print(f"    成交额历史来源 {src}，合计 {len(merged)} 天")
    return merged


def _load_json(p):
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_json(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def refresh_bj_amount_cache(force=False):
    """北交所成交额历史（东财北证50 指数 0.899050 的日成交额）

    路由 push2his（主）→ 编号节点 → push2test（测试域，仅兜底）。
    两道清洗：① 低于下限（默认 20 亿）丢弃；② 同一数值重复 >5 次判定为
    "占位脏数据"（push2test 会把成段历史填成同一常数，实测 6~9 月全被填成 16.2 亿）整体丢弃。
    """
    cache = {} if force else _load_json(BJ_CACHE)
    try:
        j = em_get(f"/api/qt/stock/kline/get?secid={BJ_SECID}&fields1=f1,f2&fields2=f51,f53,f57"
                   f"&klt=101&fqt=1&end=20500101&lmt=400", hosts=BJ_HOSTS, tries=2, timeout=20)
        kl = (j.get("data") or {}).get("klines") or []
    except Exception as e:  # noqa: BLE001
        note(f"北交所成交额刷新失败 {type(e).__name__}（沿用缓存 {len(cache)} 天）")
        return cache

    got, bad = {}, 0
    for line in kl:
        p = line.split(",")
        if len(p) >= 3:
            try:
                v = float(p[2])
            except Exception:  # noqa: BLE001
                continue
            if v >= BJ_FLOOR:
                got[p[0]] = v
            else:
                bad += 1
    # 占位值检测（同值重复出现 = 测试域填充数据）
    from collections import Counter
    rep = {k for k, c in Counter(round(v / 1e6) for v in got.values()).items() if c > 5}
    if rep:
        drop = [d for d, v in got.items() if round(v / 1e6) in rep]
        for d in drop:
            got.pop(d, None)
        note(f"北交所成交额剔除 {len(drop)} 个重复占位值，保留 {len(got)} 天")
    if not got:
        return cache
    merged = dict(cache)
    merged.update(got)
    _save_json(BJ_CACHE, merged)
    print(f"    北交所成交额取到 {len(got)} 天"
          f"{'（下限丢弃 %d 个）' % bad if bad else ''}，缓存累计 {len(merged)} 天")
    return merged


def fetch_amount(date, live_total=None, bj_today=None):
    """全市场成交额（沪 + 深 + 北交所）+ 对 5/10/20/120/250 日均值的比值

    沪深段用同花顺年度文件（非东财、抗封）；北交所段用东财北证50 指数（0.899050）成交额。
    只有两段都齐的交易日才计入合并序列，保证分子分母同口径。
    """
    d0 = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    # 当日（live_total 非空时即为今天）的沪深成交额以实时口径覆盖年度文件，防止采信未结算的残缺当日行
    hs = refresh_amount_cache(force=FORCE_AMOUNT,
                              today_date=(d0 if live_total else None),
                              today_hs=live_total)
    bj = refresh_bj_amount_cache(force=FORCE_AMOUNT)
    # 当日北交所成交额（逐股加总，准确）写入历史缓存，随时间自然补长
    if bj_today and d0 and abs(bj.get(d0, 0) - bj_today) > 1e6:
        bj = dict(bj)
        bj[d0] = bj_today
        _save_json(BJ_CACHE, bj)
    hs_dates = sorted(hs)
    common = sorted(set(hs) & set(bj)) if (hs and bj) else []
    # 北交所段需足够长、且最近一个月与沪深序列完全对齐（无数据洞），才用于历史均值
    recent_ok = len(common) >= 20 and common[-20:] == hs_dates[-20:]
    bj_ready = len(common) >= 120 and recent_ok and common[-1] == hs_dates[-1]
    if bj_ready:
        series_map = {d: hs[d] + bj[d] for d in common}
        scope = "全市场（沪深京）"
    else:
        series_map = dict(hs)
        scope = "沪深两市"
        if bj:
            note(f"北交所成交额历史仅 {len(common)} 天且不连续（需 ≥120 天并与沪深序列对齐），"
                 f"历史均值暂按沪深两市口径；当日值仍为全市场（含北交所）")
    if not series_map:
        return {}
    # 当日值统一按全市场口径；当日一律以实时口径为准（年度文件当日行可能未结算完整）
    if live_total:
        series_map[d0] = live_total + (bj_today or 0)
    elif d0 in series_map:
        if not scope.startswith("全市场") and bj_today:
            series_map[d0] = series_map[d0] + bj_today
    if d0 not in series_map:
        note(f"成交额序列缺少 {d0}，改用最后一天 {max(series_map)}")
        d0 = max(series_map)
    dates = sorted(series_map)
    idx = dates.index(d0)
    cur = series_map[d0]
    prev = series_map[dates[idx - 1]] if idx > 0 else None
    # 上日也按全市场口径补齐北交所（序列本身是沪深口径时）
    if prev is not None and not scope.startswith("全市场"):
        bj_prev = bj.get(dates[idx - 1])
        if bj_prev:
            prev = prev + bj_prev

    def avg(n):
        w = dates[max(0, idx - n + 1): idx + 1]
        return sum(series_map[x] for x in w) / len(w) if w else None

    bj_d0 = bj.get(d0)
    res = {"scope": scope, "date": d0, "total": cur, "prev_total": prev,
           "vs_prev": (cur - prev) if prev else None,
           "hs_total": (cur - bj_d0) if bj_d0 else cur, "bj_total": bj_d0,
           "bj_in_mean": bool(bj_ready), "bj_days": len(bj), "days_available": len(dates),
           "series_tail": [{"date": x, "amount": series_map[x]} for x in dates[-6:]]}
    for n in (5, 10, 20, 120, 250):
        a = avg(n)
        res[f"ma{n}"] = a
        res[f"ratio_{n}"] = round(cur / a, 4) if a else None
        res[f"ma{n}_days"] = min(n, idx + 1)
    return res


# ============================ 板块 ============================
BOARD_FIELDS = "f2,f3,f6,f8,f12,f14,f104,f105,f128,f140"


def _boards(board_type, pz=500):
    """type 2=行业板块  3=概念板块"""
    base = (f"/api/qt/clist/get?pn=1&pz={pz}&po=1&np=1&fltt=2&invt=2"
            f"&fid=f3&fs=m:90+t:{board_type}&fields={BOARD_FIELDS}")
    j = em_get(base)
    diff = (j.get("data") or {}).get("diff") or []
    rows = []
    for d in diff:
        up, dn = d.get("f104"), d.get("f105")
        tot = (up or 0) + (dn or 0)
        rows.append({
            "name": d.get("f14"), "code": d.get("f12"), "pct": d.get("f3"),
            "amount": d.get("f6"), "turnover": d.get("f8"),
            "up_count": up, "down_count": dn, "members": tot,
            "breadth": round(up / tot, 3) if tot else None,
            "leader": d.get("f128"), "leader_code": d.get("f140"),
        })
    return rows


def fetch_sectors(top=15, min_members=5):
    """行业 + 概念板块涨幅榜。窄口径板块（成员数 < min_members）不进榜，避免“1 只票拉出 6%”的假强势"""
    out = {"industry": [], "concept": [], "source": "eastmoney"}
    try:
        out["industry"] = _boards(2)
    except Exception as e:  # noqa: BLE001
        note(f"行业板块失败 {type(e).__name__}")
    try:
        out["concept"] = _boards(3)
    except Exception as e:  # noqa: BLE001
        note(f"概念板块失败 {type(e).__name__}")

    def pick(rows):
        big = [r for r in rows if (r["members"] or 0) >= min_members]
        return {"top": big[:top], "bottom": big[-top:][::-1]}

    ind = pick(out["industry"])
    con = pick(out["concept"])
    out.update({"industry_top": ind["top"], "industry_bottom": ind["bottom"],
                "concept_top": con["top"], "concept_bottom": con["bottom"]})
    return out


# ============================ 涨停 / 跌停 / 炸板 ============================
UT = "7eea3edcaed734bea9cbfc24409ed989"


def _pool(kind, date):
    path = {"zt": "getTopicZTPool", "dt": "getTopicDTPool", "zb": "getTopicZBPool"}[kind]
    sort = {"zt": "fbt%3Aasc", "dt": "fund%3Aasc", "zb": "fbt%3Aasc"}[kind]
    j = http_get(f"https://push2ex.eastmoney.com/{path}?ut={UT}&dpt=wz.ztzt"
                 f"&Pageindex=0&pagesize=1000&sort={sort}&date={date}")
    d = j.json().get("data") or {}
    return d.get("tc", 0), d.get("pool") or []


def fetch_pools(date):
    out = {}
    try:
        tc, zt = _pool("zt", date)
    except Exception as e:  # noqa: BLE001
        note(f"涨停池失败 {type(e).__name__}")
        return {"zt_count": None, "zt": [], "dt": [], "zb": [], "ladder": {}, "by_sector": []}
    try:
        dc, dtp = _pool("dt", date)
    except Exception:  # noqa: BLE001
        dc, dtp = None, []
    try:
        zc, zbp = _pool("zb", date)
    except Exception:  # noqa: BLE001
        zc, zbp = None, []

    def brief(d):
        return {
            "code": d.get("c"), "name": d.get("n"), "pct": d.get("zdp"),
            "lbc": d.get("lbc"), "hybk": d.get("hybk"),
            "fund": d.get("fund"), "zbc": d.get("zbc"),
            "hs": d.get("hs"), "ltsz": d.get("ltsz"),
        }

    zt_brief = [brief(d) for d in zt]
    zt_brief.sort(key=lambda x: (-(x["lbc"] or 1), -(x["fund"] or 0)))

    # 连板梯队
    ladder = {"3plus": [], "2": [], "1": []}
    for d in zt_brief:
        n = d["lbc"] or 1
        key = "3plus" if n >= 3 else ("2" if n == 2 else "1")
        ladder[key].append(d)

    # 涨停板块分布
    sec_map = {}
    for d in zt_brief:
        k = d["hybk"] or "其他"
        e = sec_map.setdefault(k, {"sector": k, "count": 0, "max_lbc": 0, "members": []})
        e["count"] += 1
        e["max_lbc"] = max(e["max_lbc"], d["lbc"] or 1)
        e["members"].append(d["name"])
    by_sector = sorted(sec_map.values(), key=lambda x: (-x["count"], -x["max_lbc"]))
    for e in by_sector:
        e["members"] = e["members"][:8]

    denom = tc + (zc or 0)
    return {
        "zt_count": tc, "dt_count": dc, "zb_count": zc,
        "zt": zt_brief, "dt": [brief(d) for d in dtp], "zb": [brief(d) for d in zbp],
        "ladder": {k: v for k, v in ladder.items()},
        "by_sector": by_sector,
        "seal_rate": round(tc / denom, 4) if denom else None,
        "break_rate": round((zc or 0) / denom, 4) if denom else None,
        "avg_zbc": round(sum(d["zbc"] or 0 for d in zt_brief) / len(zt_brief), 2) if zt_brief else None,
    }


# ============================ 新闻 ============================
def fetch_news(limit=40):
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        rows = []
        for _, r in df.head(limit).iterrows():
            rows.append({"time": str(r.get("发布时间", "")), "title": str(r.get("标题", "")),
                         "summary": str(r.get("摘要", "")), "url": str(r.get("链接", ""))})
        return rows
    except Exception as e:  # noqa: BLE001
        note(f"新闻抓取失败 {type(e).__name__}: {str(e)[:60]}")
        return []


# ============================ 持仓 ============================
def fetch_holdings():
    if not os.path.exists(HOLD_PATH):
        return []
    try:
        items = json.load(open(HOLD_PATH, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        rec = dict(it)
        secid = it.get("secid")
        if secid:
            try:
                j = em_get(f"/api/qt/stock/get?secid={secid}"
                           "&fields=f43,f57,f58,f60,f169,f170,f116,f117,f162,f167,f168")
                d = j.get("data") or {}
                rec.update({"name": d.get("f58"), "price": d.get("f43"),
                            "chg": d.get("f169"), "pct": d.get("f170"),
                            "pe": d.get("f162"), "pb": d.get("f167"),
                            "turnover": d.get("f168"), "mktcap": d.get("f116")})
            except Exception as e:  # noqa: BLE001
                rec["error"] = type(e).__name__
        out.append(rec)
    return out


# ============================ 汇总 ============================
def build(date=None, with_news=True):
    date = date or latest_trade_date()
    print(f"[采集] 目标交易日 {date}")
    snap = {"date": date, "generated_at": dt.datetime.now().isoformat(timespec="seconds")}

    print("  - 指数行情")
    snap["indices"] = fetch_indices()
    live_total = None
    by_name = {x["name"]: x for x in snap["indices"]}
    if "上证指数" in by_name and "深证成指" in by_name:
        a1, a2 = by_name["上证指数"].get("amount"), by_name["深证成指"].get("amount")
        if a1 and a2:
            live_total = a1 + a2
    # fetch_indices() 取的是**实时**行情，只对"今天"这一日有效。目标日期不是今天时必须弃用，
    # 否则（回填 / --date 指定历史日）会把当日实时成交额错当成那天的历史值。
    if date != dt.date.today().strftime("%Y%m%d"):
        live_total = None
    print("  - 涨停/跌停/炸板池")
    snap["pools"] = fetch_pools(date)
    print("  - 涨跌家数与区间分布")
    snap["breadth"] = fetch_breadth(date, snap["pools"])
    print("  - 成交额与历史均值")
    bj_today = ((snap["breadth"].get("by_market") or {}).get("京") or {}).get("amount")
    snap["amount"] = fetch_amount(date, live_total=live_total, bj_today=bj_today)
    print("  - 行业/概念板块排名")
    snap["sectors"] = fetch_sectors()
    if with_news:
        print("  - 财经快讯")
        snap["news"] = fetch_news()
    snap["holdings"] = fetch_holdings()
    snap["warnings"] = WARN

    ensure_dirs()
    path = os.path.join(HIST_DIR, f"{date}.json")
    json.dump(snap, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[完成] 快照已存 {path}")

    # 摘要
    idx = snap["indices"]
    b = snap["breadth"]
    a = snap["amount"]
    p = snap["pools"]
    print("\n===== 摘要 =====")
    for x in idx:
        print(f"  {x['name']:<8} {x['close']:>10} {x['pct']:>7}%  额 {fmt_yi(x.get('amount'))}")
    if b.get("up") is not None:
        print(f"  涨跌家数[{b.get('scope')}] 涨 {b['up']} / 跌 {b['down']}  平 {b.get('flat')}  "
              f"涨跌比 {b.get('ratio')}  涨停 {b.get('limit_up')} 跌停 {b.get('limit_down')}")
        hso = b.get("hs_only") or {}
        if hso:
            print(f"    （沪深两市口径 涨 {hso.get('up')} / 跌 {hso.get('down')}；"
                  f"沪深涨停池口径 {((b.get('pool_scope') or {}).get('limit_up'))}）")
    if a:
        print(f"  成交额[{a.get('scope')}] {fmt_yi(a.get('total'))}  较上日 {fmt_yi(a.get('vs_prev'))}  "
              f"5日比 {a.get('ratio_5')}  20日比 {a.get('ratio_20')}  "
              f"(序列 {a.get('days_available')} 天 / 北交所段 {a.get('bj_days')} 天)")
    if p.get("zt_count") is not None:
        print(f"  涨停 {p['zt_count']}  跌停 {p['dt_count']}  炸板 {p['zb_count']}  "
              f"封板率 {p.get('seal_rate')}  炸板率 {p.get('break_rate')}")
        print(f"  梯队 三板以上 {len(p['ladder'].get('3plus', []))} / "
              f"二板 {len(p['ladder'].get('2', []))} / 首板 {len(p['ladder'].get('1', []))}")
        print("  涨停板块 Top5: " + ", ".join(f"{x['sector']}({x['count']})" for x in p["by_sector"][:5]))
    if snap["sectors"]["industry_top"]:
        print("  行业涨幅 Top5: " + ", ".join(
            f"{x['name']} {x['pct']}%({x['leader']},{x['up_count']}涨/{x['down_count']}跌)"
            for x in snap["sectors"]["industry_top"][:5]))
    if snap["sectors"]["concept_top"]:
        print("  概念涨幅 Top5: " + ", ".join(
            f"{x['name']} {x['pct']}%" for x in snap["sectors"]["concept_top"][:5]))
    if WARN:
        print("\n  ⚠ 警告: " + " | ".join(WARN))
    return snap, path


def fmt_yi(v):
    if v is None:
        return "-"
    return f"{v / 1e8:.0f}亿" if abs(v) >= 1e8 else f"{v / 1e4:.0f}万"


def is_today_trade_day():
    """供定时任务判断：今天是否交易日（非交易日应跳过，避免重复生成旧报告）"""
    latest = latest_trade_date()
    today = dt.date.today().strftime("%Y%m%d")
    ok = latest == today
    print(("YES" if ok else "NO") + f"  (最新交易日 {latest}, 今天 {today})")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYYMMDD")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--force-amount", action="store_true", help="强制重建成交额历史缓存")
    ap.add_argument("--check-today", action="store_true", help="仅判断今天是否交易日，输出 YES/NO")
    ap.add_argument("--show-paths", action="store_true",
                    help="运行环境自检：skill 目录 / 数据根 / Python / 依赖 / 可写性")
    a = ap.parse_args()
    if a.show_paths:
        import paths
        print(paths.describe())
        if paths.missing_deps():
            print()
            print(paths.pip_hint())
        sys.exit(0)
    ensure_deps()          # 真正干活前校验依赖（缺则打印安装指引后退出，退出码 2）
    if a.check_today:
        sys.exit(0 if is_today_trade_day() else 1)
    FORCE_AMOUNT = a.force_amount
    build(a.date, with_news=not a.no_news)
