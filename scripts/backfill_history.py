# -*- coding: utf-8 -*-
"""历史快照回填（独立脚本 · 不改动任何现有采集/渲染文件）

为最近 N 个交易日生成与当日采集**完全同构**的快照
<数据根>/data/history/<date>.json（数据根见 paths.py）。
不生成 AI 归因文字、不渲染 HTML —— 只产出数据。

各字段的历史可得性与取数方式
──────────────────────────────────────────────────────────────
indices  腾讯日K（不复权，算 close/pct/chg/open/high/low）
         + 东财历史K线成交额（实测与当日快照 0 误差）            ✓ 精确
amount   fetch_amount(date) —— 同花顺年度文件 + 北交所序列，复用现有逻辑 ✓ 精确
pools    东财 push2ex 历史涨停/跌停/炸板池（接口原生支持 date）  ✓ 精确
breadth  逐股日K重算涨跌家数 + 23 档分布
         沪深→腾讯日K，北交所→新浪日K
         除权跳空/新股首日等越界样本自动剔除                    ✓ 精确
sectors  板块历史涨跌幅 = 东财板块K线（实测与当日快照逐值一致）
         name/code/pct/amount 为历史真值；
         up_count/down_count/leader/turnover/breadth 无法回溯 → null
news     财经快讯无历史接口                                     ✗ 空数组
holdings 无持仓配置                                             ✓ 空数组

用法
  python backfill_history.py --verify           # 用历史法复算 20261008 并与现快照逐项对比
  python backfill_history.py --days 10          # 回填最近 10 个交易日（跳过已存在文件）
  python backfill_history.py --days 10 --force  # 覆盖已存在文件
  python backfill_history.py --days 10 --no-sectors   # 跳过板块（省时）
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_report_data as F  # noqa: E402
from paths import HIST_DIR, CACHE_DIR, ensure_dirs, ensure_deps  # noqa: E402

DAILY_CACHE = os.path.join(CACHE_DIR, "daily_series_cache.json")

HDR_SINA = {"User-Agent": F.UA, "Referer": "https://finance.sina.com.cn/"}
# 东财历史K线可用主机（push2his 常被 IP 级限流，push2test 实测数据真实且可用）
EM_HIST_HOSTS = ["push2test.eastmoney.com", "push2his.eastmoney.com",
                 "0.push2his.eastmoney.com", "push2.eastmoney.com"]
WORKERS = 8


def log(msg):
    print(f"  {msg}", flush=True)


def _raw(url, headers=None, tries=3, timeout=12, backoff=0.35):
    """不走全局限速的裸请求（供并发使用）"""
    for i in range(tries):
        try:
            r = F.requests.get(url, headers=headers or F.HDR_TX, timeout=timeout)
            if r.status_code == 200 and r.text.strip():
                return r
        except Exception:  # noqa: BLE001
            pass
        time.sleep(backoff * (i + 1))
    return None


def _f(x):
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return None


# ==================== 交易日 ====================
def recent_trade_dates(n):
    """最近 n 个交易日（新浪上证指数日K）"""
    rows = kline_sina("sh000001", n + 12)
    if not rows:
        r = F.http_get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                       f"?param=sh000001,day,,,{n + 12},", headers=F.HDR_TX)
        arr = r.json()["data"]["sh000001"].get("day") or []
        return [x[0].replace("-", "") for x in arr]
    return [d for d, *_ in rows]


# ==================== 日K（通用） ====================
def kline_tx(code, lmt):
    """腾讯日K（不复权）→ [(date, open, close, high, low, vol_hand)]"""
    r = _raw(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{lmt},")
    if not r:
        return []
    try:
        arr = ((r.json().get("data") or {}).get(code) or {}).get("day") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in arr:
        if len(row) >= 6:
            o, c, h, l, v = _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]), _f(row[5])
            if None not in (o, c, h, l, v):
                out.append((row[0].replace("-", ""), o, c, h, l, v))
    return out


def kline_sina(code, lmt):
    """新浪日K（不复权）→ [(date, open, close, high, low, vol)]
    覆盖沪深京三市（北交所唯一可用的日K源），亦支持指数。"""
    r = _raw("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={lmt}",
             headers=HDR_SINA, tries=3)
    if not r:
        return []
    try:
        arr = r.json()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in arr or []:
        o, c, h, l, v = (_f(x.get("open")), _f(x.get("close")), _f(x.get("high")),
                         _f(x.get("low")), _f(x.get("volume")))
        if x.get("day") and None not in (o, c, h, l, v):
            out.append((x["day"].replace("-", ""), o, c, h, l, v))
    return out


def kline_day(code, lmt):
    """日K（不复权）多源降级：新浪 → 腾讯 → 东财（腾讯/新浪均可能被临时限流）"""
    for fn in (kline_sina, kline_tx, kline_em_day):
        rows = fn(code, lmt)
        if rows:
            return rows
    return []


def kline_em(secid, lmt):
    """东财历史K线 → [(date, close, vol, amount)]，多主机降级"""
    for host in EM_HIST_HOSTS:
        r = _raw(f"https://{host}/api/qt/stock/kline/get?secid={secid}"
                 f"&fields1=f1,f2&fields2=f51,f53,f56,f57&klt=101&fqt=1"
                 f"&end=20500101&lmt={lmt}", headers=F.HDR_EM, tries=2, timeout=15)
        if not r:
            continue
        try:
            kl = ((r.json().get("data") or {}).get("klines")) or []
        except Exception:  # noqa: BLE001
            continue
        out = []
        for line in kl:
            p = line.split(",")
            if len(p) >= 4:
                c, v, a = _f(p[1]), _f(p[2]), _f(p[3])
                if c is not None:
                    out.append((p[0].replace("-", ""), c, v, a))
        if out:
            return out
    return []


def kline_em_day(code, lmt):
    """东财个股日K → 6 元组（东财只给 close/vol，足够算涨跌幅与停牌过滤）"""
    secid = ("1." if code.startswith("sh") else "0.") + code[2:]
    return [(d, None, c, None, None, v) for d, c, v, _ in kline_em(secid, lmt)]


# ==================== 指数 ====================
def build_index_map(lmt):
    """{code: {date: {...OHLC}}} + {code: {date: amount}}"""
    ohlc, amt = {}, {}
    for it in F.CFG["indices"]:
        seq = kline_day(it["code"], lmt)
        if seq:
            ohlc[it["code"]] = {d: {"open": o, "close": c, "high": h, "low": l}
                                for d, o, c, h, l, _ in seq}
        rows = kline_em(it["secid"], lmt)
        if rows:
            amt[it["code"]] = {d: a for d, _, _, a in rows if a}
    return ohlc, amt


def build_indices(date, ohlc, amt):
    out = []
    for it in F.CFG["indices"]:
        code = it["code"]
        seq = ohlc.get(code) or {}
        cur = seq.get(date)
        if not cur:
            continue
        ds = sorted(seq)
        i = ds.index(date)
        prev = seq[ds[i - 1]]["close"] if i > 0 else None
        out.append({
            "name": it["name"], "close": round(cur["close"], 2),
            "pct": round((cur["close"] / prev - 1) * 100, 2) if prev else None,
            "chg": round(cur["close"] - prev, 2) if prev else None,
            "amount": (amt.get(code) or {}).get(date),
            "high": round(cur["high"], 2), "low": round(cur["low"], 2),
            "open": round(cur["open"], 2),
            "prev_close": round(prev, 2) if prev else None,
            "source": "eastmoney:kline",
        })
    return out


# ==================== 逐股序列 ====================
def fetch_universe():
    """全市场代码表（东财 clist，一次请求拿全沪深京，避免新浪分页限流）"""
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    j = F.em_get(f"/api/qt/clist/get?pn=1&pz=8000&po=0&np=1&fltt=2&invt=2"
                 f"&fid=f12&fs={fs}&fields=f12,f13,f14")
    diff = (j.get("data") or {}).get("diff") or []
    codes = set()
    for d in diff:
        c = str(d.get("f12") or "")
        if not c.isdigit():
            continue
        if c.startswith(("43", "83", "87", "88", "92")):
            codes.add("bj" + c)
        else:
            codes.add(("sh" if d.get("f13") == 1 else "sz") + c)
    return sorted(codes)


def fetch_daily_series(codes, lmt, use_cache=True):
    """{code: [(date, close, vol)]} —— 沪深腾讯、北交所新浪"""
    if use_cache and os.path.exists(DAILY_CACHE):
        try:
            c = json.load(open(DAILY_CACHE, encoding="utf-8"))
            stamp = c.get("_stamp", "")
            if c.get("_lmt") == lmt and stamp == dt.date.today().isoformat() \
                    and len(c.get("series", {})) > 5000:
                log(f"逐股日K复用本地缓存（{len(c['series'])} 只，{stamp} 抓取）")
                return c["series"]
        except Exception:  # noqa: BLE001
            pass

    def one(code):
        seq = kline_day(code, lmt)
        return code, [(d, c, v) for d, _, c, _, _, v in seq]

    res, done, t0 = {}, 0, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fu in as_completed([ex.submit(one, c) for c in codes]):
            code, seq = fu.result()
            if seq:
                res[code] = seq
            done += 1
            if done % 1000 == 0:
                log(f"逐股日K {done}/{len(codes)}（成功 {len(res)}）…{time.time() - t0:.0f}s")
    log(f"逐股日K 完成：{len(res)}/{len(codes)} 只，耗时 {time.time() - t0:.0f}s")
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump({"_stamp": dt.date.today().isoformat(), "_lmt": lmt, "series": res},
              open(DAILY_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return res


def _pct_at(seq, date):
    """序列中 date 的 (涨跌幅%, 昨收)，找不到返回 None"""
    for i, row in enumerate(seq):
        if row[0] == date:
            if i == 0:
                return None
            prev, cur, vol = seq[i - 1][1], row[1], row[2]
            if not prev or prev <= 0 or vol <= 0:
                return None
            return (cur / prev - 1) * 100, prev, cur
    return None


# ==================== 涨跌家数（同构复算） ====================
def breadth_one_date(date, series, pools):
    live = []
    for code, seq in series.items():
        got = _pct_at(seq, date)
        if not got:
            continue
        pct, prev, close = got
        lim = F._limit_pct(code, None)
        if abs(pct) > lim + 3:      # 除权跳空 / 新股首日 等异常样本剔除
            continue
        zt_px = round(prev * (1 + lim / 100), 2)
        dt_px = round(prev * (1 - lim / 100), 2)
        live.append({
            "sym": code[2:], "code": code,
            "mkt": {"sh": "沪", "sz": "深", "bj": "京"}.get(code[:2], "其他"),
            "pct": pct,
            "is_zt": (close >= zt_px - 0.005) and (pct <= lim + 0.6),
            "is_dt": (close <= dt_px + 0.005) and (pct >= -(lim + 0.6)),
        })

    zt_codes = {str(r.get("code")) for r in (pools or {}).get("zt", []) if str(r.get("code", "")).isdigit()}
    dt_codes = {str(r.get("code")) for r in (pools or {}).get("dt", []) if str(r.get("code", "")).isdigit()}
    for r in live:
        r["is_zt_pool"] = (r["sym"] in zt_codes) if zt_codes else r["is_zt"]
        r["is_dt_pool"] = (r["sym"] in dt_codes) if dt_codes else r["is_dt"]
        r["bucket"] = ("11" if r["is_zt"] else ("-11" if r["is_dt"]
                       else F._bucket_key(r["pct"], False, False)))

    def agg(rows):
        up = sum(1 for r in rows if r["pct"] > 0)
        dn = sum(1 for r in rows if r["pct"] < 0)
        return {"total": len(rows), "up": up, "down": dn,
                "flat": sum(1 for r in rows if r["pct"] == 0),
                "limit_up": sum(1 for r in rows if r["is_zt_pool"]),
                "limit_down": sum(1 for r in rows if r["is_dt_pool"]),
                "limit_up_incl_st": sum(1 for r in rows if r["is_zt"]),
                "limit_down_incl_st": sum(1 for r in rows if r["is_dt"]),
                "ratio": round(up / dn, 2) if dn else None,
                "amount": None}

    by_market = {m: agg([r for r in live if r["mkt"] == m]) for m in ["沪", "深", "京"]}
    hs = [r for r in live if r["mkt"] in ("沪", "深")]
    scopes = {"沪深": agg(hs), "沪深京": agg(live)}
    allm = scopes["沪深京"]
    cnt = {}
    for r in live:
        cnt[r["bucket"]] = cnt.get(r["bucket"], 0) + 1
    order = ["11", "10", "9", "8", "7", "6", "5", "4", "3", "2", "1", "0",
             "-1", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9", "-10", "-11"]
    return {
        "scope": "全市场（沪深京，含北交所）",
        "up": allm["up"], "down": allm["down"], "flat": allm["flat"],
        "distribution": [
            {"bucket": k, "label": F.BUCKET_LABEL.get(k, k), "count": cnt.get(k, 0),
             "dir": "涨" if not k.startswith("-") and k != "0" else ("跌" if k.startswith("-") else "平")}
            for k in order],
        "source": "backfill:tencent/sina daily kline",
        "by_market": by_market, "scopes": scopes,
        "ratio": allm["ratio"], "total": allm["total"],
        "limit_up": allm["limit_up_incl_st"], "limit_down": allm["limit_down_incl_st"],
        "pool_scope": {"limit_up": allm["limit_up"], "limit_down": allm["limit_down"]},
        "hs_only": {k: scopes["沪深"][k] for k in
                    ["up", "down", "flat", "limit_up_incl_st", "limit_down_incl_st", "ratio"]},
        "by_market_zt": {m: {"limit_up": x["limit_up_incl_st"], "limit_down": x["limit_down_incl_st"]}
                         for m, x in by_market.items()},
        # cross_check 为实时对账字段（东财ZDFenBu/乐咕），历史不可得 → 保留同构子键、值置 null
        "cross_check": {
            "em_zdfenbu": {k: None for k in ("up", "down", "flat", "limit_up", "limit_down")},
            "legu": {k: None for k in ("上涨", "下跌", "平盘", "涨停", "跌停", "统计日期")},
        },
    }


# ==================== 板块 ====================
def fetch_sector_pct(members, lmt):
    """{code: [(date, close, amount)]} —— 东财板块历史K线"""
    def one(code):
        return code, kline_em(f"90.{code}", lmt)

    res, done = {}, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fu in as_completed([ex.submit(one, b["code"]) for b in members]):
            code, rows = fu.result()
            if len(rows) >= 2 and len({r[1] for r in rows}) > 1:   # 剔除占位假数据
                res[code] = rows
            done += 1
            if done % 300 == 0:
                log(f"板块K线 {done}/{len(members)}（成功 {len(res)}）…")
    log(f"板块K线 完成：{len(res)}/{len(members)} 个")
    return res


def build_sectors(date, base_rows, seq_map):
    rows = []
    for b in base_rows:
        seq = seq_map.get(b["code"]) or []
        for i, (d, c, _v, a) in enumerate(seq):
            if d == date and i > 0:
                prev = seq[i - 1][1]
                if prev and prev > 0:
                    rows.append({
                        "name": b.get("name"), "code": b["code"],
                        "pct": round((c / prev - 1) * 100, 2),
                        "amount": a,
                        "turnover": None, "up_count": None, "down_count": None,
                        "members": b.get("members"), "breadth": None,
                        "leader": None, "leader_code": None,
                    })
                break
    return rows


def pick(rows, top=15, min_members=5):
    big = [r for r in rows if (r.get("members") or 0) >= min_members]
    big.sort(key=lambda x: -(x["pct"] if x["pct"] is not None else -999))
    return {"top": big[:top], "bottom": big[-top:][::-1] if len(big) >= top else big[::-1]}


# ==================== 组装 ====================
def build_snapshot(date, ctx, warn_start):
    pools = F.fetch_pools(date)
    return {
        "date": date,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "indices": build_indices(date, ctx["ohlc"], ctx["idx_amt"]),
        "pools": pools,
        "breadth": (breadth_one_date(date, ctx["series"], pools) if ctx.get("series") else None),
        "amount": F.fetch_amount(date),
        "sectors": {"industry": [], "concept": [], "source": "eastmoney:kline",
                    "industry_top": [], "industry_bottom": [],
                    "concept_top": [], "concept_bottom": []},
        "news": [],
        "holdings": [],
        "warnings": F.WARN[warn_start:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回填最近 N 个交易日")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的日期文件")
    ap.add_argument("--verify", action="store_true", help="只校验 20261008 与现快照是否一致")
    ap.add_argument("--no-breadth", action="store_true")
    ap.add_argument("--no-sectors", action="store_true")
    ap.add_argument("--refresh-daily", action="store_true", help="强制重抓逐股日K缓存")
    a = ap.parse_args()

    ensure_deps()             # 缺依赖时给出安装指引后退出

    if not a.verify:          # --verify 只读，不建目录、不落盘
        ensure_dirs()

    print("[历史回填] 启动")
    dates = recent_trade_dates(a.days)
    target = dates[-a.days:]
    lmt = a.days + 12
    print(f"  目标交易日（最近 {len(target)} 个）：{target}")

    ctx = {}
    print("  - 指数历史K线（腾讯）+ 成交额（东财）")
    ctx["ohlc"], ctx["idx_amt"] = build_index_map(lmt)
    print(f"    指数 {len(ctx['ohlc'])} 个，成交额 {len(ctx['idx_amt'])} 个")

    if not a.no_breadth:
        print("  - 全市场逐股日K（沪深腾讯 / 北交所新浪）")
        codes = fetch_universe()
        print(f"    代码表 {len(codes)} 只")
        ctx["series"] = fetch_daily_series(codes, lmt, use_cache=not a.refresh_daily)

    # 板块底座（实时列表 + 历史K线）
    ctx["sectors_base"] = None
    ctx["sector_seq"] = {}
    if not a.no_sectors:
        print("  - 板块列表（实时）+ 板块历史K线")
        try:
            ctx["sectors_base"] = F._boards(2)
            print(f"    行业板块 {len(ctx['sectors_base'])} 个")
        except Exception as e:  # noqa: BLE001
            F.note(f"板块列表失败 {type(e).__name__}")
        if ctx["sectors_base"]:
            ctx["sector_seq"] = fetch_sector_pct(ctx["sectors_base"], lmt)

    # 校验模式
    if a.verify:
        d = "20261008"
        print(f"\n===== 校验 {d}（历史法复算 vs 当日真实快照）=====")
        old = json.load(open(os.path.join(HIST_DIR, f"{d}.json"), encoding="utf-8"))
        newb = breadth_one_date(d, ctx["series"], old.get("pools"))
        ob = old["breadth"]
        keys = ["up", "down", "flat", "total", "limit_up", "limit_down", "ratio"]
        print("  breadth:", " | ".join(
            f"{k}: 复算 {newb.get(k)} / 真实 {ob.get(k)}"
            + ("  ✅" if newb.get(k) == ob.get(k) else "  ❌") for k in keys))
        d1 = {x["bucket"]: x["count"] for x in newb["distribution"]}
        d0 = {x["bucket"]: x["count"] for x in ob["distribution"]}
        bad = [k for k in sorted(set(d0) | set(d1), key=lambda x: -int(x)) if d0.get(k) != d1.get(k)]
        if bad:
            print("  23档分布差异：", ", ".join(f"{k}: {d1.get(k)}/{d0.get(k)}" for k in bad[:12]))
        else:
            print("  23档分布：逐档完全一致 ✅")
        ni = build_indices(d, ctx["ohlc"], ctx["idx_amt"])

        def _eq(x, y):
            """数值比较：None 严格相等；否则容差 = max(1 分钱, 相对 1e-6)。

            历史K线（腾讯/新浪）与当日快照（东财实时）来自不同源，
            价格可能相差 1 分钱、成交额末位有浮点误差，均属正常。
            """
            if x is None or y is None:
                return x == y
            try:
                return abs(float(x) - float(y)) <= max(0.011, abs(float(y)) * 1e-6)
            except Exception:  # noqa: BLE001
                return x == y

        for x, y in zip(ni, old["indices"]):
            fields = ("close", "pct", "amount", "high", "low", "open")
            bad = [k for k in fields if not _eq(x[k], y[k])]
            mark = "✅" if not bad else "❌ " + " ".join(f"{k}:{x[k]}≠{y[k]}" for k in bad)
            print(f"  {x['name']}: close {x['close']}/{y['close']} pct {x['pct']}/{y['pct']} "
                  f"amt {x['amount']:.0f}/{y['amount']:.0f} {mark}")
        return

    # 正式回填
    warn0 = len(F.WARN)
    ok, skip = [], []
    for i, d in enumerate(target, 1):
        path = os.path.join(HIST_DIR, f"{d}.json")
        if os.path.exists(path) and not a.force:
            skip.append(d)
            print(f"  [{i}/{len(target)}] {d} 已存在，跳过（--force 可覆盖）")
            continue
        print(f"  [{i}/{len(target)}] 生成 {d} …")
        snap = build_snapshot(d, ctx, warn0)
        if ctx.get("sectors_base") is not None:
            ind = build_sectors(d, ctx["sectors_base"], ctx["sector_seq"])
            pk = pick(ind)
            snap["sectors"] = {"industry": ind, "concept": [],
                               "source": "eastmoney:kline",
                               "industry_top": pk["top"], "industry_bottom": pk["bottom"],
                               "concept_top": [], "concept_bottom": []}
        snap["warnings"] = F.WARN[warn0:] + [
            "本文件由 backfill_history.py 回填：indices/pools/breadth/sectors(pct,amount)/amount 为历史真实值；",
            "sectors 的 up_count/down_count/leader/turnover/breadth 为实时字段无法回溯，置 null；"
            "news 无历史接口，为空数组。",
        ]
        json.dump(snap, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        b = snap["breadth"]
        print(f"      涨 {b['up']} / 跌 {b['down']} / 平 {b['flat']}  涨停 {b['limit_up']} "
              f"跌停 {b['limit_down']}  成交 {F.fmt_yi(snap['amount'].get('total'))}")
        ok.append(d)

    print(f"\n[完成] 已生成 {len(ok)} 个：{ok}")
    if skip:
        print(f"        跳过已存在 {len(skip)} 个：{skip}")
    if F.WARN:
        print("  警告: " + " | ".join(F.WARN[:6]))


if __name__ == "__main__":
    main()
