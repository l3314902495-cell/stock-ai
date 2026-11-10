# Stock AI — AI 美股 / 加密货币分析工具

让 AI 自己调用数据工具来分析任意美股、ETF、指数或加密货币，支持**短线**（技术面 + 交易计划）和**长线**（基本面 + 估值）。数据源全部免费，AI 提供方可以自由切换（Claude 官方 / 中转站 / DeepSeek / 通义 / 任何 OpenAI 兼容接口）。

```
你: stockai analyze NVDA -m short
         │
         ▼
┌──────────────┐  按需调用工具（并行）  ┌───────────────────────────┐
│  AI（可切换） │ ─────────────────────► │ 12 个数据/计算工具 + 网络搜索 │
│  推理 + 写报告 │ ◄───────────────────── │ 指标、估值、DCF 全部代码算   │
└──────┬───────┘     浓缩后的结构化数据  └─────────────┬─────────────┘
       │ [可选] 审稿：挑错 → 修订版                     │ SQLite 缓存 + 限流重试
       ▼                                              yfinance · SEC · OKX · CoinGecko · DefiLlama
 数字核对（报告里的数字能否在工具数据中找到）→ 保存报告 + 原始数据 → 自动生成价位提醒 → 事后回放统计胜率
```

**设计原则**
- **AI 只负责推理，数字全部由代码计算**：RSI、支撑压力、估值分位、DCF、盈亏比都在 Python 里算好
- **数字自动核对**：报告里每个数字都会去本次工具数据里找，找不到的高亮显示
- **全程留痕**：报告、工具原始数据、核对结果、成本都保存下来，可复现、可追溯
- **事后检验**：每次结论都结构化记录，自动回放"是否进入入场区 → 先到止损还是目标"，统计各模型胜率

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

1. 在 `.env` 里填写 API Key（以及可选的推送密钥）
2. 在 `config.yaml` 里设置 `llm.default` 和各个 provider（官方 API / 中转站 / OpenAI 兼容）
3. **在中国大陆**：Yahoo、OKX、CoinGecko、SEC 一般需要代理，在 `network.proxy` 填写，例如 `http://127.0.0.1:7890`
4. 把 `data.sec_user_agent` 改成你自己的名字和邮箱（SEC 的要求）
5. 可选：`watchlists` 自选列表、`notify` 推送渠道、`analysis.critic` 是否默认审稿

检查配置、确认数据通畅（不调用 AI）：

```bash
stockai providers
```

```bash
stockai quote NVDA BTC SOL 苹果
```

## 使用

### 分析

```bash
stockai analyze NVDA -m short              # 短线：多周期技术面 + 期权 + 交易计划
stockai analyze AAPL -m long               # 长线：10 年财报 + 季度历史 PE 分位 + DCF + 同行
stockai analyze BTC                        # 默认 both：长线判断 + 短线时机
stockai analyze SOL -m short -p deepseek   # 指定用哪个 AI
stockai analyze TSLA -q "财报前要不要减仓"   # 附带你关心的问题
stockai analyze NVDA --critic              # 追加审稿轮：挑错并输出修订版（约 +30% 成本）
```

每次分析结束会显示：
- **数字核对**：例如 `46/49 个数字能在工具数据中找到（94%）`，并列出找不到的数字及上下文。找不到不一定是编造（可能是 AI 推算或来自网络搜索），但值得看一眼
- **成本**：Claude 官方模型自动按官方价格估算；中转站/其他服务商在 provider 里配置 `price` 后显示
- 报告保存在 `./reports/`，同名 `.tools.json` 是本次所有工具的原始返回数据

### 扫描自选股（不花 AI 的钱）

```bash
stockai scan                       # 扫描 config.yaml 的 watchlists.default
stockai scan NVDA AMD TSM BTC SOL  # 临时指定
stockai scan -w crypto --ai-top 3  # 扫描后对排名前 3 的调用 AI 做短线分析
```

