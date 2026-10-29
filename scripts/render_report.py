# -*- coding: utf-8 -*-
"""A股每日复盘 · 报告渲染层

输入（路径以 <数据根> 为基准，见 paths.py）
  <数据根>/data/history/<date>.json   采集层产出的数据快照（硬数据）
  <数据根>/data/ai/<date>.json        AI 归因文字（新闻影响分析/主线逻辑/涨停判断/后市展望）
  <数据根>/data/newhigh/<date>.json   创新高明细（可选，由 fetch_new_high.py 采集）
  <数据根>/data/newhigh/series.json   创新高家数累积序列（可选，逐日累积）
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
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 让 scripts/ 目录可 import（无论从哪个 cwd 启动）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import (AI_DIR, HIST_DIR, JUNK_PATH, NEWHIGH_DIR, OUT_DIR,  # noqa: E402
                   TPL_PATH, WHITELIST_PATH, ensure_dirs)

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


# ---------------- 片段：走势图（近 N 个交易日，Chart.js 折线经 CDN） ----------------
TREND_DAYS = 10
CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
_CDN_EMITTED = False  # 一页多图时，CDN 的 <script src> 只输出一次（见 chart_block）

# 通用折线图块，@@XXX@@ 占位符由 chart_block() 填充。
# 关键：必须等 load（布局定型）再建图。若在文档解析中途建图，首绘会落在未定型的窄容器上，
# 之后 responsive 只更新内部模型、不重绘画布，表现为「折线挤在左侧、坐标轴却铺满全宽」。
_CHART_TMPL = """
<div class="chart-box" id="@@BOX@@"><canvas id="@@CANVAS@@"></canvas></div>
@@CDNSCRIPT@@
<script>
(function(){
  var L = @@LABELS@@, V = @@VALS@@, U = @@UNIT@@;
  var box = document.getElementById('@@BOX@@');
  function fallback(){
    box.innerHTML = '<div class="chart-fb">图表库未能加载（本图需联网引用 CDN）<br>'
      + L.map(function(d,i){ return d + ' ' + V[i] + U; }).join(' ｜ ') + '</div>';
  }
  function init(){
    if (!window.Chart) { fallback(); return; }
    // 在折点上直接标数值（不引 datalabels 插件，自己画）
    var vLabels = { id:'vLabels', afterDatasetsDraw:function(c){
      var ctx = c.ctx, meta = c.getDatasetMeta(0);
      ctx.save();
      ctx.font = '600 10px -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; ctx.fillStyle = '@@COLOR@@';
      meta.data.forEach(function(pt, i){ ctx.fillText(String(V[i]), pt.x, pt.y - 5); });
      ctx.restore();
    }};
    try {
      var chart = new Chart(document.getElementById('@@CANVAS@@'), {
        type: 'line',
        data: { labels: L, datasets: [{
          data: V, borderColor: '@@COLOR@@', borderWidth: 2,
          backgroundColor: '@@FILL@@', fill: true, tension: .32,
          pointRadius: 3, pointHoverRadius: 5,
          pointBackgroundColor: '#fffdf9', pointBorderColor: '@@COLOR@@', pointBorderWidth: 2
        }]},
        options: {
          responsive: true, maintainAspectRatio: false,
          layout: { padding: { top: 20, right: 10, left: 0, bottom: 0 } },
          interaction: { intersect: false, mode: 'index' },
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: function(x){ return @@TIP@@ + x.parsed.y + U; } } }
          },
          scales: {
            y: { @@YOPTS@@
                 grid: { color: '#f4e9d8', drawTicks: false },
                 border: { display: false },
                 ticks: { @@YTICK@@color: '#8b7568', font: { size: 10 }, maxTicksLimit: 5, padding: 6 } },
            x: { grid: { display: false }, border: { color: '#ecd9bd' },
                 ticks: { color: '#8b7568', font: { size: 10 }, autoSkip: false,
                          maxRotation: 0, padding: 4 } }
          },
          animation: false
        },
        plugins: [vLabels]
      });
      // 保险：实测个别环境下首绘会落在未定型的窄布局上，且此后模型更新、位图不再重绘。
      // 这里在下一帧和 120ms 后各强制全量重绘一次。
      requestAnimationFrame(function(){ requestAnimationFrame(function(){ chart.update('none'); }); });
      setTimeout(function(){ chart.update('none'); }, 120);
    } catch (e) { fallback(); }
  }
  if (document.readyState === 'complete') { init(); }
  else { window.addEventListener('load', init); }
})();
</script>"""


def chart_block(canvas_id, labels, values, *, color, fill, unit=" 家", tip="",
                y_opts="beginAtZero: true,", y_tick=""):
    """生成一段自包含折线图（Chart.js 经 CDN）。labels / values 需等长。

    一页可能有多张图，CDN 的 <script src> 只在第一张图处输出一次
    （_CDN_EMITTED 保证），后续图表直接复用已加载的 window.Chart。
    """
    global _CDN_EMITTED
    cdn_script = ""
    if not _CDN_EMITTED:
        cdn_script = f'<script src="{CHARTJS_CDN}"></script>'
        _CDN_EMITTED = True
    return (_CHART_TMPL
            .replace("@@BOX@@", canvas_id + "Box")
            .replace("@@CANVAS@@", canvas_id)
            .replace("@@CDNSCRIPT@@", cdn_script)
            .replace("@@LABELS@@", json.dumps(labels, ensure_ascii=False))
            .replace("@@VALS@@", json.dumps(values))
            .replace("@@UNIT@@", json.dumps(unit, ensure_ascii=False))
            .replace("@@TIP@@", json.dumps(tip, ensure_ascii=False))
            .replace("@@COLOR@@", color)
            .replace("@@FILL@@", fill)
            .replace("@@YOPTS@@", y_opts)
            .replace("@@YTICK@@", y_tick))


def _hist_days(date, n):
    """<= date 的最近 n 个交易日（YYYYMMDD，升序）。"""
    days = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(HIST_DIR, "*.json")))
    return [d for d in days if d.isdigit() and d <= date][-n:]


def _load_trend(date, pick, n=TREND_DAYS):
    """按 pick(snapshot) 逐日取值 → [(YYYYMMDD, value)]，日期升序；取不到的日子跳过。

    只看 <= date 的快照，缺文件的交易日自动跳过（非交易日本就不该有文件）。
    """
    out = []
    for d in _hist_days(date, n):
        try:
            s = json.load(open(os.path.join(HIST_DIR, f"{d}.json"), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        v = pick(s)
        if isinstance(v, (int, float)):
            out.append((d, int(v)))
    return out


def _up_of(snap):
    """全市场（沪深京）上涨家数。"""
    return (snap.get("breadth") or {}).get("up")


def _max_lbc_of(snap):
    """当日涨停池最高连板高度（max lbc）；无涨停池则 None。"""
    zt = (snap.get("pools") or {}).get("zt") or []
    lbcs = [x.get("lbc") for x in zt if isinstance(x.get("lbc"), int)]
    return max(lbcs) if lbcs else None


def _trend_frag(date, loader, cid, title, color, fill, unit, tip, scope,
                y_opts="beginAtZero: true,", y_tick=""):
    """通用走势片段：子标题（含区间） + 折线图。快照不足 2 天时退化为一行提示。"""
    rows = loader(date)
    if len(rows) < 2:
        return (f'<div class="sub-t">{title}</div>'
                '<div class="empty">— 历史快照不足 2 个交易日，暂不绘制走势'
                '（可用 backfill_history.py 回填历史快照）—</div>')
    labels = [f"{d[4:6]}-{d[6:]}" for d, _ in rows]
    vals = [v for _, v in rows]
    head = (f'<div class="sub-t">{title}（近 {len(rows)} 个交易日）'
            f'<span class="scope-note">{scope(min(vals), max(vals))}</span></div>')
    return head + chart_block(cid, labels, vals, color=color, fill=fill,
                              unit=unit, tip=tip, y_opts=y_opts, y_tick=y_tick)


UP_TREND_YMAX = 5400      # 上涨家数走势固定纵轴上界（留出普涨日空间，且跨日报告刻度可比）


def frag_up_trend(date):
    """第二节：「涨跌幅区间分布」之后的走势图 —— 近 N 个交易日上涨家数（含报告当日）。"""
    return _trend_frag(
        date, lambda d: _load_trend(d, _up_of),
        cid="chUpTrend", title="📈 上涨家数走势",
        color="#d32b28", fill="rgba(211,43,40,.07)", unit=" 家", tip=" 上涨 ",
        y_opts=f"beginAtZero: true, max: {UP_TREND_YMAX},",
        scope=lambda lo, hi: f"全市场（沪深京）口径 · 区间 {lo} ~ {hi} 家")


def frag_lbc_trend(date):
    """第四节：「涨停板块分布」之后的走势图 —— 近 N 个交易日最高连板高度（含报告当日）。"""
    return _trend_frag(
        date, lambda d: _load_trend(d, _max_lbc_of),
        cid="chLbcTrend", title="📶 最高连板高度",
        color="#7c4ddb", fill="rgba(124,77,219,.08)", unit=" 板", tip=" 最高连板 ",
        y_opts="beginAtZero: false, grace: '25%',", y_tick="precision: 0,",
        scope=lambda lo, hi: f"当日涨停池最高连板 · 区间 {lo} ~ {hi} 板")


# ---------------- 片段：创新高（同花顺，按日累积） ----------------
NEWHIGH_BLUE = "#2563eb"
NEWHIGH_WINDOW_NOTE = "同花顺技术选股口径 · 实际窗口≈近 20 个交易日 · 全市场（沪深京）"


def _newhigh_today(date):
    """当日创新高明细 JSON；没有则 None。"""
    p = os.path.join(NEWHIGH_DIR, f"{date}.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _newhigh_series(date, n=TREND_DAYS):
    """[(YYYYMMDD, 家数)] —— <= date 的最近 n 个交易日。

    序列由采集层逐日累积（同花顺榜单接口没有历史，回填不了）。
    """
    p = os.path.join(NEWHIGH_DIR, "series.json")
    if not os.path.exists(p):
        return []
    try:
        s = json.load(open(p, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    days = sorted(d for d in s if str(d).isdigit() and str(d) <= date)
    return [(d, int(s[d])) for d in days[-n:] if isinstance(s[d], (int, float))]


def frag_newhigh(snap):
    """第二节末尾：创新高家数曲线 + 当日摘要 + 行业分布。

    数据未采集时**整块不渲染**，老快照出的报告不会因缺数据而变形。
    """
    date = str(snap.get("date") or "")
    today = _newhigh_today(date)
    series = _newhigh_series(date)
    if today is None and not series:
        return ""

    window = (today or {}).get("window") or "创月新高"
    rows = (today or {}).get("rows") or []
    cur = (today or {}).get("count")
    vals = [v for _, v in series]

    # ---- 曲线：不足 2 天只给提示（同花顺无历史，只能逐日累积） ----
    if len(series) >= 2:
        head = (f'<div class="sub-t">🏔 {e(window)}家数走势（近 {len(series)} 个交易日）'
                f'<span class="scope-note">{NEWHIGH_WINDOW_NOTE} · '
                f'区间 {min(vals)} ~ {max(vals)} 家</span></div>')
        chart = chart_block(
            "chNewHigh", [f"{d[4:6]}-{d[6:]}" for d, _ in series], vals,
            color=NEWHIGH_BLUE, fill="rgba(37,99,235,.07)", unit=" 家", tip=" 家 ",
            y_opts="beginAtZero: true, grace: '12%',")   # 家数柱底为 0 有意义；留白给顶点数值标签
    else:
        got = "、".join(f"{d[4:6]}-{d[6:]} {v} 家" for d, v in series) or "暂无"
        head = (f'<div class="sub-t">🏔 {e(window)}家数走势'
                f'<span class="scope-note">{NEWHIGH_WINDOW_NOTE}</span></div>')
        chart = ('<div class="chart-fb">📊 家数曲线积累中（当前 %d 个交易日，至少需 3 天才能看出形态）<br>'
                 '同花顺「创新高」榜单接口不提供历史数据，只能自启用之日起逐日累积。<br>'
                 '已记录：%s</div>' % (len(series), e(got)))

    # ---- 当日摘要（一行） ----
    if cur is None:
        note = ""
    else:
        prev = None
        for d, v in series:
            if d < date:
                prev = v
        b = snap.get("breadth") or {}
        uni = (b.get("up") or 0) + (b.get("down") or 0) + (b.get("flat") or 0)
        bits = [f'当日 <b>{cur} 家</b>']
        if prev is not None:
            bits.append(f'较前一交易日 <span class="{cls(cur - prev)}">{cur - prev:+d} 家</span>')
        if uni:
            bits.append(f"占全市场 {cur / uni * 100:.2f}%")
        if b.get("up"):
            bits.append(f"为上涨家数的 {cur / b['up'] * 100:.0f}%")
        if len(vals) >= 2:
            bits.append(f"近 {len(vals)} 日区间 {min(vals)} ~ {max(vals)} 家")
        warn = ('<span class="mu">（当日有分页抓取失败，家数偏小，已排除在曲线之外）</span>'
                if (today or {}).get("partial") else "")
        note = f'<div class="note"><b>创新高摘要：</b>{" ｜ ".join(bits)}{warn}</div>'

    # ---- 行业分布 ----
    ind_html = _newhigh_industry(rows)

    return head + chart + note + ind_html


def _newhigh_industry(rows, top=8):
    """创新高个股的行业分布（行业来自采集时的东财映射，缺失则退化为一行提示）。"""
    if not rows:
        return ""
    cnt = Counter((r.get("industry") or "") for r in rows)
    mapped = cnt.pop("", 0)
    if not cnt:
        return ('<div class="sub-t">🏭 创新高行业分布</div>'
                '<div class="empty">— 本次未做行业映射（采集时带了 --no-industry '
                '或映射失败），完整名单见 newhigh CSV —</div>')

    tot = sum(cnt.values())
    grp = {}
    for r in rows:
        if r.get("industry"):
            grp.setdefault(r["industry"], []).append(r)

    trs = []
    for ind, n in cnt.most_common(top):
        reps = sorted(grp.get(ind, []), key=lambda r: -(r.get("pct") if r.get("pct") is not None else -99))
        cell = "／".join(f'{e(r.get("name"))}({pct(r.get("pct"))})' for r in reps[:3])
        trs.append(f'<tr><td class="t1">{e(ind)}</td><td class="num">{n}</td>'
                   f'<td class="num mu">{n / tot * 100:.1f}%</td>'
                   f'<td class="mu">{cell}</td></tr>')

    tail = (f'（另有 {mapped} 只未匹配到行业，多为北交所 / 次新股）' if mapped else "")
    return ('<div class="sub-t">🏭 创新高行业分布'
            f'<span class="scope-note">共 {len(cnt)} 个行业 · 行业取自东财{tail}</span></div>'
            + T('<tr><th>行业</th><th>家数</th><th>占比</th><th>代表股（涨跌幅）</th></tr>'
                + "".join(trs)))


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
    trend = frag_up_trend(snap.get("date"))
    newhigh = frag_newhigh(snap)   # 未采集创新高时返回 ""，整块不出现

    return f'''<section class="sec">
<h2><i></i>一、市场交易数据<span class="tag mix">数据 + AI</span></h2>
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
{trend}
{newhigh}
<div class="sub-t">核心成交数据{amt_note}</div>
{amt_tbl}
{note}
</section>'''


# ---------------- 片段：板块轮动矩阵（行=当日排名，列=交易日） ----------------
ROT_DAYS = 10   # 列数：最近 N 个交易日
ROT_ROWS = 10   # 行数：每日 TopN

_LISTS = {}


def _board_lists():
    """(行业白名单 set, 概念黑名单词 list)，读 assets/ 下两份固定清单。

    白名单缺失时降级为「排除 Ⅲ 结尾」，保证矩阵不至于整块空白。
    """
    if not _LISTS:
        try:
            wl = set(json.load(open(WHITELIST_PATH, encoding="utf-8")).get("names") or [])
        except Exception:  # noqa: BLE001
            wl = set()
        junk, seen = [], set()
        try:
            grp = json.load(open(JUNK_PATH, encoding="utf-8")).get("groups") or {}
            for arr in grp.values():
                for w in arr:
                    if w not in seen:
                        seen.add(w)
                        junk.append(w)
        except Exception:  # noqa: BLE001
            junk = ["昨日"]
        _LISTS["wl"], _LISTS["junk"] = wl, junk
    return _LISTS["wl"], _LISTS["junk"]


_CON_SUFFIX = ("概念", "板块", "指数")


def _short_name(name, is_concept):
    """板块名压到 4~5 字 —— 矩阵一格只有 64px 宽。

    行业去掉 Ⅱ/Ⅲ 后缀（白名单同层只有一个，不会歧义）；
    概念去掉「概念/板块/指数」尾巴（「英伟达概念」→「英伟达」）。
    """
    n = (name or "").strip()
    if is_concept:
        for suf in _CON_SUFFIX:
            if n.endswith(suf) and len(n) > len(suf) + 1:
                return n[: -len(suf)]
        return n
    return n[:-1] if (n and n[-1] in "ⅡⅢ") else n


def _rotation(date, kind):
    """逐日取该口径下的 TopN → [(YYYYMMDD, rows|None)]，日期升序。

    历史快照里 15 天只有前 100 名（采集分页 bug 已修，旧数据未重采），
    但 Top10 必然落在前 100 名内，回填不受影响。
    """
    wl, junk = _board_lists()
    is_con = kind == "concept"
    out = []
    for d in _hist_days(date, ROT_DAYS):
        try:
            snap = json.load(open(os.path.join(HIST_DIR, f"{d}.json"), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            out.append((d, None))
            continue
        rows = (snap.get("sectors") or {}).get(kind) or []
        if is_con:
            rows = [x for x in rows if x.get("name") and not any(k in x["name"] for k in junk)]
        elif wl:
            rows = [x for x in rows if x.get("name") in wl]
        else:  # 白名单缺失：退回「东财三级板块一律以 Ⅲ 结尾」的经验规则
            rows = [x for x in rows if x.get("name") and not x["name"].endswith("Ⅲ")]
        rows = [x for x in rows if isinstance(x.get("pct"), (int, float))]
        rows.sort(key=lambda x: -x["pct"])
        out.append((d, rows[:ROT_ROWS]))
    return out


def _rot_cell(x, is_con):
    if not x:
        return '<td class="c em">—</td>'
    p = x["pct"]
    tone = "u2" if p >= 2.5 else "u1" if p > 0 else "d1" if p > -1 else "d2"
    return (f'<td class="c {tone}" title="{e(x.get("name"))} {pct(p)}">'
            f'<span class="n">{e(_short_name(x.get("name"), is_con))}</span>'
            f'<span class="p {cls(p)}">{pct(p, 1)}</span></td>')


def _rot_matrix(cols, is_con):
    head = ['<tr><th class="rk">#</th>']
    for i, (d, _) in enumerate(cols):
        cur = ' class="cur"' if i == len(cols) - 1 else ""
        head.append(f'<th{cur}>{d[4:6]}-{d[6:]}</th>')
    head.append("</tr>")
    body = []
    for r in range(ROT_ROWS):
        tds = [f'<td class="rk">{r + 1}</td>']
        for _, rows in cols:
            tds.append(_rot_cell(rows[r] if rows and r < len(rows) else None, is_con))
        body.append(f'<tr>{"".join(tds)}</tr>')
    # 列宽由 <col> 定死（见 report.html 的 .rot col 规则）：table-layout:fixed 下
    # 单元格 width 只是建议值，会被压成一条缝（实测 10 列压到 39px/列）；
    # 表格本身 width:auto，宽度即各列之和，桌面端靠媒体查询把列加宽填满
    cg = ('<colgroup><col class="rk">'
          + '<col class="c">' * len(cols) + "</colgroup>")
    return ('<div class="tw rot-tw"><table class="rot">'
            + cg + "".join(head) + "".join(body) + "</table></div>")


def _rot_summary(cols, is_con):
    """在榜次数 + 当前连续天数 —— 轮动真正要看的是「谁留下来了」，不是单日涨幅。"""
    if not cols:
        return ""
    freq, span = {}, {}
    for _, rows in cols:
        for x in rows or []:
            k = _short_name(x.get("name"), is_con)
            freq[k] = freq.get(k, 0) + 1
    for x in (cols[-1][1] or []):  # 只对最新一日在榜的板块算连续天数
        k = _short_name(x.get("name"), is_con)
        n = 0
        for _, rows in reversed(cols):
            if k in {_short_name(y.get("name"), is_con) for y in (rows or [])}:
                n += 1
            else:
                break
        span[k] = n
    if not freq:
        return ""
    top = sorted(freq.items(), key=lambda kv: (-kv[1], -span.get(kv[0], 0)))[:4]
    chips = "".join(
        f'<span class="chip n">{e(k)} {v} 次'
        + (f' · 连 {span[k]} 日' if span.get(k, 0) >= 2 else "")
        + "</span>" for k, v in top)
    return (f'<div class="rot-sum">近 {len([c for c in cols if c[1]])} 日在榜最多：'
            f'{chips}</div>')


_ROT_JS = """
<script>
(function(){
  function toEnd(){ document.querySelectorAll('.rot-tw').forEach(function(b){ b.scrollLeft = b.scrollWidth; }); }
  function boot(){
    toEnd();
    document.querySelectorAll('input[name=rotTab]').forEach(function(r){
      r.addEventListener('change', function(){ setTimeout(toEnd, 0); });
    });
  }
  if (document.readyState === 'complete') { boot(); }
  else { window.addEventListener('load', function(){ boot(); setTimeout(toEnd, 150); }); }
})();
</script>"""


def frag_rotation(date):
    """第二节：板块轮动矩阵 + 口径说明。行业 / 概念两个 tab（只有一侧有数据时退化单块）。"""
    ind = _rotation(date, "industry")
    con = _rotation(date, "concept")
    has_ind = any(r for _, r in ind)
    has_con = any(r for _, r in con)
    if not has_ind and not has_con:
        return ""
    head = '<div class="sub-t">🔄 板块轮动</div>'

    # 早期快照没采概念（2026-09 那批），此时不显示空 tab，直接给单块
    if not (has_ind and has_con):
        body = (_rot_matrix(ind, False) + _rot_summary(ind, False) if has_ind
                else _rot_matrix(con, True) + _rot_summary(con, True))
        return f'{head}\n{body}\n{_ROT_JS}'

    def pane(cols, is_con, cls):
        return f'<div class="pane {cls}">{_rot_matrix(cols, is_con)}{_rot_summary(cols, is_con)}</div>'

    return f'''{head}
<div class="tabs">
<input type="radio" name="rotTab" id="rotTabInd" checked>
<input type="radio" name="rotTab" id="rotTabCon">
<div class="tab-bar">
  <label for="rotTabInd">行业</label>
  <label for="rotTabCon">概念</label>
</div>
{pane(ind, False, "p-ind")}
{pane(con, True, "p-con")}
</div>
{_ROT_JS}'''


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

    def board_tbl(rows, title, hint=""):
        if not rows:
            return f'<div class="sub-t">{title}</div><div class="empty">{hint}</div>' if hint else ""
        body = "".join(
            f'<tr><td>{e(x["name"])}</td><td class="num {cls(x["pct"])}">{pct(x["pct"])}</td>'
            f'<td class="mu num">{x["up_count"]}涨/{x["down_count"]}跌</td>'
            f'<td class="mu">{e(x.get("leader") or "")}</td></tr>' for x in rows)
        inner = f'<tr><th>板块</th><th>涨幅</th><th>内部涨跌</th><th>领涨股</th></tr>{body}'
        return f'<div class="sub-t">{title}</div>{T(inner)}'

    # 板块样本完整性：采集层分页拿全才给跌幅榜；旧快照无 meta 时按条数兜底
    # （全量日 496 个，被单页截断的日子只有 100 个）
    ind_meta = (sec.get("meta") or {}).get("industry") or {}
    ind_fetched = ind_meta.get("fetched") or len(sec.get("industry") or [])
    ind_complete = ind_meta.get("complete")
    if ind_complete is None:
        ind_complete = ind_fetched >= 300
    bottom_hint = ""
    if not ind_complete:
        bottom_hint = (f"板块样本不全（仅取到涨幅前 {ind_fetched} 名），"
                       f"跌幅榜已隐藏 —— 避免把涨幅榜中段误当跌幅")

    # 板块轮动矩阵：历史快照不足时退回原来的一张涨幅榜
    rot = frag_rotation(snap.get("date") or "")
    top_fallback = "" if rot else board_tbl(sec.get("industry_top", [])[:8], "📊 行业涨幅榜（硬数据）")

    return f'''<section class="sec">
<h2><i></i>二、主线与支线<span class="tag mix">数据 + AI</span></h2>
<div class="sub-t">🔥 今日最强主线</div>
{ai_rows(ai.get("mainlines"), main_cols)}
<div class="sub-t">⚡ 次主线 / 支线</div>
{ai_rows(ai.get("secondlines"), sub_cols)}
{rot}{top_fallback}
<div class="sub-t">📉 调整方向</div>
{ai_rows(ai.get("adjusting"), adj_cols)}
<div style="margin-top:16px">
  <div>{board_tbl((sec.get("industry_bottom", [])[:8] if ind_complete else []), "📉 行业跌幅榜（硬数据）", bottom_hint or "— 暂无跌幅板块 —")}</div>
</div>
</section>'''


# ---------------- 片段：涨停梯队 ----------------
def frag_limitup(snap, ai):
    p = snap.get("pools") or {}
    zt = (ai or {}).get("zt") or {}
    if not p or p.get("zt_count") is None:
        return '<section class="sec"><h2><i></i>三、涨停梯队分析<span class="tag mix">数据 + AI</span></h2><div class="empty">— 无涨停池数据 —</div></section>'

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
    lbc_trend = frag_lbc_trend(snap.get("date"))

    return f'''<section class="sec">
<h2><i></i>三、涨停梯队分析<span class="tag mix">数据 + AI</span></h2>
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
{lbc_trend}
<div class="sub-t">🏆 涨停梯队结构</div>
{ladder_tbl}
{logic_html}
</section>'''


# ---------------- 片段：持仓追踪 ----------------
def frag_holdings(snap, ai):
    hs = snap.get("holdings") or []
    head = '<h2><i></i>四、持仓收益追踪<span class="tag data">硬数据</span></h2>'
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
<h2><i></i>五、后市展望<span class="tag ai">AI 判断</span></h2>
<div class="sub-t">短期（1 周内）</div>
{tbl(s, short_cols)}
<div class="sub-t">中期（1 个月 ~ 2 个月）</div>
{tbl(m, mid_cols)}
</section>'''


# ---------------- 主流程 ----------------
def render(date=None):
    ensure_dirs()
    global _CDN_EMITTED
    _CDN_EMITTED = False
    if not date:
        files = sorted(glob.glob(os.path.join(HIST_DIR, "*.json")))
        if not files:
            raise SystemExit(
                "没有找到任何数据快照，请先运行采集脚本：\n"
                f"  查找位置 : {HIST_DIR}\n"
                "  采集命令 : python <skill目录>/scripts/fetch_report_data.py")
        date = os.path.basename(files[-1])[:-5]
    snap_path = os.path.join(HIST_DIR, f"{date}.json")
    if not os.path.exists(snap_path):
        raise SystemExit(
            f"[缺少快照] 没有 {date} 的数据快照。\n"
            f"  查找位置 : {HIST_DIR}\n"
            f"  当天采集 : python <skill目录>/scripts/fetch_report_data.py --date {date}\n"
            f"  回填历史 : python <skill目录>/scripts/backfill_history.py --days 10")
    snap = json.load(open(snap_path, encoding="utf-8"))
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
