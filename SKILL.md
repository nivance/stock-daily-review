---
name: stock-daily-review
description: A股每日自动复盘报告。触发词：A股复盘、每日复盘、复盘报告、生成今日复盘、stock review、ashare review。流程：检测交易日 → 采集行情数据（限速+多源降级）→ AI 归因（新闻影响/主线支线/涨停梯队/后市展望）→ 渲染浅色 HTML 报告。数据来自公开免费接口，无需任何账号或密钥。
agent_created: true
---

# A股每日复盘报告

每个交易日收盘后生成一份手机适配版式的 A 股复盘报告。

**设计原则：代码与数据分离。** skill 目录只放只读代码，可整体打包/分发；所有运行期产物（快照、缓存、报告）写到独立的数据根，不进 skill 目录。

## 目录结构

**skill 目录**：

```
stock-daily-review/
├── SKILL.md
├── config.json                    # 数据源主机池、限速参数、指数清单、持仓
├── requirements.txt               # Python 依赖
├── scripts/
│   ├── paths.py                   # 路径解析：代码根 / 数据根分离（改路径只动这里）
│   ├── fetch_report_data.py       # 采集层（每日定时任务调用）
│   ├── render_report.py           # 渲染层
│   └── backfill_history.py        # 历史数据回填（手动执行）
└── templates/report.html          # 报告版式（配色/布局，含移动端适配）
```

**数据根**：

```
<数据根>/
├── data/
│   ├── history/<YYYYMMDD>.json    # 每日数据快照（采集层产出）
│   ├── ai/<YYYYMMDD>.json         # AI 归因文字（本 skill 产出）
│   ├── cache/                     # 成交额序列、逐股日K缓存（自动积累）
│   └── holdings.json              # 持仓清单（可选，用户自填）
└── out/report_<YYYYMMDD>.html     # 最终报告
```

## 数据根怎么定

优先级（见 `scripts/paths.py`）：

1. 环境变量 **`STOCK_REVIEW_HOME`** —— 指向任意可写目录
2. 默认 **`~/.stock-review`**

数据根**不允许**落在 skill 目录内（脚本会直接报错退出），避免运行期数据混进 skill 包。

查看当前解析结果：

```bash
python scripts/fetch_report_data.py --show-paths
```

例：把数据放到 `D:/stock-data`（Linux/macOS 同理，用 `export`）：

```bash
# Windows PowerShell:  $env:STOCK_REVIEW_HOME = "D:/stock-data"
# Windows CMD:         set STOCK_REVIEW_HOME=D:/stock-data
export STOCK_REVIEW_HOME=D:/stock-data
```

## 环境准备

- Python **3.9+**，跨平台（Windows / Linux / macOS），**不需要任何账号或密钥**
- 网络：需能访问东财 / 同花顺 / 腾讯 / 新浪的公开行情接口。**默认绕过系统代理**（本地代理常污染请求）；企业网络必须走代理时设 `STOCK_REVIEW_TRUST_ENV=1`
- Windows 建议先 `set PYTHONIOENCODING=utf-8`（脚本内部已做 stdout 重编码，加一层更稳）

### 安装依赖（二选一）

```bash
# A) 装进当前解释器 —— 最省事
python -m pip install -r <skill目录>/requirements.txt

# B) 装进独立虚拟环境 —— 推荐，不动系统 Python
python -m venv ~/.stock-review/.venv
~/.stock-review/.venv/bin/pip install -r <skill目录>/requirements.txt   # Windows 用 .venv\Scripts\pip.exe
```

这些命令不用记：**缺依赖时脚本不会抛裸 traceback，而是把"该装什么、装到哪、之后用哪个解释器"直接打印出来**（退出码 2）。

### 先做环境自检

```bash
python <skill目录>/scripts/fetch_report_data.py --show-paths
```

一次性打印：Python 版本 / 解释器路径 / 五个依赖的安装状态 / skill 目录 / 数据根及其可写性。
**任何"跑不起来"的情况，先看这个输出。**

## 单独运行（首次使用 / 不接定时任务）

`<skill目录>` = 本 skill 的安装路径（如 `~/.workbuddy/skills/stock-daily-review`），`$PY` = 装了依赖的解释器。
脚本用 `__file__` 自定位，**与 cwd 无关**，用绝对路径 + 任意解释器调用都能跑。