信号包括：均线排列、突破/跌破 20 日高低点、接近一年新高、RSI 超买超卖、放量、布林收口、MACD 新交叉、接近支撑/压力、7 天内财报、资金费率极端、持仓量异动。按"关注度"排序，"方向"为正偏多、为负偏空。

### 价位提醒与推送

AI 分析结论里的**入场区、止损、目标、长线合理价值下沿**会自动生成提醒，也可以手动添加：

```bash
stockai alerts list                          # 查看
stockai alerts add NVDA below 170 --note 回踩支撑
stockai alerts watch                         # 前台持续监控（默认每 5 分钟）
stockai alerts test-notify                   # 测试推送配置
```

推送渠道在 `config.yaml` 的 `notify` 下配置，支持 Telegram、Server酱（微信）、Bark（iOS）、ntfy、通用 webhook（可接飞书/钉钉/企业微信机器人的中转），可同时开多个。

### 回看与统计

```bash
stockai review      # 每条历史分析：至今涨跌、短线计划结果（等待入场/止损/到目标/到期）、R 倍数、核对率
stockai stats       # 按模型 / 模式 / 资产类型汇总：胜率、平均 R、至今平均涨跌；长线按评级汇总
stockai show 12     # 查看第 12 条报告（含审稿意见、核对结果、成本）
```

短线回放规则：先判断价格是否进入 AI 给的入场区间（没进入就是"等待入场"或"未触发入场"），入场后再看先触及止损还是目标；超过 horizon 仍未结束按收盘价计算。

### 对话

```bash
stockai chat
```

网页里也有"对话追问"，默认附带当前报告作为上下文，可以接着问"止损为什么放这里""如果跌破 170 怎么办"。

### 网页界面

```bash
stockai web               # 打开 http://127.0.0.1:8765
stockai web --alerts      # 同时在后台运行价位提醒监控
```

- K 线图（MA20/50/200 + 代码计算的支撑压力线，15 分钟 ~ 周线切换）
- AI 分析：实时显示调用了哪些工具；报告里**未核实的数字黄色高亮**；审稿意见；成本；一键查看工具原始数据
- 对话追问、历史记录（点击评估至今表现）、自选股扫描（一键 AI 分析）、提醒管理、胜率统计

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
| `get_technicals` | 15m/1h/4h/1d/1wk 多周期：均线、RSI、MACD、ADX、布林、ATR、OBV、支撑压力（含触及次数）、成交密集区、多周期共振；**未收盘 K 线标记 + 预估量比**；收益/回撤/波动率/Beta、相对 SPY 和行业 ETF 的超额收益 | 同左，相对 BTC |
| `get_fundamentals` | 估值、利润率、增长、负债、空头比例、分析师目标价、5 年年报 + 6 季财务、Piotroski / Altman 分数、**TTM 自由现金流与股权激励（SBC）** | 市值/FDV/流通率/ATH、板块、DefiLlama 的 TVL/手续费/协议收入 |
| `get_long_term_financials` | SEC 官方 10 年以上财报、3/5/10 年复合增速、**季度滚动 PE 历史（约 40 个点）及当前分位/中位数/区间** | — |
| `get_derivatives` | 期权：ATM 隐含波动率、隐含波动幅度、Put/Call、最大痛点、大额持仓行权价 | 永续：资金费率（当前/7日/30日）、持仓量变化、多空比、主动买卖比、盘口深度 |
| `get_news` | Yahoo 新闻 | 同左 |
| `get_events` | 财报日、EPS 预期、近 4 次超预期、评级变动、内部人买卖、机构持仓 | — |
| `get_peers` | 同行业估值/盈利对比 | 与 BTC/ETH/SOL 等对比 |
| `get_market_context` | 指数、VIX、美债、美元、黄金、11 个行业 ETF 强弱 | BTC/ETH、恐惧贪婪、BTC 市占率、BTC 资金费率 |
| `run_dcf` | 两阶段 DCF 多情景（**默认扣除 SBC**）+ 反向 DCF（当前股价隐含的增速） | — |
| `trade_plan` | 止损距离（% / ATR 倍数）、各目标 R 倍数、按风险比例计算仓位 | 同左 |
| `web_search` | Claude 官方 API 自带：最新新闻、公告、财报指引、监管/安全事件 | 同左 |

