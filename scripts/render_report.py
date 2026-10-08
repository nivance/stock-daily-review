# -*- coding: utf-8 -*-
"""A股每日复盘 · 报告渲染层

输入（路径以 <数据根> 为基准，见 paths.py）
  <数据根>/data/history/<date>.json   采集层产出的数据快照（硬数据）
  <数据根>/data/ai/<date>.json        AI 归因文字（新闻影响分析/主线逻辑/涨停判断/后市展望）
输出
  <数据根>/out/report_<date>.html     浅色（宣纸中国红）手机适配版式复盘报告

AI 文字缺失时报告仍可渲染，对应模块标注「待补」，硬数据部分不受影响。

用法
  python render_report.py --date 20260930
  python render_report.py                       # 取 history 里最新一天
  python render_report.py --show-paths          # 运行环境自检（skill 目录 / 数据根 / 依赖）
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 让 scripts/ 目录可 import（无论从哪个 cwd 启动）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import AI_DIR, HIST_DIR, OUT_DIR, TPL_PATH, ensure_dirs  # noqa: E402

TPL = TPL_PATH

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------- 格式化工具 ----------------
def e(s):
    return html.escape(str(s if s is not None else ""))


def cls(v):
    try:
        v = float(v)
    except Exception:  # noqa: BLE001
        return "mu"
    return "up" if v > 0 else ("dn" if v < 0 else "mu")


def pct(v, dec=2, sign=True):
    if v is None:
        return "-"
    s = f"{v:+.{dec}f}%" if sign else f"{v:.{dec}f}%"
    return s


def yi(v, dec=0):
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    if a >= 1e8:
        return f"{v / 1e8:,.{dec}f}亿"
    return f"{v / 1e4:,.0f}万"


def t2date(s):
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))


def T(inner, stack=False, cls=""):
    """把 <tr> 片段包成表格，并套一层可横向滚动的容器。

    stack=True 表示「文字主导」的表：手机上把每行拆成上下堆叠的卡片，
    避免 2~4 列文字被挤成一条细缝。数字型窄表不加，靠 .tw 横滑兜底。
    """
    classes = [x for x in (cls, "stack" if stack else "") if x]
    attr = f' class="{" ".join(classes)}"' if classes else ""
    return f'<div class="tw"><table{attr}>{inner}</table></div>'


# ---------------- 片段：指数卡片 ----------------
def frag_indices(snap):
    out = []
    for x in snap.get("indices", []):
        c = cls(x.get("pct"))
        arrow = "▲" if (x.get("pct") or 0) > 0 else ("▼" if (x.get("pct") or 0) < 0 else "—")
        out.append(
            f'<div class="kpi"><div class="k-name">{e(x.get("name"))}</div>'
            f'<div class="k-val num">{x.get("close"):,.2f}</div>'
            f'<div class="k-pct num {c}">{arrow} {pct(x.get("pct"))}</div>'
            f'<div class="k-sub num">高 {x.get("high"):,.2f} ｜ 低 {x.get("low"):,.2f}</div>'
            f'<div class="k-sub num">成交 {yi(x.get("amount"))}</div></div>')
    return "".join(out)


# ---------------- 片段：新闻与消息面 ----------------
def frag_news(ai, idx=1):
    n = (ai or {}).get("news") or {}
    items = n.get("items") or []
    head = (f'<h2><i></i>一、新闻与消息面<span class="tag ai">AI 归因</span></h2>'
            f'<div class="sub-t">● {e(n.get("label") or "消息面")}</div>')
    if not items:
        return f'<section class="sec">{head}<div class="empty">— 本次未生成新闻归因（待补 ai.news.items）—</div></section>'
    rows = []
    chip = {"利好": "g", "利空": "b", "中性": "n"}
    for it in items:
        tag = it.get("tag") or "中性"
        rows.append(f'<tr><td><span class="chip {chip.get(tag, "n")}">{e(tag)}</span>'
                    f'<span class="t1">{e(it.get("title"))}</span></td>'
                    f'<td>{e(it.get("text"))}</td></tr>')
    tbl = T(f'<tr><th style="width:28%">事件</th><th>影响分析</th></tr>{"".join(rows)}', stack=True)
    return f'<section class="sec">{head}{tbl}</section>'


# ---------------- 片段：市场交易数据 ----------------
def frag_market(snap, ai):
    b = snap.get("breadth") or {}
    a = snap.get("amount") or {}
    dist = b.get("distribution") or []
    up, dn, flat = b.get("up"), b.get("down"), b.get("flat")
    tot = (up or 0) + (dn or 0) + (flat or 0)
    wu = (up or 0) / tot * 100 if tot else 0
    wd = (dn or 0) / tot * 100 if tot else 0
    wf = max(0.0, 100 - wu - wd)

    left = [
        f'<tr><td class="mu">上涨家数</td><td class="up num">{up} 家（{up / tot * 100:.2f}%）</td></tr>' if tot else "",
        f'<tr><td class="mu">下跌家数</td><td class="dn num">{dn} 家（{dn / tot * 100:.2f}%）</td></tr>' if tot else "",
        f'<tr><td class="mu">平盘</td><td class="num">{flat} 家</td></tr>',
        f'<tr><td class="mu">涨停 / 跌停</td><td class="num"><span class="up">{b.get("limit_up")}</span>'
        f' / <span class="dn">{b.get("limit_down")}</span> 家</td></tr>',
        f'<tr><td class="mu">涨跌比</td><td class="num">{b.get("ratio")} : 1</td></tr>',
    ]

    mx = max([x["count"] for x in dist] or [1])
    drows = []
    for x in dist[:12]:
        pc = x["count"] / mx * 100
        d = " d" if x["dir"] == "跌" else ""
        col = "dn" if x["dir"] == "跌" else ("up" if x["dir"] == "涨" else "mu")
        drows.append(f'<tr><td class="num">{e(x["label"])}</td><td class="num {col}">{x["count"]}</td>'
                     f'<td><span class="mini{d}" style="width:{pc:.1f}%"></span></td></tr>')

    arows = []
    amt_scope_txt = ""
    if a:
        vs = a.get("vs_prev")
        arows.append(f'<tr><td class="mu">全市场成交额</td><td class="t1 num">{yi(a.get("total"))}</td>'
                     f'<td class="num {cls(vs)}">较上日 {yi(vs)}</td></tr>')
        if a.get("bj_total") and a.get("total"):
            arows.append(f'<tr><td class="mu">其中北交所</td><td class="num">{yi(a.get("bj_total"))}</td>'
                         f'<td class="num mu">占比 {a["bj_total"] / a["total"] * 100:.2f}%</td></tr>')
        if a.get("bj_in_mean"):
            amt_scope_txt = "全市场（沪深京）口径"
        else:
            amt_scope_txt = "当日为全市场（含北交所）；历史均值为沪深两市口径（北交所历史数据累积中，日均占比 &lt;1%）"
        for n, label in ((5, "5日"), (10, "10日"), (20, "20日"), (120, "120日"), (250, "250日")):
            r = a.get(f"ratio_{n}")
            mc = "up" if (r or 0) >= 1 else "dn"
            md = a.get(f"ma{n}_days")
            tag = "" if (md is None or md >= n) else f'<span class="mu" style="font-size:10px">（近{md}日）</span>'
            arows.append(f'<tr><td class="mu">{label}均值</td><td class="num">{yi(a.get(f"ma{n}"))}{tag}</td>'
                         f'<td class="num {mc}">当日 / {label} = {r * 100:.2f}%</td></tr>') if r else ""
        arows = [x for x in arows if x]

    cm = (ai or {}).get("market_commentary")
    note = (f'<div class="note"><b>行情惯性（AI）：</b>{e(cm)}</div>' if cm
            else '<div class="note warn"><b>行情惯性：</b>待补（ai.market_commentary）</div>')

    # 口径标注：家数与涨跌停均按全市场（沪深京，含北交所）统计，不剔除
    scope_txt = ""
    if b.get("scope"):
        hso = b.get("hs_only") or {}
        extra = ""
        if hso:
            extra = (f' ｜ 沪深两市口径为 涨 {hso.get("up")} / 跌 {hso.get("down")}'
                     f'（涨停池口径 {((b.get("pool_scope") or {}).get("limit_up"))}）')
        scope_txt = (f'<span class="scope-note">{e(b.get("scope"))}口径 · 涨跌停含 ST{extra}</span>')

    breadth_tbl = T("".join(left))
    dist_tbl = T(f'<tr><th>区间</th><th>家数</th><th>分布</th></tr>{"".join(drows)}', cls="dist")
    amt_tbl = T(f'<tr><th style="width:24%">项目</th><th style="width:26%">数值</th><th>对比</th></tr>{"".join(arows)}')
    amt_note = f'<span class="scope-note">{amt_scope_txt}</span>' if amt_scope_txt else ""

    return f'''<section class="sec">
<h2><i></i>二、市场交易数据<span class="tag mix">数据 + AI</span></h2>
<div class="g2">
  <div>
    <div class="sub-t">涨跌家数分布{scope_txt}</div>
    {breadth_tbl}
    <div class="bar"><span style="width:{wu:.1f}%;background:var(--up)"></span><span style="width:{wf:.1f}%;background:var(--bar-mid)"></span><span style="width:{wd:.1f}%;background:var(--dn)"></span></div>
    <div class="bar-lbl"><span class="up">上涨 {up}</span><span class="mu">平 {flat}</span><span class="dn">下跌 {dn}</span></div>
  </div>
  <div>
    <div class="sub-t">涨跌幅区间分布</div>
    {dist_tbl}
  </div>
</div>
<div class="sub-t">核心成交数据{amt_note}</div>
{amt_tbl}
{note}
</section>'''


# ---------------- 片段：主线与支线 ----------------
def frag_mainline(snap, ai):
    ai = ai or {}
    sec = snap.get("sectors") or {}

    def ai_rows(rows, cols):
        if not rows:
            return '<div class="empty">— 待补 —</div>'
        head = "".join(f'<th>{c[0]}</th>' for c in cols)
        body = []
        for r in rows:
            tds = []
            for i, (_, key) in enumerate(cols):
                v = r.get(key)
                if key == "pct_text":
                    tds.append(f'<td class="num {cls(r.get("pct_val"))}">{e(v)}</td>')
                elif i == 0:
                    tds.append(f'<td class="t1">{e(v)}</td>')
                else:
                    tds.append(f"<td>{e(v)}</td>")
            body.append(f'<tr>{"".join(tds)}</tr>')
        return T(f'<tr>{head}</tr>{"".join(body)}', stack=True)

    main_cols = [("主线", "name"), ("涨幅", "pct_text"), ("核心逻辑", "logic"), ("核心龙头", "leaders")]
    sub_cols = [("方向", "name"), ("涨幅", "pct_text"), ("逻辑", "logic"), ("代表股", "leaders")]
    adj_cols = [("方向", "name"), ("涨幅", "pct_text"), ("调整原因", "reason")]

    def board_tbl(rows, title):
        if not rows:
            return ""
        body = "".join(
            f'<tr><td>{e(x["name"])}</td><td class="num {cls(x["pct"])}">{pct(x["pct"])}</td>'
            f'<td class="mu num">{x["up_count"]}涨/{x["down_count"]}跌</td>'
            f'<td class="mu">{e(x.get("leader") or "")}</td></tr>' for x in rows)
        inner = f'<tr><th>板块</th><th>涨幅</th><th>内部涨跌</th><th>领涨股</th></tr>{body}'
        return f'<div class="sub-t">{title}</div>{T(inner)}'

    return f'''<section class="sec">
<h2><i></i>三、主线与支线<span class="tag mix">数据 + AI</span></h2>
<div class="sub-t">🔥 今日最强主线</div>
{ai_rows(ai.get("mainlines"), main_cols)}
<div class="sub-t">⚡ 次主线 / 支线</div>
{ai_rows(ai.get("secondlines"), sub_cols)}
<div class="sub-t">📉 调整方向</div>
{ai_rows(ai.get("adjusting"), adj_cols)}
<div class="g2" style="margin-top:16px">
  <div>{board_tbl(sec.get("industry_top", [])[:8], "📊 行业涨幅榜（硬数据）")}</div>
  <div>{board_tbl(sec.get("industry_bottom", [])[:8], "📉 行业跌幅榜（硬数据）")}</div>
</div>
</section>'''


# ---------------- 片段：涨停梯队 ----------------
def frag_limitup(snap, ai):
    p = snap.get("pools") or {}
    zt = (ai or {}).get("zt") or {}
    if not p or p.get("zt_count") is None:
        return '<section class="sec"><h2><i></i>四、涨停梯队分析<span class="tag mix">数据 + AI</span></h2><div class="empty">— 无涨停池数据 —</div></section>'

    ladder = p.get("ladder") or {}
    n3, n2, n1 = len(ladder.get("3plus", [])), len(ladder.get("2", [])), len(ladder.get("1", []))
    notes = zt.get("ladder_notes") or {}

    profile = [
        ("涨停家数（沪深池）", f'<span class="up num">{p["zt_count"]}</span> 家 ｜ 全市场含 ST '
                              f'<span class="up num">{((snap.get("breadth") or {}).get("limit_up"))}</span> 家'),
        ("跌停家数（沪深池）", f'<span class="dn num">{p.get("dt_count")}</span> 家 ｜ 全市场含 ST '
                              f'<span class="dn num">{((snap.get("breadth") or {}).get("limit_down"))}</span> 家'),
        ("炸板家数", f'<span class="num">{p.get("zb_count")}</span> 家'),
        ("封板率", f'<span class="num">{p.get("seal_rate", 0) * 100:.1f}%</span> ｜ 炸板率 '
                 f'<span class="num">{p.get("break_rate", 0) * 100:.1f}%</span>'),
        ("平均炸板次数", f'<span class="num">{p.get("avg_zbc")}</span> 次/涨停股'),
    ]
    prow = "".join(f'<tr><td class="mu">{k}</td><td>{v}</td></tr>' for k, v in profile)

    srow = "".join(
        f'<tr><td class="t1">{e(s["sector"])}</td><td class="num up">{s["count"]}</td>'
        f'<td class="num">{s["max_lbc"]} 板</td>'
        f'<td class="mu">{e("／".join(s["members"][:5]))}</td></tr>'
        for s in p.get("by_sector", [])[:8])

    def rep(key, k=6):
        rows = ladder.get(key, [])[:k]
        return "／".join(f'{r["name"]}({r["lbc"]}板)' for r in rows) if rows else "—"

    ladder_rows = [
        ("3plus", "lt3", "三板以上", n3, rep("3plus"), notes.get("3plus")),
        ("2", "lt2", "二板", n2, rep("2"), notes.get("2")),
        ("1", "lt1", "首板", n1, rep("1", 8), notes.get("1")),
    ]
    lrow = "".join(
        f'<tr><td><span class="ladder-tag {t}">{lab}</span></td><td class="num">≈{n} 只</td>'
        f'<td class="mu">{e(rp)}</td><td>{e(nt or "—")}</td></tr>'
        for _, t, lab, n, rp, nt in ladder_rows)

    logic = zt.get("logic_vs_emotion")
    logic_html = (f'<div class="note"><b>逻辑迷雾 vs 情绪炒作：</b>{e(logic)}</div>' if logic
                  else '<div class="note warn"><b>逻辑迷雾 vs 情绪炒作：</b>待补（ai.zt.logic_vs_emotion）</div>')

    profile_tbl = T(prow)
    sector_tbl = T(f'<tr><th>板块</th><th>涨停数</th><th>最高板</th><th>代表股</th></tr>{srow}')
    ladder_tbl = T(f'<tr><th style="width:12%">梯队</th><th style="width:11%">数量</th>'
                   f'<th style="width:46%">代表股</th><th>判断</th></tr>{lrow}', stack=True)

    return f'''<section class="sec">
<h2><i></i>四、涨停梯队分析<span class="tag mix">数据 + AI</span></h2>
<div class="g2">
  <div>
    <div class="sub-t">涨停整体画像（沪深涨停池，打板口径）</div>
    {profile_tbl}
  </div>
  <div>
    <div class="sub-t">涨停板块分布</div>
    {sector_tbl}
  </div>
</div>
<div class="sub-t">🏆 涨停梯队结构</div>
{ladder_tbl}
{logic_html}
</section>'''


# ---------------- 片段：持仓追踪 ----------------
def frag_holdings(snap, ai):
    hs = snap.get("holdings") or []
    head = '<h2><i></i>五、持仓收益追踪<span class="tag data">硬数据</span></h2>'
    if not hs:
        return (f'<section class="sec">{head}<div class="empty">— 未配置持仓清单：在 '
                f'<code>data/holdings.json</code> 中按 <code>[{{"secid":"0.300145","name":"南方泵业","cost":..}}]</code> '
                f'格式添加即可自动追踪 —</div></section>')
    body = "".join(
        f'<tr><td class="t1">{e(h.get("name"))}</td><td class="num">{h.get("price", "-")}</td>'
        f'<td class="num {cls(h.get("pct"))}">{pct(h.get("pct"))}</td>'
        f'<td class="mu num">{e(h.get("note") or "")}</td></tr>' for h in hs)
    tbl = T(f'<tr><th>标的</th><th>现价</th><th>涨跌幅</th><th>关键信息</th></tr>{body}', stack=True)
    return f'<section class="sec">{head}{tbl}</section>'


# ---------------- 片段：后市展望 ----------------
def frag_outlook(ai):
    ai = ai or {}

    def tbl(rows, cols):
        if not rows:
            return '<div class="empty">— 待补 —</div>'
        head = "".join(f"<th>{c[0]}</th>" for c in cols)
        body = ""
        for r in rows:
            tds = ""
            for i, (_, key) in enumerate(cols):
                v = r.get(key)
                if i == 0:
                    tds += f'<td class="t1">{e(v)}</td>'
                elif key == "expect":
                    tds += f'<td class="num {cls(r.get("expect_cls"))}">{e(v)}</td>'
                else:
                    tds += f"<td>{e(v)}</td>"
            body += f"<tr>{tds}</tr>"
        return T(f"<tr>{head}</tr>{body}", stack=True)

    short_cols = [("方向", "name"), ("预期", "expect"), ("核心逻辑", "logic")]
    mid_cols = [("方向", "name"), ("核心逻辑", "logic")]
    s = ai.get("outlook_short") or []
    m = ai.get("outlook_mid") or []
    return f'''<section class="sec">
<h2><i></i>六、后市展望<span class="tag ai">AI 判断</span></h2>
<div class="sub-t">短期（1 周内）</div>
{tbl(s, short_cols)}
<div class="sub-t">中期（1 个月 ~ 2 个月）</div>
{tbl(m, mid_cols)}
</section>'''


# ---------------- 主流程 ----------------
def render(date=None):
    ensure_dirs()
    if not date:
        files = sorted(glob.glob(os.path.join(HIST_DIR, "*.json")))
        if not files:
            raise SystemExit(
                "没有找到任何数据快照，请先运行采集脚本：\n"
                f"  查找位置 : {HIST_DIR}\n"
                "  采集命令 : python <skill目录>/scripts/fetch_report_data.py")
        date = os.path.basename(files[-1])[:-5]
    snap = json.load(open(os.path.join(HIST_DIR, f"{date}.json"), encoding="utf-8"))
    ai_path = os.path.join(AI_DIR, f"{date}.json")
    ai = json.load(open(ai_path, encoding="utf-8")) if os.path.exists(ai_path) else {}

    d = t2date(date)
    tpl = open(TPL, encoding="utf-8").read()
    srcs = "、".join(sorted({x.get("source", "?") for x in snap.get("indices", [])}))
    b = snap.get("breadth") or {}
    universe = (b.get("up") or 0) + (b.get("down") or 0) + (b.get("flat") or 0)

    tokens = {
        "TITLE": "A股每日复盘报告",
        "SUBTITLE": f'{d.strftime("%Y年%m月%d日")}（{WEEK[d.weekday()]}）收盘复盘',
        "SOURCE_NOTE": f'数据日期 {d.isoformat()} ｜ 统计口径：全市场 {universe} 只个股 ｜ 指数来源 {srcs}',
        "INDEX_CARDS": frag_indices(snap),
        "SEC_NEWS": frag_news(ai),
        "SEC_MARKET": frag_market(snap, ai),
        "SEC_MAINLINE": frag_mainline(snap, ai),
        "SEC_LIMITUP": frag_limitup(snap, ai),
        "SEC_HOLDINGS": frag_holdings(snap, ai),
        "SEC_OUTLOOK": frag_outlook(ai),
        "FOOTER_NOTE": (ai.get("footer_note")
                        or f'📌 本报告数据截至 {d.isoformat()} 收盘；下一交易日收盘后自动更新。'),
        "GENERATED_AT": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "DATA_SOURCES": "东方财富公开行情接口 / 同花顺 / 腾讯行情",
    }
    for k, v in tokens.items():
        tpl = tpl.replace("{{" + k + "}}", v)

    os.makedirs(OUT_DIR, exist_ok=True)   # ensure_dirs 已在 render() 入口调用
    out = os.path.join(OUT_DIR, f"report_{date}.html")
    open(out, "w", encoding="utf-8").write(tpl)
    print(f"[完成] 报告已生成 {out}")
    if not ai:
        print(f"  ! 未找到 AI 归因文件 {ai_path}，AI 模块标注为待补")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYYMMDD")
    ap.add_argument("--show-paths", action="store_true",
                    help="运行环境自检：skill 目录 / 数据根 / Python / 依赖 / 可写性")
    a = ap.parse_args()
    if a.show_paths:
        import paths
        print(paths.describe())
        raise SystemExit(0)
    render(a.date)