```bash
SKILL_DIR="<skill目录>"
PY="python"          # 或你的虚拟环境 Python

$PY "$SKILL_DIR/scripts/fetch_report_data.py" --show-paths     # 1) 环境自检
$PY "$SKILL_DIR/scripts/fetch_report_data.py" --check-today    # 2) 是否交易日：YES / NO
$PY "$SKILL_DIR/scripts/fetch_report_data.py"                  # 3) 采集 → <数据根>/data/history/<日期>.json
# 4) 由 AI 完成：读快照，撰写 <数据根>/data/ai/<日期>.json（见下方「第 2 步」）
$PY "$SKILL_DIR/scripts/render_report.py" --date <日期>         # 5) 渲染 → <数据根>/out/report_<日期>.html
```

- 数据根默认 `~/.stock-review`，**首次运行自动创建**；空数据根首次采集约 2 分钟（联网重建 425 天成交额历史等缓存），之后复用缓存、次日运行约 1.5 分钟。
- 只想看数据、不渲染报告：跑到第 3 步即可（`data/ai/*.json` 缺失时报告仍可渲染，AI 模块显示「待补」）。

## 执行流程（严格按顺序）

下文命令用 `$SKILL_DIR` / `$PY` 表示（同上一节）；若已 `cd` 到 skill 目录，也可省略前缀直接 `python scripts/xxx.py`。

### 第 0 步：判断今天是否交易日

```bash
$PY "$SKILL_DIR/scripts/fetch_report_data.py" --check-today
```

输出 `NO`（周末/节假日）→ 直接结束，**不生成报告、不发通知**。
输出 `YES` → 继续。

### 第 1 步：采集数据

```bash
$PY "$SKILL_DIR/scripts/fetch_report_data.py"      # 自动取最新交易日
```

产出 `<数据根>/data/history/<date>.json`，并在 stdout 打印摘要（指数/涨跌家数/成交额/涨停梯队/板块榜）。
采集层已内置：绕过系统代理、全局限速 1.6s、重试退避、多主机降级。**不要并行、不要缩短间隔、不要短时间内反复重跑**——东财会做 IP 级封禁（实测：高频请求后 push2/push2his 整域断连，约 40 分钟后解封）。

### 第 2 步：撰写 AI 归因（本 skill 的核心工作）

读取快照 JSON，**基于其中的真实数据**撰写 `<数据根>/data/ai/<date>.json`。禁止编造快照里没有的数字；新闻影响分析只基于当日实际新闻，不得虚构事件。

Schema（所有键均可选，缺省时报告对应模块标注"待补"）：

```jsonc
{
  "news": { "label": "消息面与催化（日期）", "items": [
    { "tag": "利好|利空|中性", "title": "事件", "text": "影响分析" } ] },
  "market_commentary": "行情惯性点评：成交额相对各均量的位置、涨跌比、封板炸板、指数与板块的结构特征、资金高低切方向",
  "mainlines":    [ { "rank": 1, "name": "1. xxx", "pct_text": "+2.73%", "pct_val": 2.73, "logic": "…", "leaders": "…" } ],
  "secondlines":  [ { "name": "xxx", "pct_text": "…", "pct_val": 0, "logic": "…", "leaders": "…" } ],
  "adjusting":    [ { "name": "xxx", "pct_text": "-4.01%", "pct_val": -4.01, "reason": "…" } ],
  "zt": {
    "ladder_notes": { "3plus": "…", "2": "…", "1": "…" },
    "logic_vs_emotion": "逻辑迷雾 vs 情绪炒作判断"
  },
  "outlook_short": [ { "name": "方向", "expect": "预期", "expect_cls": 1, "logic": "…" } ],
  "outlook_mid":   [ { "name": "方向", "logic": "…" } ],
  "footer_note": "页脚说明"
}
```

`expect_cls` / `pct_val`：正数渲染红色（涨）、负数绿色（跌）。

### 第 3 步：主线 / 支线判定规则（写归因时必须遵守）

**主线 ≠ 涨幅最大，主线 = 合力最强。** 四个维度：