## AI 提供方说明

- **Claude 官方 API**：默认开启提示缓存（多轮工具调用能省大量输入 token）、`effort` 档位、服务端拒答回退、网络搜索（约 $10/千次，可关）
- **Anthropic 格式中转站**：设置 `base_url` 即可。中转站如果不支持缓存、effort、网络搜索这些参数，程序收到 400 后会自动去掉重试并提示
- **OpenAI 兼容**：DeepSeek、通义、Kimi、智谱、OpenAI 格式中转、本地 Ollama 都可以，前提是模型支持 function calling。小模型的工具调用和分析质量会明显差一些——可以用 `stockai stats` 按模型比较实际表现

成本参考：一次完整分析大约调用 6~12 个工具，输入 3~8 万 token（官方 API 大部分能命中缓存）、输出 3~6 千 token；开启审稿约 +30%。

## 免费数据源的限制

- **yfinance** 是非官方接口：
  - Yahoo 改版时可能失效，遇到问题先 `pip install -U yfinance`
  - **短时间大量请求会被 Yahoo 限流**，此时 yfinance 往往不报错而是返回空数据。程序会把空数据当限流处理：自动退避重试、并且不缓存空结果；仍然取不到时工具会如实返回"未获取到"
  - 分钟线只能取最近 7 天，15 分钟线 60 天，小时线约 2 年
- **期权数据**在非交易时段经常不完整（持仓量为 0、IV 异常），工具会标记为不可用，不会把错误数据交给 AI
- **yfinance 的 `freeCashflow` 字段**是 Yahoo 的"杠杆自由现金流"，口径不同，本工具不用它，改用最近 4 季现金流量表合计
- **CoinGecko 免费接口**有频率限制（已做缓存）；**OKX** 在部分地区需要代理，可在配置中换成 bybit / kraken
- 加密货币的爆仓数据、链上资金流等暂无免费稳定来源，没有接入

## 项目结构

```
stockai/
├── cli.py              命令行
├── agent.py            分析流程（提示词 → AI 工具循环 → 审稿 → 数字核对 → 成本 → 保存 → 生成提醒）
├── prompts.py          系统提示词（数据纪律 + 短线/长线框架 + 输出格式）+ 审稿提示词
├── tools.py            暴露给 AI 的 12 个工具（名称/描述/参数 schema/实现）
├── verify.py           报告数字核对
├── pricing.py          成本估算
├── scanner.py          自选股扫描（纯代码）
├── alerts.py notify.py 价位提醒与推送
├── journal.py          分析日志、计划回放、胜率统计
├── llm/                AI 提供方：anthropic_provider.py / openai_provider.py
├── data/               数据源：us_stock.py(yfinance) sec.py crypto.py(ccxt/CoinGecko/DefiLlama)
├── analytics/          纯计算：technicals.py（指标/结构/未收盘判断）valuation.py（DCF/交易计划）
├── cache.py            SQLite TTL 缓存
└── web/                FastAPI + 单页前端（lightweight-charts，已内置不依赖 CDN）
tests/                  离线测试（含模拟 AI 服务，覆盖两种接口格式、审稿、核对、提醒推送、扫描）
```

想加新数据源：在 `data/` 里写函数，在 `tools.py` 里注册一个工具（名称、描述、JSON Schema），AI 就会自动在合适的时候调用它。

## 测试

```bash
pip install -e ".[dev]"
```

```bash
pytest -q
```

测试全部离线运行（模拟 AI 服务 + 模拟数据），不消耗 API 额度。

---

> 本工具仅用于个人研究学习。AI 分析可能出错，数据可能延迟或不准确，不构成任何投资建议。
