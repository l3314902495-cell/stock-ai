# Stock AI — AI 美股 / 加密货币分析工具

让 AI 自己调用数据工具来分析任意美股、ETF、指数或加密货币，支持**短线**（技术面 + 交易计划）和**长线**（基本面 + 估值）两种模式。数据源全部免费，AI 提供方可以自由切换（Claude 官方 / 中转站 / DeepSeek / 通义 / 任何 OpenAI 兼容接口）。

```
你: stockai analyze NVDA -m short
         │
         ▼
┌──────────────┐  按需调用工具（并行）  ┌───────────────────────────┐
│  AI（可切换） │ ─────────────────────► │ 12 个数据/计算工具         │
│  推理 + 写报告 │ ◄───────────────────── │ 指标、估值、DCF 全部代码算 │
└──────────────┘     浓缩后的结构化数据  └─────────────┬─────────────┘
                                                      │ SQLite 缓存
                       yfinance · SEC EDGAR · OKX(ccxt) · CoinGecko · DefiLlama · 恐惧贪婪指数
```

**设计原则**
- **AI 只负责推理，数字全部由代码计算**：RSI、支撑压力、估值分位、DCF、盈亏比都在 Python 里算好，AI 不做算术，减少胡编数字
- **工具返回浓缩数据**：不把几千根 K 线塞给 AI，而是给摘要统计 + 最近几根，省 token 也更容易抓重点
- **每个数字带来源和时间**；缺数据时工具返回 error，提示词要求 AI 明说"未获取到"
- **每次结论都记录下来**（结构化 JSON），之后可以回看 AI 判断准不准

---

## 安装

需要 Python 3.10+。

```bash
git clone <你的仓库地址> stock-ai
cd stock-ai
python -m venv .venv
```

```bash
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

```bash
pip install -e .
```

## 配置

```bash
cp config.example.yaml config.yaml
```

```bash
cp .env.example .env
```

1. 在 `.env` 里填写你要用的 API Key
2. 在 `config.yaml` 里设置 `llm.default` 和各个 provider（官方 API / 中转站 / OpenAI 兼容）
3. **在中国大陆**：Yahoo、OKX、CoinGecko、SEC 一般需要代理，在 `network.proxy` 里填写，例如 `http://127.0.0.1:7890`
4. 把 `data.sec_user_agent` 改成你自己的名字和邮箱（SEC 的要求）

检查配置：

```bash
stockai providers
```

先不调用 AI，确认数据通畅：

```bash
stockai quote NVDA BTC SOL 苹果
```

## 使用

### 命令行

```bash
stockai analyze NVDA -m short              # 短线：多周期技术面 + 期权 + 交易计划
stockai analyze AAPL -m long               # 长线：10 年财报 + 估值分位 + DCF + 同行
stockai analyze BTC                        # 默认 both：长线判断 + 短线时机
stockai analyze SOL -m short -p deepseek   # 指定用哪个 AI
stockai analyze TSLA -q "财报前要不要减仓"   # 附带你关心的问题
```

```bash
stockai chat                 # 多轮对话，AI 按需查数据，可以追问
stockai review               # 回看历史分析：至今涨跌、短线计划是先到止损还是先到目标
stockai show 12              # 查看第 12 条历史报告
stockai data technicals NVDA # 直接看某个工具的原始输出（调试用，不花 AI 的钱）
stockai data market_context crypto
stockai cache-clear          # 清缓存
```

报告默认保存在 `./reports/`，日志在 `~/.stock-ai/journal.db`。

### 网页界面

```bash
stockai web
```

打开 http://127.0.0.1:8765：K 线图（MA20/50/200 + 代码计算的支撑压力线，多周期切换）、实时显示 AI 调用了哪些工具、Markdown 报告、历史记录（点击可看至今表现）。图表库已内置，不依赖 CDN。

### 标的输入规则

| 输入 | 解析为 |
|---|---|
| `NVDA` `aapl` `SPY` `^VIX` `BRK-B` | 美股 / ETF / 指数 |
| `BTC` `eth` `sol` `pepe` | 加密货币（OKX 的 XXX/USDT） |
| `苹果` `英伟达` `比特币` `纳指` | 常用中文名 |
| `palantir` `coinbase` | 英文名搜索 |
| `stock:COIN` / `crypto:XXX` | 代码冲突时强制指定类型 |

## AI 能调用的工具