1. **广度（最重要）**：板块内部上涨家数占比 >70%、多个细分方向共振才算；只有龙头独舞的是"妖股行情"。用快照里 `sectors.industry_top/concept_top` 的 `up_count/down_count` 和涨停板块分布交叉验证。
2. **梯队完整度**：首板→二板→三板以上是否成链；封板率高、炸板率低的方向才是资金真认可（用 `pools.seal_rate/break_rate`）。
3. **资金体量与连续性**：板块成交额是否放大、是否连续强于大盘、是否增量资金（缩量日看成交额分布迁移）。
4. **逻辑硬度**：政策/产业/业绩 > 事件驱动 > 纯情绪。逻辑栏只能写出"超跌反弹/事件驱动/主题炒作"的，一律归支线。

评分口径：**主线** = 涨幅 Top5 且 >3% ＋ 广度 >70% ＋ 涨停 ≥5 且有 ≥2 连板 ＋ 成交额放大 ≥30% ＋ 有明确催化，**同时满足 ≥4 条**；满足 1–2 条为支线；跌幅居前＋内部广度崩坏＋资金净流出为调整方向。

动态演化：支线升级主线 = 连续 2 日强于大盘 + 出现 2 板以上连板 + 成交额连续放大；主线退潮 = 龙头断板 + 炸板率上升 + 从领涨变滞涨 + 板块净流出。

### 第 4 步：渲染

```bash
$PY "$SKILL_DIR/scripts/render_report.py" --date <YYYYMMDD>
```

产出 `<数据根>/out/report_<YYYYMMDD>.html`。

### 第 5 步：交付

把报告 HTML 路径告知用户并简要总结当日结论（3~5 条）。若配置了飞书 webhook，可推送报告链接与摘要。

## 历史数据回填（手动，非每日流程）

要把更早的交易日补成与当日快照**完全同构**的数据文件：

```bash
$PY "$SKILL_DIR/scripts/backfill_history.py" --days 10            # 回填最近 10 个交易日
$PY "$SKILL_DIR/scripts/backfill_history.py" --days 30 --force    # 覆盖已存在的日期
$PY "$SKILL_DIR/scripts/backfill_history.py" --verify             # 只校验：用历史法复算 20261008 与现快照比对
```

- 产出 `<数据根>/data/history/<YYYYMMDD>.json`，与当日快照**键集/键序完全一致**（252 个键）
- 已存在的日期文件默认跳过，不加 `--force` 不会覆盖当天采集的真值
- 相关开关：`--no-breadth`（跳过涨跌家数）、`--no-sectors`（跳过板块）、`--refresh-daily`（强制重抓逐股日K缓存）
- 首次运行约 4~5 分钟（逐股日K 8 并发，约 5900 只）；结果缓存于 `data/cache/daily_series_cache.json`，同日重复运行可秒级复用
- **历史不可得的字段**按同构保留键位、值置 null：板块 `up_count/down_count/leader/turnover/breadth`（实时字段）、`news`（无历史快讯接口）、`breadth.cross_check`（对账用交叉校验值）
- 精度：以 10-08 当日真值为基准校验，total/平盘/涨停/跌停/涨跌比全部一致，涨跌家数差 1 只；差异来源是涨跌幅落在 ±0.12%（一分钱临界区）的股票，属数据源间固有精度差

## 数据源与降级（已内置，故障时按此排查）

| 数据 | 主源 | 兜底 |
|---|---|---|
| 指数行情 | 东财 push2 | 腾讯 qt.gtimg.cn |
| 行业/概念板块 | 东财 push2（主域被禁自动切 push2test 镜像） | — |
| 涨停/跌停/炸板池 | 东财 push2ex | — |
| 涨跌家数/区间分布 | **新浪全市场逐股统计**（akshare `stock_zh_a_spot`，口径透明） | 东财 ZDFenBu |
| 成交额历史（沪深段） | **同花顺** `d.10jqka.com.cn`（hs_1A0001/hs_399001 年度文件，非东财、抗封） | 东财 push2his |
| 成交额历史（北交所段） | 东财北证50 指数 `0.899050` K线成交额（逐日累积 + push2his 补齐） | push2test（仅兜底，历史段为占位假数据，有同值检测） |
| 财经快讯 | akshare `stock_info_global_em`（仅当日实时，无法回溯历史） | — |
| 历史日K（回填用） | 新浪 `CN_MarketData.getKLineData`（沪深京全覆盖 + 指数） | 腾讯 fqkline → 东财 push2test |
| 历史板块K线（回填用） | 东财 push2test / push2his | — |
| 全市场代码表（回填用） | 东财 clist（1 次请求拿全沪深京） | akshare `stock_zh_a_spot` |

