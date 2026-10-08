# A股每日自动复盘报告 skill 

每个交易日收盘后，可自动调用本skill采集行情数据、生成手机适配的 HTML 复盘报告。

数据全部来自**公开免费接口**（东财 / 同花顺 / 腾讯 / 新浪），**不需要任何账号或密钥**。

## 效果

一份报告包含：四大指数行情、涨跌家数与区间分布、全市场成交额（含历史均量对比）、涨停/跌停/炸板池与连板梯队、行业与概念板块榜、消息面催化、主线/支线/调整方向判定、后市展望（短期 + 中期）。

## 快速开始

```bash
# 1) 安装依赖（Python 3.9+）
python -m pip install -r requirements.txt

# 2) 环境自检：打印 Python 版本、依赖状态、skill 目录、数据根
python scripts/fetch_report_data.py --show-paths

# 3) 是否交易日：YES / NO
python scripts/fetch_report_data.py --check-today

# 4) 采集数据 → <数据根>/data/history/<日期>.json
python scripts/fetch_report_data.py

# 5) 撰写 AI 归因 → <数据根>/data/ai/<日期>.json（见 SKILL.md「第 2 步」）

# 6) 渲染报告 → <数据根>/out/report_<日期>.html
python scripts/render_report.py --date <YYYYMMDD>
```

**代码与数据分离**：脚本用 `__file__` 自定位，**与 cwd 无关**；所有运行期产物写到独立的数据根，默认 `~/.ashare-review`，可用环境变量覆盖：

```bash
export ASHARE_REVIEW_HOME=/path/to/your/data    # Windows: set / $env:
```

数据根**不允许**落在 skill 目录内（脚本会报错退出），避免运行期数据混进代码包。

缺少依赖时脚本不会抛裸 traceback，而是直接打印「装什么、装到哪、之后用哪个解释器」。

## 目录结构

```
stock-daily-review/
├── SKILL.md                    # 完整 skill 说明（流程、判定规则、数据源与踩坑记录）
├── config.json                 # 数据源主机池、限速、指数清单、持仓
├── requirements.txt
├── scripts/
│   ├── paths.py                # 路径解析：代码根 / 数据根分离（改路径只动这里）
│   ├── fetch_report_data.py    # 采集层
│   ├── render_report.py        # 渲染层（零第三方依赖）
│   └── backfill_history.py     # 历史数据回填（手动）
└── templates/report.html       # 报告版式（配色 / 布局 / 移动端适配）
```

## 历史数据回填

```bash
python scripts/backfill_history.py --days 10     # 回填最近 10 个交易日
python scripts/backfill_history.py --verify      # 用历史法复算并与当日快照比对
```

产出与当日快照**键集/键序完全一致**（252 个键）。历史不可得的字段按同构保留键位、值置 null。

## 设计要点

- **限速 1.6s + 重试退避 + 多主机降级**：东财会做 IP 级封禁（实测高频请求后 push2/push2his 整域断连约 40 分钟），请勿并行或缩短间隔
- **默认绕过系统代理**（本地代理常污染 requests）；企业网络必须走代理时设 `ASHARE_REVIEW_TRUST_ENV=1`
- **主源 + 兜底多源交叉**：指数、板块、涨跌家数、成交额各有独立兜底链路，脏数据自动丢弃并落本地缓存
- **统计口径**：市场交易数据一律全市场（沪深京，含北交所、含 ST），不做剔除

更多实现细节、数据源对照表与实测踩坑记录见 [`SKILL.md`](SKILL.md)。

## License

[MIT](LICENSE)