| 工具 | 美股 | 加密 |
|---|---|---|
| `resolve_symbol` | 名称 → 标准代码 | 同左 |
| `get_quote` | 价格、52 周区间、量比、盘前盘后 | 价格、24h 区间、成交额、市值 |
| `get_technicals` | 15m/1h/4h/1d/1wk 多周期：均线、RSI、MACD、ADX、布林、ATR、OBV、支撑压力（含触及次数）、成交密集区、多周期共振；收益/回撤/波动率/Beta、相对 SPY 和行业 ETF 的超额收益 | 同左，相对 BTC |
| `get_fundamentals` | 估值、利润率、增长、负债、空头比例、分析师目标价、5 年年报 + 6 季财务、Piotroski / Altman 分数 | 市值/FDV/流通率/ATH、板块、DefiLlama 的 TVL/手续费/协议收入 |
| `get_long_term_financials` | SEC 官方 10 年以上财报、3/5/10 年复合增速、**历史 PE 及当前分位**（EPS 已按拆股调整） | — |
| `get_derivatives` | 期权：ATM 隐含波动率、隐含波动幅度、Put/Call、最大痛点、大额持仓行权价 | 永续：资金费率（当前/7日/30日）、持仓量变化、多空比、主动买卖比、盘口深度 |
| `get_news` | Yahoo 新闻 | 同左 |
| `get_events` | 财报日、EPS 预期、近 4 次超预期、评级变动、内部人买卖、机构持仓 | — |
| `get_peers` | 同行业估值/盈利对比 | 与 BTC/ETH/SOL 等对比 |
| `get_market_context` | 指数、VIX、美债、美元、黄金、11 个行业 ETF 强弱 | BTC/ETH、恐惧贪婪、BTC 市占率、BTC 资金费率 |
| `run_dcf` | 两阶段 DCF 多情景 + **反向 DCF**（当前股价隐含的增速） | — |
| `trade_plan` | 止损距离（% / ATR 倍数）、各目标 R 倍数、按风险比例计算仓位 | 同左 |

## AI 提供方说明

- **Claude 官方 API**：默认开启提示缓存（多轮工具调用能省大量输入 token）、`effort` 档位、服务端拒答回退（被安全策略拒答时自动换模型重试）
- **Anthropic 格式中转站**：设置 `base_url` 即可；如果中转站不支持缓存、effort 这些参数，程序收到 400 后会自动去掉重试，并提示你在配置里关闭
- **OpenAI 兼容**：DeepSeek、通义、Kimi、智谱、OpenAI 格式中转、本地 Ollama 都可以，前提是模型支持 function calling。小模型的工具调用和分析质量会明显差一些

成本参考：一次完整分析大约调用 6~12 个工具，输入 3~8 万 token（其中大部分能命中缓存）、输出 3~6 千 token。

## 免费数据源的限制

- **yfinance** 是非官方接口，Yahoo 改版时可能失效：遇到问题先 `pip install -U yfinance`。分钟线只能取最近 7 天，15 分钟线 60 天，小时线约 2 年
- **期权数据**在非交易时段经常不完整（持仓量为 0、IV 异常），工具会标记为不可用，不会把错误数据交给 AI
- **CoinGecko 免费接口**有频率限制（已做缓存）；**OKX** 在部分地区需要代理，可在配置中换成 bybit / kraken
- 加密货币的爆仓数据、链上资金流等暂无免费稳定来源，没有接入

## 项目结构

```
stockai/
├── cli.py              命令行
├── agent.py            分析流程（组装提示词 → AI 工具循环 → 保存报告）
├── prompts.py          系统提示词（数据纪律 + 短线/长线分析框架 + 输出格式）
├── tools.py            暴露给 AI 的 12 个工具（名称/描述/参数 schema/实现）
├── llm/                AI 提供方：anthropic_provider.py / openai_provider.py
├── data/               数据源：us_stock.py(yfinance) sec.py crypto.py(ccxt/CoinGecko/DefiLlama)
├── analytics/          纯计算：technicals.py（指标/结构）valuation.py（DCF/交易计划）
├── journal.py          分析日志与事后评估
├── cache.py            SQLite TTL 缓存
└── web/                FastAPI + 单页前端（lightweight-charts）
```

想加新数据源：在 `data/` 里写函数，在 `tools.py` 里注册一个工具（名称、描述、JSON Schema），AI 就会自动在合适的时候调用它。

## 测试

```bash
pip install -e ".[dev]"
```

```bash
pytest -q
```

## 后续可以做

- 自选股批量扫描 + 定时推送（Telegram / 邮件）
- 多角色辩论模式（技术面 / 基本面 / 风控分别出观点，再综合）
- 回测统计：按模型、模式统计历史判断胜率
- 更多数据源：FRED 宏观、Finnhub 新闻情绪、财报电话会纪要

---

> 本工具仅用于个人研究学习。AI 分析可能出错，数据可能延迟或不准确，不构成任何投资建议。
