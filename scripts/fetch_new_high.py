# -*- coding: utf-8 -*-
"""A股每日复盘 · 创新高采集层（同花顺「技术选股-创新高」）

为什么单独一个脚本、而且要按日累积
  同花顺数据中心 http://data.10jqka.com.cn/rank/cxg/ 只提供**当前**榜单：
  URL 里没有日期参数，实测把 date/YYYY-MM-DD 挂在路径末尾、挂在 board 之后
  等多种形式，返回内容字节数完全相同 → 日期被服务端忽略，**没有历史可回填**。
  所以家数曲线只能从启用当天起一天一个点累积出来。

窗口（board 编号）
  4 = 创月新高（约近 20 个交易日，默认）
  3 = 半年新高（约 120 个交易日）
  2 = 一年新高（约 250 个交易日）
  1 = 历史新高
  注意：同花顺**没有 60 日这一档**。

输出（数据根见 paths.py，默认 ~/.stock-review）
  <数据根>/data/newhigh/<YYYYMMDD>.json   当日明细 + 家数 + 前期高点日期分布
  <数据根>/data/newhigh/<YYYYMMDD>.csv    同日明细（Excel 可直接打开）
  <数据根>/data/newhigh/series.json       {"日期": 家数} 累积序列，供画曲线

用法
  python fetch_new_high.py                  # 取 history 里最新交易日，采创月新高
  python fetch_new_high.py --date 20261009
  python fetch_new_high.py --window 半年新高
  python fetch_new_high.py --probe          # 只探数据源可用性，不落盘

出网
  默认直连（绕过系统代理）；脚本会在 6 秒硬超时内自动探测，直连不通就切到系统代理，
  两条都试过才报错。也可用 STOCK_REVIEW_TRUST_ENV=1 让系统代理作为默认通道。
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from paths import CACHE_DIR, HIST_DIR, NEWHIGH_DIR, ensure_deps, ensure_dirs  # noqa: E402

try:
    import requests  # noqa: E402
except ImportError:  # 缺依赖时保持 None，ensure_deps() 会给出安装指引
    requests = None

# ---------- 环境修补：默认绕过系统代理 ----------
# 本机与企业网络常配 127.0.0.1:xxxx 本地代理，会污染 requests 导致接口全挂，
# 故默认关闭 trust_env（与 fetch_report_data.py 的处理保持一致）。
# 如需**默认**走系统代理，设 STOCK_REVIEW_TRUST_ENV=1。
# 注意：无论默认是哪条通道，_get() 都会在硬超时内自动探测并回退到可用的那一条，
#       所以本脚本在「必须走代理」的受限环境里也能自己跑通，无需额外配置。
if requests is not None and (os.environ.get("STOCK_REVIEW_TRUST_ENV") or "").strip().lower() not in (
        "1", "true", "yes", "on"):
    _orig_sess_init = requests.Session.__init__

    def _patched_sess_init(self, *a, **k):  # noqa: ANN001
        _orig_sess_init(self, *a, **k)
        self.trust_env = False

    requests.Session.__init__ = _patched_sess_init

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36")
BASE = "http://data.10jqka.com.cn/rank/cxg/board/{board}"
PAGE_URL = BASE + "/field/stockcode/order/asc/page/{page}/ajax/1/free/1/"

WINDOWS = {"创月新高": 4, "半年新高": 3, "一年新高": 2, "历史新高": 1}
DEFAULT_WINDOW = "创月新高"

PAGE_GAP = 0.6        # 页间间隔：同花顺限流很严，实测连发会被整体拒绝
PAGE_RETRY = 3        # 单页重试次数
COLS = ["序号", "股票代码", "股票简称", "涨跌幅", "换手率",
        "最新价", "前期高点", "前期高点日期"]


def log(msg):
    print(msg, flush=True)


# ---------------- 反爬 cookie ----------------
def _v_js():
    """同花顺需要一个由 ths.js 生成的 v= cookie；复用 akshare 自带的脚本。

    返回 (make_v, closer)：make_v() 取一个可用 cookie，closer() 释放 JS 引擎。
    注意 py_mini_racer 在解释器关闭阶段可能挂起，调用方需硬退出（见文件末尾）。
    """
    import inspect
    import py_mini_racer
    import akshare as ak

    mod = inspect.getmodule(ak.stock_rank_cxg_ths)
    js = py_mini_racer.MiniRacer()
    js.eval(mod._get_file_content_ths("ths.js"))

    def make_v():
        return js.call("v")

    def closer():
        try:
            js.close()
        except Exception:  # noqa: BLE001
            pass
    return make_v, closer


# ---------------- 单页抓取 ----------------
def _parse_page(text):
    """把榜单页 HTML 解析成 [[8 列], ...]；无表格返回 []。"""
    import pandas as pd
    from io import StringIO

    fixed = re.sub(r'\srowspan="\d+"', "", text)   # rowspan 会让 read_html 错位
    try:
        dfs = pd.read_html(StringIO(fixed), header=0)
    except ValueError:
        return []
    if not dfs:
        return []
    df = dfs[0]
    if df.shape[1] < len(COLS):
        return []
    df = df.iloc[:, :len(COLS)]
    out = []
    for row in df.itertuples(index=False):
        vals = [str(x) for x in row]
        # read_html 会把 000011 这类代码当数字 → 丢了前导零，这里补回来
        if vals[1].strip().isdigit():
            vals[1] = vals[1].strip().zfill(6)
        out.append(vals)
    return out


def _total_pages(text):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "lxml")
    span = soup.find(name="span", attrs={"class": "page_info"})
    if not span:
        return 1
    try:
        return max(1, int(span.text.split("/")[1]))
    except Exception:  # noqa: BLE001
        return 1


# ---------------- 出网通道：直连 / 系统代理 自动探测 ----------------
# 两种环境都会遇到，且症状相反：
#   · 本机/企业网络的本地代理会污染 requests → 必须直连（文件顶部的全局补丁）；
#   · 受限环境（容器 / 沙箱）限制直连、只放行系统代理 → 直连会长时间挂起（实测 75s+，
#     且 socket read timeout 不一定会触发，请求像被黑洞吞掉）。
# 所以每次运行先探测一次通道：默认通道按 STOCK_REVIEW_TRUST_ENV 决定（默认直连），
# 在硬超时内拿不到结果就换另一条；选定后本次运行的所有分页都沿用它。
_CHANNEL = {"cur": None}
_PROBE_DEADLINE = 6      # 通道探测的墙钟上限（秒），避免被黑洞请求拖死
_REQ_TMO = (10, 15)      # 通道确定后的常规请求超时


def _default_channel():
    """默认通道：True=走系统代理，False=直连。"""
    v = (os.environ.get("STOCK_REVIEW_TRUST_ENV") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _once(url, cookie, trust_env, tmo):
    """单次请求（独立 Session，不复用 keep-alive 连接）。返回 (状态码, 文本) / (None, 原因)。"""
    s = None
    try:
        s = requests.Session()
        s.trust_env = bool(trust_env)          # 显式覆盖文件顶部的全局补丁
        r = s.get(url,
                  headers={"User-Agent": UA, "Cookie": "v=%s" % cookie,
                           "Connection": "close"},
                  timeout=tmo)
        if r.status_code == 200 and r.text:
            return r.status_code, r.text
        return None, "http=%s bytes=%d" % (r.status_code, len(r.text or ""))
    except Exception as e:  # noqa: BLE001
        return None, "%s %s" % (type(e).__name__, str(e)[:100])
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def _once_bounded(url, cookie, trust_env, deadline):
    """把单次请求放进线程并施加硬超时——某些网络黑洞不会触发 socket 超时。"""
    import threading
    box = {}

    def work():
        box["r"] = _once(url, cookie, trust_env, (min(5, deadline), deadline))

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(deadline + 1)
    if t.is_alive():
        return None, "无响应(>%ds)" % deadline
    return box.get("r") or (None, "无返回")


def _get(url, make_v, tries=PAGE_RETRY):
    """带通道自适应 + 重试的单页请求，返回 (状态码, 文本)。"""
    ch = _CHANNEL["cur"]
    if ch is None:
        first = _default_channel()
        notes = []
        for cand in (first, not first):
            code, text = _once_bounded(url, make_v(), cand, _PROBE_DEADLINE)
            if code is not None:
                _CHANNEL["cur"] = cand
                if cand is not first:
                    log("  · 默认通道（%s）不通，已自动切换为%s"
                        % ("系统代理" if first else "直连",
                           "系统代理" if cand else "直连"))
                return code, text
            notes.append("%s→%s" % ("系统代理" if cand else "直连", text))
        ch = first
        last = "；".join(notes)
    else:
        last = None

    for i in range(tries):
        code, text = _once(url, make_v(), ch, _REQ_TMO)
        if code is not None:
            return code, text
        last = text
        if i < tries - 1:
            time.sleep(1.5 ** i)          # 1.5s / 2.25s 退避
    return None, last


def _f(x):
    """'3.88%' / '12.24' → float；失败返回 None。"""
    if x is None:
        return None
    s = str(x).strip().replace("%", "").replace(",", "")
    if s in ("", "-", "--", "None", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _days_since(date, d):
    """交易日 - 前期高点日期 = 距前期高点天数；解析失败返回 None。"""
    try:
        return (dt.date(int(date[:4]), int(date[4:6]), int(date[6:]))
                - dt.date.fromisoformat(str(d)[:10])).days
    except Exception:  # noqa: BLE001
        return None


# ---------------- 行业映射（东财，一次请求拿全市场） ----------------
INDUSTRY_CACHE = os.path.join(CACHE_DIR, "industry_map.json")
INDUSTRY_DAYS = 7      # 行业属性变化极慢，7 天内直接复用缓存


def fetch_industry_map(force=False, quiet=False):
    """{6 位代码: 东财所属行业}，缓存到 <缓存>/industry_map.json。

    同花顺创新高榜**没有行业字段**，要做「板块分布」必须再映射一次行业。
    用东财 clist 的 f100字段一次拿全市场（沪深京），比 akshare 逐板块遍历省几百个请求。
    抓取失败时回退到旧缓存（可能过期但不至于没得用）；再失败就返回 {}，
    报告会自动隐藏板块分布，不影响曲线与摘要。
    """
    def _cached():
        try:
            d = json.load(open(INDUSTRY_CACHE, encoding="utf-8"))
            return d.get("map") or {}, str(d.get("_stamp") or "")
        except Exception:  # noqa: BLE001
            return {}, ""

    old, stamp = _cached()
    if not force and old and stamp:
        try:
            if (dt.date.today() - dt.date.fromisoformat(stamp)).days <= INDUSTRY_DAYS:
                if not quiet:
                    log("  行业映射：复用缓存（%s，%d 只）" % (stamp, len(old)))
                return old
        except Exception:  # noqa: BLE001
            pass

    try:
        import fetch_report_data as F            # 复用其主机降级 + 限速 + 重试
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        j = F.em_get("/api/qt/clist/get?pn=1&pz=8000&po=0&np=1&fltt=2&invt=2"
                     f"&fid=f12&fs={fs}&fields=f12,f100")
        mp = {}
        for d in (j.get("data") or {}).get("diff") or []:
            code = str(d.get("f12") or "").strip()
            ind = str(d.get("f100") or "").strip()
            if code.isdigit() and ind and ind not in ("-", "--"):
                mp[code] = ind
        if len(mp) < 1000:
            raise RuntimeError("只拿到 %d 条，疑似接口异常" % len(mp))
        json.dump({"_stamp": dt.date.today().isoformat(), "_count": len(mp), "map": mp},
                  open(INDUSTRY_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        if not quiet:
            log("  行业映射：已更新（%d 只）" % len(mp))
        return mp
    except Exception as ex:  # noqa: BLE001
        if old:
            log("  ! 行业映射抓取失败（%s），复用旧缓存 %s（%d 只）"
                % (type(ex).__name__, stamp or "?", len(old)))
            return old
        log("  ! 行业映射不可用（%s），报告将不显示板块分布" % type(ex).__name__)
        return {}


# ---------------- 主流程 ----------------
def fetch(window=DEFAULT_WINDOW, gap=PAGE_GAP, quiet=False):
    """抓完整榜单 → (rows, meta)；失败抛 RuntimeError。"""
    ensure_deps()
    board = WINDOWS[window]
    url_of = lambda p: PAGE_URL.format(board=board, page=p)  # noqa: E731

    make_v, closer = _v_js()
    try:
        t0 = time.time()
        code, text = _get(url_of(1), make_v)
        if code is None:
            raise RuntimeError(
                "同花顺第 1 页请求失败（%s）。\n"
                "  最常见原因：接口被限流（实测短时间内连续调用会被整体拒绝），"
                "或本机代理拦截了 data.10jqka.com.cn。\n"
                "  建议：等几分钟后重试；若长期失败，检查是否需要关闭系统代理。" % text)
        total = _total_pages(text)
        rows = _parse_page(text)
        pages_ok = 1 if rows else 0
        if not quiet:
            log("  第 1/%d 页：%d 行" % (total, len(rows)))

        for p in range(2, total + 1):
            time.sleep(gap)
            code, text = _get(url_of(p), make_v)
            if code is None:
                if not quiet:
                    log("  第 %d/%d 页失败（%s），跳过" % (p, total, text))
                continue
            got = _parse_page(text)
            if not got:
                if not quiet:
                    log("  第 %d/%d 页返回空表（可能被限流），跳过" % (p, total))
                continue
            rows.extend(got)
            pages_ok += 1
            if not quiet:
                log("  第 %d/%d 页：%d 行" % (p, total, len(got)))

        if not rows:
            raise RuntimeError("同花顺返回空榜单，未能解析出任何个股（可能被限流）")

        parsed, dist = [], {}
        for v in rows:
            code_, name = v[1].strip(), v[2].strip()
            if not code_ or not code_.isdigit():
                continue
            d = v[7].strip()[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                dist[d] = dist.get(d, 0) + 1
            parsed.append({
                "code": code_, "name": name,
                "pct": _f(v[3]), "turnover": _f(v[4]),
                "price": _f(v[5]), "prev_high": _f(v[6]), "prev_high_date": d,
            })
        meta = {
            "window": window, "board": board,
            "count": len(parsed), "pages": total, "pages_ok": pages_ok,
            "partial": pages_ok < total,
            "elapsed": round(time.time() - t0, 1),
            "prev_high_date_dist": dict(sorted(dist.items())),
        }
        return parsed, meta
    finally:
        closer()


# ---------------- 落盘 ----------------
def save(date, rows, meta, industry=None):
    ensure_dirs()
    industry = industry or {}
    for r in rows:
        r["industry"] = industry.get(r["code"], "")
        r["prev_high_days"] = _days_since(date, r.get("prev_high_date"))
    meta["industry_mapped"] = sum(1 for r in rows if r.get("industry"))
    meta["industry_total"] = len(rows)

    payload = {"date": date, "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "source": "同花顺数据中心 /rank/cxg/", **meta, "rows": rows}
    jp = os.path.join(NEWHIGH_DIR, "%s.json" % date)
    json.dump(payload, open(jp, "w", encoding="utf-8"), ensure_ascii=False)

    cp = os.path.join(NEWHIGH_DIR, "%s.csv" % date)
    with open(cp, "w", encoding="utf-8-sig", newline="") as f:
        f.write("股票代码,股票简称,涨跌幅,换手率,最新价,前期高点,前期高点日期,"
                "距前期高点天数,所属行业\n")
        for r in rows:
            f.write("%s,%s,%s,%s,%s,%s,%s,%s,%s\n" % (
                r["code"], r["name"], r["pct"], r["turnover"],
                r["price"], r["prev_high"], r["prev_high_date"],
                r["prev_high_days"] if r["prev_high_days"] is not None else "",
                r["industry"]))

    sp = os.path.join(NEWHIGH_DIR, "series.json")
    series = {}
    if os.path.exists(sp):
        try:
            series = json.load(open(sp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            series = {}
    if meta.get("partial"):
        # 有页失败 → 家数偏小，写进曲线就是错的：宁可缺一天，不可错一天
        log("  ! 有页抓取失败，本次家数（%d）不计入系列，仅保留明细" % meta["count"])
    else:
        series[date] = meta["count"]
    json.dump(dict(sorted(series.items())), open(sp, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return jp, cp, sp, series


def _latest_date():
    files = sorted(glob.glob(os.path.join(HIST_DIR, "*.json")))
    if not files:
        raise SystemExit(
            "没有找到任何数据快照，无法确定交易日：\n"
            f"  {HIST_DIR}\n"
            "  请先跑采集脚本：python <skill目录>/scripts/fetch_report_data.py\n"
            "  或显式指定日期：--date 20261009")
    return os.path.basename(files[-1])[:-5]


def main():
    ap = argparse.ArgumentParser(description="同花顺「创新高」榜单采集")
    ap.add_argument("--date", help="交易日 YYYYMMDD（默认取 history 里最新一天）")
    ap.add_argument("--window", default=DEFAULT_WINDOW, choices=list(WINDOWS),
                    help="创新高窗口（默认 创月新高；同花顺无 60 日档）")
    ap.add_argument("--probe", action="store_true", help="只探数据源可用性，不落盘")
    ap.add_argument("--no-industry", action="store_true",
                    help="跳过东财行业映射（报告将不显示板块分布）")
    ap.add_argument("--show-paths", action="store_true", help="打印数据根等信息")
    a = ap.parse_args()

    if a.show_paths:
        import paths
        print(paths.describe())
        return 0

    date = (a.date or _latest_date()).replace("-", "")
    log("[创新高] %s · %s" % (date, a.window))
    rows, meta = fetch(a.window, quiet=a.probe)
    log("  共 %d 只（%d 页，耗时 %.1fs%s）"
        % (meta["count"], meta["pages"], meta["elapsed"],
           "，部分页失败" if meta["partial"] else ""))

    if a.probe:
        log("  --probe：不落盘。样例前 3 只：")
        for r in rows[:3]:
            log("    %s %s 最新价%s 前期高点%s(%s)"
                % (r["code"], r["name"], r["price"], r["prev_high"], r["prev_high_date"]))
        dist = meta["prev_high_date_dist"]
        if dist:
            ks = sorted(dist)
            log("  前期高点日期分布：%s … %s（共 %d 个不同日期）"
                % (ks[0], ks[-1], len(ks)))
        return 0

    industry = {} if a.no_industry else fetch_industry_map()
    jp, cp, sp, series = save(date, rows, meta, industry)
    log("  明细 : %s" % jp)
    log("  明细 : %s" % cp)
    log("  序列 : %s（已累积 %d 个交易日）" % (sp, len(series)))
    if industry:
        log("  行业 : 已映射 %d/%d 只，报告可渲染行业分布"
            % (meta["industry_mapped"], meta["industry_total"]))
    else:
        log("  行业 : 未映射，报告将不显示行业分布")
    if len(series) < 3:
        log("  提示 : 家数曲线至少需要 3 个交易日才能看出形态，"
            "当前 %d 天 —— 同花顺接口无历史，只能逐日累积。" % len(series))
    return 0


if __name__ == "__main__":
    _code = 1
    try:
        _code = main()
    except SystemExit as ex:
        _code = ex.code if isinstance(ex.code, int) else 1
    except Exception as ex:  # noqa: BLE001
        print("[错误] %s: %s" % (type(ex).__name__, ex))
        _code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        # py_mini_racer 在解释器关闭阶段可能挂起（实测），必须硬退出
        os._exit(_code)