统计口径（用户指定：**市场交易数据一律全市场，不剔除**）：
- **涨跌家数 = 全市场（沪深京，含北交所、含 ST）**。快照另存沪深两市口径（与同花顺/乐咕一致，如 2026-09-30：全市场 2566/2824 vs 沪深 2345/2713）与东财 ZDFenBu 交叉校验值，便于对账。
- **涨停/跌停 = 收盘封板价自算（含 ST、含北交所 30% 档）**，报告同时标注沪深涨停池口径（52，剔 ST）。
- **成交额当日 = 全市场（沪+深+北交所）**；历史均值：北交所历史段 ≥120 天且与沪深序列对齐时用全市场口径，否则用沪深两市口径并在报告标注（北交所日均占比 <1%，对比值影响可忽略）。
- **主板 ST 股涨跌幅限制已是 10% 而非 5%**（2026-09-30 实测 *ST 涨停于 9.98%），不要按 5% 判 ST 封板。
- 新股上市首日无涨跌幅限制（名称带 N、涨幅可超 100%），须排除在封板判定外。
- 涨跌幅区间分布桶：负向分桶是 `-{min(int(-pct)+1, 10)}`（-0.5% 归 "-1%~0" 桶），否则会出现 "-0" 桶丢数据。

已知坑（实测踩过，勿重复踩）：
- **同花顺年度文件的"当日行"可能尚未结算完整**：实测 2026-10-08 年度文件只给 8,301 亿，而真实沪深成交额为 16,822 亿（仅 49%）。因此**当日成交额一律以实时口径（东财指数）为准**，脚本会自动纠正残缺值并回写缓存 —— 日志里出现"当日…按实时口径修正"属正常行为，不是报错。
- **东财板块接口的涨跌家数字段可能是字符串**：概念板块（`fs=m:90+t:3`）里**部分板块的 `f104/f105` 返回 `"-"`**（空数据板块），旧代码直接 `up / (up + dn)` 会抛 `TypeError: unsupported operand type(s) for /: 'str' and 'str'`，**导致整个概念板块榜被吞掉**（只留一行 `概念板块失败 TypeError` 警告，报告与主线判定双双失去概念维度）。行业板块（`t:2`）通常返回 int，所以只在概念板块上暴露。脚本已在 `_num()` 里统一兜底：转不动按 0 计、整数值保持 int（避免渲染出 `4.0涨`）。实测 2026-10-08 曾因此整榜丢失。
- `push2test` 的 **K 线接口返回残缺假数据**：老日期成交额只有几亿，且会**把成段历史填成同一常数**（实测 6~9 月北证50 全被填成 16.2 亿）——脚本已内置"同值重复 >5 次即整段丢弃"的检测；它只能用于板块/实时类接口。
- **腾讯 `fqkline` 日K 密集请求后会返回 HTTP 501 + 反爬页**（触发临时限流），排查时应改用新浪源。
- 东财 `ZDFenBu` 涨跌分布接口**忽略 date 参数、永远返回当天**，不可用于历史回填（假历史）。
- 成交额序列有下限校验（沪深 1000 亿 / 北交所 20 亿），脏数据自动丢弃并落本地缓存；沪深缓存 ≥200 天且 7 天内更新过就不再联网。
- 东财快讯只能取实时，**回测历史日期时新闻模块会是当天新闻**，属正常现象。
- 本地/企业代理（如 `127.0.0.1:xxxx`）会污染 requests，脚本默认 `trust_env=False` 绕过；企业网络必须走代理时设 `STOCK_REVIEW_TRUST_ENV=1`。

## 持仓追踪（可选）

在 `<数据根>/data/holdings.json` 写入：
```json
[{"secid": "0.300145", "name": "南方泵业", "note": "液冷泵"}]
```
`secid` 规则：沪市 `6xxxxx` 用 `1.6xxxxx`，深市/创业板 `0/3` 开头用 `0.xxxxxx`。留空数组则该模块显示配置说明。
