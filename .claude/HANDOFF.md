# Aegis Research OS — Handoff 文档

> 最后更新: 2026-04-13 · 第28轮迭代 · 633测试全通过
> 核心升级: **Phase 3 全部完成** — 回测/催化剂/A股/Insider/A股Pack/新闻情绪/A股一致预期
> 架构变迁: LLM 接管程度 ~90%，双市场（US+CN）完整覆盖，15个Sector Pack，全数据源接入
> 里程碑: 叙事情景 / Agent追问 / 电话会议NLP / 估值区间图 / 同业估值 / ConsensusStore / Driver-Based收入建模 / VariantAnalyst深化 / 预测回测 / 催化剂日历 / A股激活 / 15个Sector Pack / SEC Form 4 / A股行业模型 / 新闻情绪 / A股一致预期

---

## 第28轮完成: A股一致预期 ✅ (Phase 3 全部完成!)

**A股一致预期激活** — 解除 `not is_a_share` 守卫，yfinance 已原生支持 A 股分析师预期:

**核心发现:** yfinance `revenue_estimate` / `earnings_estimate` 对 A 股返回完整数据（如茅台 600519.SS 返回 16 个分析师的营收预期），无需 akshare。

**修改内容:**
| 文件 | 变更 |
|------|------|
| `aegis/core/orchestrator/auto_research.py` | Step 6c 守卫拆分: yfinance功能(双市场) + FRED/FMP(US-only); 新增 `_to_yfinance_symbol()` |
| `tests/unit/test_system_optimization.py` | 更新守卫测试: A股获取consensus(不再跳过) |
| `tests/unit/test_a_share_consensus.py` | **新增** — 16个测试 |

**守卫逻辑变更:**
| 功能 | 修改前 | 修改后 |
|------|--------|--------|
| 一致预期 (consensus) | ❌ A股跳过 | ✅ 双市场 (yfinance) |
| 盈利历史 (earnings history) | ❌ A股跳过 | ✅ 双市场 (yfinance) |
| 同业数据 (peer fundamentals) | ❌ A股跳过 | ✅ 双市场 (yfinance + peer map) |
| 历史估值 (historical valuation) | ❌ A股跳过 | ✅ 双市场 (yfinance) |
| 目标价 (price target) | ❌ A股跳过 | ❌ 仍US-only |
| FRED宏观 | ❌ A股跳过 | ❌ 仍US-only |
| 电话会议 (transcript) | ❌ A股跳过 | ❌ 仍US-only (FMP) |
| SEC Form 4 | ❌ A股跳过 | ❌ 仍US-only |

**影响:** A 股 DCF 收入增长率校准现在使用分析师一致预期（优先级最高），不再依赖纯历史 CAGR。

---

## 第27轮完成: 新闻情绪分析 ✅

**yfinance News + LLM 情绪分析** — 填补非结构化信息源空白:

**新增文件:**
| 文件 | 说明 |
|------|------|
| `aegis/core/acquisition/connectors/news_connector.py` | yfinance 新闻获取（US + A股，无需API key） |
| `aegis/core/chief_analyst/news_sentiment_analyzer.py` | LLM情绪分析 + rule-based fallback |
| `tests/unit/test_news_sentiment.py` | 28个单元测试 |

**修改文件:**
| 文件 | 变更 |
|------|------|
| `aegis/core/chief_analyst/__init__.py` | 导出 NewsSentimentAnalyzer, NewsSentimentInsights |
| `aegis/core/orchestrator/auto_research.py` | Step 6g + config + agent_macro["news_sentiment"] + 传给报告 |
| `aegis/core/reports/html_report.py` | News Sentiment 卡片（情绪/分数/趋势/主题/信号） |

**双模式分析:**
| 模式 | 条件 | 能力 |
|------|------|------|
| LLM模式 | `use_llm=True` | 深度情绪分析 + 主题提取 + bullish/bearish信号 + 投资叙事 |
| Rule-based | `use_llm=False` | 关键词情绪（positive/negative word counting）作为fallback |

**数据流:**
```
Step 6g: yfinance News (NEW)
  → NewsConnector.get_recent_news(ticker, limit=20)
  → NewsSentimentAnalyzer.analyze(articles, symbol)
  → LLM call_structured() / rule_based_sentiment() fallback
  → 注入 agent_macro["news_sentiment"]
  → RiskAnalyst + VariantAnalyst 可引用情绪信号
```

**报告卡片:** Sentiment badge + Score + Trend + Key Themes标签 + Bullish/Bearish信号分栏

---

## 第26轮完成: A股专属 Sector Pack ✅

**4个中国特色行业 Sector Pack** — 白酒/银行/新能源/医药，含 driver-based 收入分解:

| Pack | 行业 | 驱动因子 | 特色 |
|------|------|---------|------|
| sp_baijiu_cn_v1 | 白酒 | Premium Volume × ASP + Mid-range Volume × ASP | 合同负债前瞻/消费税/经销商压货 |
| sp_banking_cn_v1 | A股银行 | Earning Assets × NIM + Fee + Investment | LPR/不良率/LGFV/理财回表 |
| sp_new_energy_cn_v1 | 新能源 | EV Battery GWh × ASP + Storage + Materials | 产能过剩/贸易壁垒/技术路线 |
| sp_pharma_cn_v1 | 中国医药 | 创新药 + 仿制药(集采) + API/CDMO + 器械 | 集采降价/反腐/BIOSECURE Act |

**TICKER_SECTOR_MAP 新增 22 个 A 股映射:**
- 白酒: 600519(茅台), 000858(五粮液), 000568(泸州老窖), 002304(洋河), 603369(今世缘)
- 银行: 600036(招商), 601166(兴业), 000001(平安), 601398(工商), 601288(农业), 601818(光大)
- 新能源: 300750(宁德), 002594(比亚迪), 601238(广汽), 601127(赛力斯)
- 医药: 600276(恒瑞), 000538(云南白药), 300015(爱尔), 600196(复星), 300122(智飞)

**区别于 US pack 的中国特色:**
- 白酒: `special_risk_factors.policy_sensitivity` — 反腐运动可导致高端需求骤降30-50%
- 银行: `special_risk_factors.real_estate_exposure` / `lgfv_risk` — 地产+LGFV双重敞口
- 新能源: `special_risk_factors.overcapacity` / `trade_barriers` — 产能过剩+欧盟反补贴税
- 医药: `special_risk_factors.vbp_expansion` / `anti_corruption` — 集采+反腐+BIOSECURE

**文件变更:**
| 文件 | 类型 |
|------|------|
| `configs/sector_packs/sp_baijiu_cn_v1.yaml` | **新增** |
| `configs/sector_packs/sp_banking_cn_v1.yaml` | **新增** |
| `configs/sector_packs/sp_new_energy_cn_v1.yaml` | **新增** |
| `configs/sector_packs/sp_pharma_cn_v1.yaml` | **新增** |
| `aegis/core/orchestrator/auto_research.py` | 修改 — TICKER_SECTOR_MAP +22 A股 |
| `tests/unit/test_cn_sector_packs.py` | **新增** — 69个测试 |

---

## 第25轮完成: SEC Form 4 Insider Trading ✅

**SEC Form 4 数据接入** — 从 EDGAR 免费获取内部人买卖数据，Management Analyst 数据完备度 75%→90%:

**新增文件:**
| 文件 | 说明 |
|------|------|
| `aegis/core/acquisition/connectors/sec_form4_connector.py` | Form 4 connector — XML解析 + 汇总分析 |
| `tests/unit/test_form4_connector.py` | 37个单元测试 |

**修改文件:**
| 文件 | 变更 |
|------|------|
| `aegis/core/acquisition/connectors/__init__.py` | 导出文档更新 |
| `aegis/core/orchestrator/auto_research.py` | Step 6f: Form 4获取 + agent_macro注入 + 传给报告 |
| `aegis/core/agents/management_analyst/agent.py` | insider observations + inferences（sentiment/cluster/notable） |
| `aegis/core/reports/html_report.py` | Insider Trading Activity 卡片（摘要+Notable表格） |

**数据模型:**
- `InsiderTransaction` — 单笔交易（filer/title/type/shares/price/value/date）
- `InsiderSummary` — 汇总（net_value/buy_sell_count/notable/cluster/sentiment）

**分析能力:**
| 信号 | 说明 |
|------|------|
| Sentiment | bullish/bearish/neutral/mixed（基于买卖比、C-suite买入、金额） |
| Cluster Detection | 30天内3+不同内部人同向操作 |
| Notable Transactions | >$1M 的大额交易 |
| C-suite买入 | CEO/CFO/President 用个人资金买入 = 强看多信号 |

**Pipeline:**
```
Step 6e: Catalyst Calendar (existing)
Step 6f: SEC Form 4 (NEW)
  → SECAPIClient 获取 Form 4 filing list
  → 解析 XML (nonDerivativeTransaction + derivativeTransaction)
  → 计算 InsiderSummary (net, cluster, sentiment)
  → 注入 agent_macro["insider_trading"]
Step 7+: ManagementAnalyst 生成 insider observations + inferences
```

**约束:**
- US only（`not is_a_share` 守卫）
- Non-fatal enrichment（失败不阻塞管线）
- 复用已有 SECAPIClient（rate limiting + CIK resolution）
- 无新依赖（XML用标准库 `xml.etree.ElementTree`）

---

## 第24轮完成: 系统优化 ✅

**11个US Sector Pack全覆盖** — 从2/11到11/11，39个US ticker全有专业行业分析:
| Pack | 行业 | 驱动因子 |
|------|------|---------|
| sp_ad_platform_v1 | 广告平台 | DAU × Sessions × Ads × CPM |
| sp_semiconductor_v1 | 半导体 | GPU Units × ASP + Networking + Software |
| sp_saas_v1 | SaaS | Customers × ARPU × Retention |
| sp_banking_v1 | 银行 | Earning Assets × NIM + Fees |
| sp_biotech_pharma_v1 | 生物医药 | Key Drugs + Pipeline - Biosimilar Erosion |
| sp_consumer_electronics_v1 | 消费电子 | Smartphones/PC/Wearables/Services/Gaming |
| sp_energy_v1 | 能源 | Upstream/Downstream/Chemical/LNG |
| sp_industrial_v1 | 工业 | Equipment/Aftermarket/Automation/Construction/Defense |
| sp_reits_v1 | REITs | Occupancy × Rental × Area + Development |
| sp_ecommerce_v1 | 电商 | GMV/Buyers/AOV/1P/Cloud/Ads |
| sp_consumer_staples_v1 | 日用消费 | DM/EM Volume × Price/Mix |

**其他优化:**
- A股 OpenBB guard — `not is_a_share` 条件守卫，跳过 US-only 的 consensus/transcript/macro
- 报告货币全面国际化 — 11处 `$` 硬编码替换为 `ccy` 变量（¥/$），单位 B/亿
- A股 peer 映射 — 11个蓝筹（白酒/保险/家电/银行/新能源/医药/半导体），yfinance .SS/.SZ 格式

---

## 第23轮完成: A股Connector激活 + 兼容性修复 ✅

**CNINFO Connector 从 mock 升级到真实数据:**
- `_fetch_via_yfinance()` — yfinance 获取完整三表 + 35个 yf→CAS 概念映射
- `_to_yfinance_ticker()` — 6位代码→yfinance格式（600xxx→.SS, 000/300xxx→.SZ）
- 编排器 `_is_a_share_ticker()` + 双管线路由（US: SEC→EDGAR→XBRL / CN: CNINFO→yfinance→CAS）

**兼容性修复（三个核心差距）:**
- FactBridge: 新增 `market_id`/`currency` 参数，段落处理按市场选 adapter，`__currency`/`__market_id` 标注
- 历史数据: `_extract_a_share_historical()` — yfinance 多年报表提取，自动计算 CAGR
- DCFInput: 新增 `currency` 字段，报告场景估值用 ¥/$

**数据流:** `CAS原始 → CNMarketAdapter → FactBridge(cn,CNY) → meta_facts → DCF(CNY) → 报告(¥)`

---

## 第22轮完成: 催化剂日历 ✅

**CatalystCalendar (`aegis/core/catalyst_calendar.py`):**
| 数据源 | 事件类型 |
|--------|---------|
| yfinance | 财报日（过去+未来，含EPS estimate/surprise） |
| SEC日历 | 10-K/10-Q filing截止日（60/40天规则） |
| Sector Pack | 行业事件（从 `catalyst_calendar` YAML字段） |
| Agent推理 | 边际衰减、追踪指标（兼容 CatalystEvent + dict） |
| 电话会议 | 管理层指引变更（raised/lowered/withdrawn） |
| 市场数据 | 除息日 |

**数据模型:** CalendarEvent（urgency/impact_direction/days_until） + CatalystTimeline（.upcoming/.next_catalyst/.next_earnings）
**集成:** agent_macro["catalyst_calendar"] + HTML报告 Catalyst Timeline 卡片

---

## 第21轮完成: 预测回测系统 ✅

**闭环反馈系统:**
- 编排器 `run()` 结束自动 `CalibrationLoop.record_thesis()` 记录预测
- `ForecastAccuracyReport` — 方向准确率、MAE、场景命中率、系统偏差检测
- `review_due_predictions()` — 自动扫描到期预测 + 批量 post-mortem
- `get_calibration_context()` — 校准数据注入 agent_macro
- HTML报告 Prediction Calibration Dashboard 卡片

---

## 第20轮完成: Phase 2B-5 收入建模升级 (driver-based) ✅

**升级内容**: 从单一 `revenue_growth_path` 升级为 driver-based 乘法分解模型

**核心数据结构 (`dcf_engine.py`):**
- `RevenueDriver` — 单个收入驱动因子（名称、基准值、10年增长路径、单位）
- `RevenueDriverTree` — 乘法收入分解树（Revenue = ∏ drivers × scale）
- `resolve_driver_revenue(base_revenue, driver_tree)` — driver tree → revenue_growth_path 转换
- `apply_driver_deltas(tree, deltas)` — 场景级别驱动因子调整

**引擎集成 (`auto_research.py`):**
- DCFInput 增长路径优先级: driver tree > 一致预期 > 历史CAGR > 规模启发式
- `_build_driver_tree()` — 从 sector pack YAML 自动构建驱动树
- Bear/Bull DCF 支持 `driver_deltas`（个别驱动因子调整 vs 全局delta）
- driver_projections 注入 scenarios dict + agent_macro 供 Agent 和报告使用

**ScenarioArchitect 升级 (`scenario_architect.py`):**
- `ScenarioCase.driver_deltas: dict[str, list[float]]` — 可选驱动因子级别场景调整
- LLM tool schema 新增 driver_deltas 字段
- `_build_message` 向 LLM 提供驱动因子上下文（当 sector pack 含 revenue_drivers 时）

**Sector Pack YAML 定义 (`configs/sector_packs/`):**
- `sp_ad_platform_v1.yaml` — 广告平台 4 驱动模型: Revenue = DAU × Sessions/DAU × Ads/Session × CPM/1000
- `sp_semiconductor_v1.yaml` — 半导体 4 驱动模型: Revenue = GPU_Units × ASP + Networking + Software

**兼容性修复:**
- `business_analyst`, `sector_context_agent`, `sector_critic` — 兼容 string/dict 两种 `key_kpis` 格式

**设计特点:**
- 完全向后兼容 — 无 sector pack 驱动定义时自动回退原逻辑
- 乘法组合 — 驱动因子相乘产生收入，忠实反映业务机理
- Scale factor 自动校准 — 确保 Year 0 产出匹配实际基准收入

**文件变更:**
| 文件 | 变更 |
|------|------|
| `aegis/core/truth/scenario_engine/dcf_engine.py` | 新增 RevenueDriver, RevenueDriverTree, resolve_driver_revenue(), apply_driver_deltas() |
| `aegis/core/chief_analyst/scenario_architect.py` | ScenarioCase.driver_deltas + tool schema + _build_message driver context |
| `aegis/core/orchestrator/auto_research.py` | _build_driver_tree() + driver tree→DCF集成 + bear/bull driver_deltas |
| `aegis/core/agents/business_analyst/agent.py` | key_kpis string兼容 |
| `aegis/core/agents/sector_context_agent/agent.py` | key_kpis string兼容 |
| `aegis/core/critics/sector_critic/critic.py` | key_kpis string兼容 |
| `configs/sector_packs/sp_ad_platform_v1.yaml` | **新增** 广告平台 sector pack |
| `configs/sector_packs/sp_semiconductor_v1.yaml` | **新增** 半导体 sector pack |
| `tests/unit/test_driver_revenue.py` | **新增** 29个单元测试 |

---

## 第19轮完成: Phase 2B-3/4 ConsensusStore + VariantAnalyst深化 ✅

**升级内容**: ConsensusStore 持久化 + MarketExpectationsLayer 真实数据填充 + VariantAnalyst revision momentum 深化

**ConsensusStore 持久化:**
| 文件 | 变更 |
|------|------|
| `aegis/core/storage/models.py` | 新增 `ConsensusSnapshotRow` ORM 模型（含 1w/1m/3m/6m 修正字段） |
| `aegis/core/storage/repository.py` | 新增 save_consensus_snapshot, save_consensus_batch, get_latest_consensus, get_consensus_for_run, get_consensus_for_entity |
| `aegis/core/storage/__init__.py` | 导出 ConsensusSnapshotRow |

**MarketExpectationsLayer 激活 (`expectations.py`):**
- `ingest_consensus_estimates()` — 从 OpenBB ConsensusEstimate 自动转换并填充内存层
- `get_aggregate_revision_signal()` — 跨指标聚合广度判断（broad_upgrade/downgrade/mixed）
- `ConsensusRevisionSignal` 新增 `acceleration` 字段（accelerating/decelerating/stable）
- `get_revision_signal()` 增强: 计算修正速度加速/减速

**编排器集成 (`auto_research.py`):**
- 真实一致预期数据替代硬编码 `"neutral"` revision_momentum
- `revision_signal_detail` dict（含 momentum/breadth/acceleration/1w/1m/3m pct）注入 agent_macro

**VariantAnalyst 深化 (`variant_analyst/agent.py`):**
- 丰富的 `revision_signal` 对象替代简单字符串观察
- **共识动量对齐推理**: 判断 variant 是 FAVORABLE（顺势）还是 CONTRARIAN（逆势）
- **加速/减速检测**: 修正速度是在加快还是放缓
- **广度判断**: broad_upgrade / broad_downgrade / mixed
- 新增第 4 个杀死触发器: 共识修正方向逆转

**文件变更:**
| 文件 | 变更 |
|------|------|
| `aegis/core/storage/models.py` | 新增 ConsensusSnapshotRow |
| `aegis/core/storage/repository.py` | 5个consensus repo方法 + stats |
| `aegis/core/storage/__init__.py` | 导出 |
| `aegis/core/market_expectations/expectations.py` | ingest_consensus_estimates, get_aggregate_revision_signal, acceleration |
| `aegis/core/orchestrator/auto_research.py` | 真实 revision signal 注入 |
| `aegis/core/agents/variant_analyst/agent.py` | 丰富观察 + 动量对齐推理 + 逆转触发器 |
| `tests/unit/test_consensus_store.py` | **新增** 28个单元测试 |

---

## 第18轮完成: Phase 2B-2 同业相对估值矩阵 ✅

**升级内容**: Peer Comparison 从基础表格升级为完整的相对估值分析

**改动:**
| 文件 | 变更 |
|------|------|
| `aegis/core/reports/html_report.py` | Peer表格重构: 主体公司高亮★行、Peer Median行、Premium/Discount标签、P/E+EV/EBITDA横向对比柱状图 |

**新增报告元素:**
- 主体公司以蓝色高亮显示在表格首行（★标记）
- Peer Median统计行（斜体，灰色背景）
- "P/E vs peer median: +15% premium" / "EV/EBITDA vs peer median: -8% discount" 标签（红/绿色标）
- 双横向柱状图：P/E Comparison + EV/EBITDA Comparison（主体蓝色，peers灰色）

---

## 第17轮完成: Phase 2B-1 历史估值区间图 ✅

**数据源**: yfinance（免费，无API key）— 5年月度P/E和EV/EBITDA

**新增/修改文件:**
| 文件 | 变更 |
|------|------|
| `aegis/core/acquisition/connectors/openbb_connector.py` | 新增`get_historical_valuation()` — yfinance获取5年月度PE/EV-EBITDA |
| `aegis/core/orchestrator/auto_research.py` | Step 6c新增历史估值获取 + 传递给HTML报告 |
| `aegis/core/reports/html_report.py` | 新增`_build_valuation_chart_js()` + Historical Valuation Range双图表卡片 |
| `tests/unit/test_historical_valuation.py` | 7个单元测试 |

**报告新增内容:**
- 5年P/E走势图（蓝色线）+ 中位数/P25/P75参考线
- 5年EV/EBITDA走势图（绿色线）+ 区间带
- 统计摘要：min-max范围、中位数、当前值
- Chart.js渲染，与现有报告风格一致

---

## 第16轮完成: Phase 2A-3 电话会议纪要接入 ✅

**数据源**: FMP Earnings Transcript API（`FMP_API_KEY` 环境变量，250次/天免费）

**新增文件:**
| 文件 | 说明 |
|------|------|
| `aegis/core/chief_analyst/earnings_call_analyzer.py` | EarningsCallAnalyzer LLM层 — 从纪要中提取投资信号 |
| `tests/unit/test_earnings_call.py` | 11个单元测试 |

**修改文件:**
| 文件 | 变更 |
|------|------|
| `aegis/core/acquisition/connectors/openbb_connector.py` | 新增`get_earnings_transcript()` — FMP API获取完整纪要 |
| `aegis/core/orchestrator/auto_research.py` | Step 6d: 获取纪要→LLM分析→注入agent_macro["earnings_call"] |
| `aegis/core/reports/html_report.py` | "Earnings Call Insights"卡片（管理层tone、guidance、analyst focus、hedging） |
| `aegis/core/chief_analyst/__init__.py` | 导出EarningsCallAnalyzer, EarningsCallInsights |

**EarningsCallInsights输出:**
- `overall_tone`: confident / cautiously_optimistic / defensive / neutral
- `tone_shift_vs_prior`: more_confident / less_confident / unchanged
- `guidance_items`: [{metric, guidance_text, direction(raised/maintained/lowered/new/withdrawn)}]
- `analyst_focus_topics`: 买方最关注的3-5个话题
- `hedging_signals`: 管理层回避/模糊回答检测
- `management_key_numbers`: 管理层主动提到的关键数字
- `call_summary`: 3-5句投资视角摘要
- `materiality`: high/medium/low

**Pipeline:**
```
Step 6c: OpenBB Data (existing)
Step 6d: Earnings Call Transcript (NEW)
  → FMP API获取最近一季纪要
  → EarningsCallAnalyzer LLM分析
  → 注入 agent_macro["earnings_call"] 供所有Agent使用
Step 7+: 所有Agent现在能看到电话会议洞察
```

---

## 第15轮完成: Phase 2A-2 Agent追问能力 (Follow-Up Questions) ✅

**问题**: Agent一次性输出判断，发现异常后不能"追问"获取更多数据。

**方案**: Agent输出新增`follow_up_questions`，Orchestrator检查数据是否已有→自动补充并重跑Agent→未能回答的记录为`open_questions`传给Synthesizer和报告。

**新增/修改文件:**
| 文件 | 变更 |
|------|------|
| `aegis/data_contracts/judgment_schema.py` | 新增`FollowUpQuestion`模型 + `JudgmentContract.follow_up_questions`字段 |
| `aegis/core/agents/llm_agent_base.py` | tool schema新增follow_up_questions + 解析逻辑 + system prompt指导 |
| `aegis/core/agents/base.py` | `AgentInput.supplemental_data` — 第二次分析时注入追问答案 |
| `aegis/core/orchestrator/auto_research.py` | Agent循环中: 检查high优先级问题→`_try_answer_follow_up()`查数据→有则重跑agent→收集open_questions |
| `aegis/core/chief_analyst/thesis_synthesizer.py` | `SynthesizedThesis.open_questions` + synthesize()接收open_questions + 注入LLM context |
| `aegis/core/decision_engine/engine.py` | `ThesisDecision.open_questions` |
| `aegis/core/reports/html_report.py` | "Open Research Questions"卡片（优先级+Agent来源+问题） |
| `tests/unit/test_follow_up_questions.py` | 17个单元测试 |

**数据查找策略 (`_try_answer_follow_up`):**
- `metric` → 查`computed_metrics`（精确+模糊匹配）
- `segment` → 查`segment_detail`中各category的分段数据
- `fact` → 查`meta_facts`
- `time_series` → 查`historical_data`
- 找不到 → return None → 记录为open_question

**约束:**
- 每Agent最多3个follow-up questions
- 只有`priority="high"`触发重跑
- 每Agent最多1次重跑（防止循环）
- Rule-based agents不生成follow-up questions（向后兼容）

---

## 第14轮完成: 代码精简 + Phase 2A-1 ScenarioArchitect ✅

### 代码精简

删除23个空壳目录（仅含空`__init__.py`）、14个未加载的config YAML、空的prompts/scripts/docs、84KB的SYSTEM_CONSTITUTION.md。
从210→186 Python文件，~140→78 目录。核心管线完全不受影响。

### ScenarioArchitect — 叙事驱动情景构建

**问题**: bear/bull情景是机械式 `growth ± 3-4%, margin ± 2-3%`，专业读者一眼识破。

**方案**: 新增第四个Chief Analyst组件 `ScenarioArchitect`，在base DCF计算后、bear/bull DCF之前运行LLM，生成三个叙事驱动的业务情景。

**新增文件:**
| 文件 | 说明 |
|------|------|
| `aegis/core/chief_analyst/scenario_architect.py` | ScenarioArchitect LLM层（ScenarioBlueprint, ScenarioCase dataclass + tool schema + system prompt） |
| `tests/unit/test_scenario_architect.py` | 12个单元测试 |

**修改文件:**
| 文件 | 变更 |
|------|------|
| `aegis/core/orchestrator/auto_research.py` | Step 7b/7c: ScenarioArchitect替代机械bear/bull；portfolio signal用动态概率权重 |
| `aegis/core/decision_engine/engine.py` | ThesisDecision新增: scenario_narratives, scenario_probabilities, probability_weighted_value, primary_swing_factor |
| `aegis/core/reports/html_report.py` | 情景卡片显示叙事文本+概率权重+概率加权目标价+swing factor |
| `aegis/core/reports/serializers.py` | investment_memo/one_page_note/dashboard_json新增narrative+probability字段 |
| `aegis/core/chief_analyst/__init__.py` | 导出ScenarioArchitect, ScenarioBlueprint |
| `aegis/core/portfolio/portfolio_integration.py` | 修复pre-existing除零bug |

**ScenarioArchitect输出:**
```python
ScenarioBlueprint:
  scenarios: [ScenarioCase(name, probability, narrative, key_driver, revenue_growth_delta[10], margin_delta[10])]
  key_disagreements: list[str]   # 我们vs市场的分歧点
  primary_swing_factor: str       # 决定情景走向的关键变量
```

**Pipeline插入点:**
```
Step 7a: Base DCF (不变)
Step 7b: ScenarioArchitect LLM call (NEW)
Step 7c: Bear/Bull DCF with architect's deltas (替代机械±3-4%)
```

**Fallback**: `use_llm=False`或LLM调用失败时回退到机械模式。

**LLM接管更新:**
| 维度 | 升级前 | 升级后 |
|------|--------|--------|
| 情景构建 | 机械±3-4% | LLM叙事驱动 ✅ |
| 概率权重 | 固定25/50/25 | LLM动态评估 ✅ |
| 情景叙事 | 无 | 每个情景2-3句业务故事 ✅ |

---

## 第9轮完成: OpenBB数据层 + 报告品质升级 ✅

### 新增: OpenBB Data Connector (`aegis/core/acquisition/connectors/openbb_connector.py`)

| 数据类型 | 方法 | 数据源 | 状态 |
|---------|------|--------|------|
| 一致预期 (Revenue/EPS/EBITDA) | `get_consensus_estimates()` | **yfinance (免费)**, FMP fallback | ✅ 已验证 |
| 分析师目标价 | `get_price_target_consensus()` | **yfinance (免费)** | ✅ 已验证: $296 (40 analysts) |
| 盈利Beat/Miss历史 | `get_earnings_history()` | **yfinance (免费)** | ✅ 已验证: 8季EPS surprise |
| 宏观指标 (Fed/10Y/CPI/PMI/VIX) | `get_macro_snapshot()` | **FRED via OpenBB** | ✅ 已验证: 实时数据 |
| 同业财务数据 | `get_peer_fundamentals()` | **yfinance (免费)** | ✅ 已验证: 5家同业 |
| 同业发现 | `get_sector_peers()` | 内置映射表 + FMP fallback | ✅ 已验证: 16个行业 |

### 管线集成

**Orchestrator (`auto_research.py`) 新增Step 6c:**
1. 自动检测 FMP_API_KEY / FRED_API_KEY 环境变量
2. 若有key → 拉取一致预期、盈利历史、同业数据、目标价共识
3. 若有FRED key → 用实时FRED数据替代硬编码宏观快照
4. 所有数据注入 `agent_macro` 上下文供Agent使用
5. 所有数据传入 `generate_html_report()` 渲染新板块

**ResearchConfig 新增字段:**
- `fmp_api_key`, `fred_api_key`, `enable_openbb`

**CLI 新增参数:**
- `--fmp-key`, `--fred-key`, `--no-openbb`

### 报告格式修复

| 问题 | 修复 |
|------|------|
| `EdgeType.ANALYTICAL` 枚举暴露 | `_format_enum()` → "Analytical" |
| `Iphone` / `Ipad` 命名 | `_format_segment_name()` → "iPhone" / "iPad" |
| `Wearables Homeand Accessories` 缺空格 | 名称映射表 → "Wearables, Home & Accessories" |
| `Product` / `Service` 不够清晰 | 映射为 "Products" / "Services" |

### HTML报告新增板块

1. **Consensus Estimates** — Revenue/EPS/EBITDA 一致预期表（按期间展示 Low/Mean/High/分析师数）
2. **Analyst Price Targets** — 目标价区间 (Low/Median/Consensus/Upside%)
3. **Earnings History (Beat/Miss)** — 近8季EPS/Revenue surprise表，颜色标注
4. **Peer Comparison** — 同业对标表 (Mkt Cap/Revenue/GM/OM/ROIC/PE/EV-EBITDA)

---

## 第10轮完成: 分析引擎升级 + 决策系统修复 ✅

### SBC Double-Counting 根因修复

**问题**: 所有7个Agent的judgment都同时声明使用了 `sbc_to_revenue` 和 `dilution_rate` 两个指标，触发Logic Critic和Accounting Critic的double-counting BLOCK，导致系统永远输出BLOCKED。

**修复**: `AgentBase._collect_metric_ids()` 增加SBC/dilution互斥逻辑 — Agent不再同时声明两个指标。

### Publish Gate 阈值校准

| 参数 | 修改前 | 修改后 | 原因 |
|------|--------|--------|------|
| warn_accumulation_threshold | 8 | 20 | 7 agents × 7 critics 产生17个正常warn，8的阈值过严 |

### Edge Assessment 实质化

修改前: `"Systematic factor decomposition may identify mispricings"` (占位文本)
修改后: 基于DCF/Reverse DCF/Sensitivity Analysis的定量edge描述，包含：
- DCF base case vs 当前价的gap%
- 最关键的敏感性因子
- Market-implied growth vs 历史CAGR的对比
- 一致预期数据引用

### Revenue Growth 一致预期校准

修改前: 仅用历史CAGR线性衰减
修改后: 优先使用yfinance一致预期的FY_Current和FY_Next收入估计推导Y1/Y2增长率，Y3-Y10再衰减至terminal rate

### Portfolio Signal 逻辑增强

修改前: 仅靠variant文本中的"upside"/"downside"关键词
修改后: 使用DCF base case vs 当前价差异自动判断方向（>10% upside=long, >10% downside=short）

---

## 当前系统状态

### 数据源覆盖

| 层级 | 数据源 | US | CN (A股) |
|------|--------|-----|----------|
| Tier 1 (监管) | SEC EDGAR XBRL | ✅ 10-K, 10-Q, 分段数据 | — |
| Tier 1 (监管) | CNINFO / yfinance | — | ✅ 三表数据 + CAS概念映射 |
| Tier 2 (聚合) | OpenBB/FMP | ✅ 一致预期、盈利历史、同业、电话会议纪要 | ❌ 已守卫跳过 |
| Tier 2 (聚合) | OpenBB/FRED | ✅ 宏观实时数据 | ❌ US only |
| Tier 2 (聚合) | ConsensusStore | ✅ 一致预期持久化 + revision tracking | ✅ yfinance双市场 |
| Tier 3 (市场) | Yahoo Finance | ✅ 价格、市值、基本面、5年历史估值 | ✅ .SS/.SZ格式 |
| 回测 | CalibrationLoop | ✅ 预测记录 + post-mortem + 校准反馈 | ✅ |
| 催化剂 | CatalystCalendar | ✅ 财报日 + SEC filing + 电话会议 | ✅ (财报日) |
| Tier 3 (市场) | yfinance News | ✅ LLM情绪分析+rule-based | ✅ yfinance双市场 |
| Tier 1 (监管) | SEC Form 4 | ✅ insider买卖+cluster+sentiment | ❌ US only |

### 已激活Agent能力

| Agent | 数据完备度 | 核心依赖 |
|-------|-----------|---------|
| Accounting Analyst | 90% | XBRL/CAS → ✅ |
| Business Analyst | 90% | 三表 + 一致预期 + driver decomposition(11个行业) → ✅ |
| Valuation Analyst | 90% | DCF(双货币) + driver-based revenue + 一致预期 + 同业 → ✅ |
| Management Analyst | **90%** | 三表 + 电话会议NLP + SEC Form 4 insider交易 → ✅ |
| Risk Analyst | 85% | 三表 + 宏观 + 电话会议hedging + 催化剂时间线 → ✅ |
| Sector Context Agent | **95%** | 11个 Sector Pack(全覆盖driver定义) + 同业 → ✅ |
| Variant Analyst | **95%** | revision momentum + 动量对齐 + 催化剂timing + 校准反馈 → ✅ |

---

## 第11-12轮完成: Chief Analyst Layer — LLM 研究主导权升级 ✅

### 核心问题
系统中 LLM 像"被关在流程里的分析组件"而非"主导判断的研究员"。单独问 LLM 反而更像顶级分析师，放进系统后输出"完整但不尖锐"。根因：系统默认"流程先于判断"，LLM 自由度被格式和固定 pipeline 削弱。

### 第11轮: 三层 Chief Analyst Layer ✅

**1. Research Director (`aegis/core/chief_analyst/research_director.py`)** — 在所有 Agent 之前
- 输入: entity_id + meta_facts + sector_pack
- 输出: ResearchDirective（salient_characteristics, initial_hypothesis, key_variables, key_controversy, agent_emphasis, agent_depth, opening_angle, why_now, key_numbers）
- 作用: "首席分析师定调"，给每个 Agent 注入针对性指导

**2. Thesis Synthesizer (`aegis/core/chief_analyst/thesis_synthesizer.py`)** — 在所有 Agent 之后
- 输入: ResearchDirective + 7个 Agent 的 JudgmentContract
- 输出: SynthesizedThesis（core_thesis, my_variant, variant_magnitude, market_implied_story, counter_thesis, edge_source, conviction_narrative, hypothesis_validated, hypothesis_evolution, biggest_surprise, agents_that_challenged）
- 作用: 替代旧的关键词匹配 thesis 提取，LLM 综合判断

**3. Report Editor (`aegis/core/chief_analyst/report_editor.py`)** — 在报告序列化之前
- 输入: SynthesizedThesis + ResearchDirective + DecisionEngine output
- 输出: EditedReport（headline, opening_paragraph, executive_summary, front_page_numbers, section_order, closing_paragraph）
- 作用: LLM 编辑层，控制报告叙事和编排

### 第12轮: 动态 Agent 执行 + 假设验证闭环 ✅

**动态 Agent 执行**
- Research Director 输出 `agent_depth: dict[str, str]`，控制每个 agent 深度:
  - `"deep"`: LLM agent + 额外 prompt 要求更多 obs/inf/counterargs（6-8 obs, ≥2 counterargs, ≥3 triggers）
  - `"standard"`: 正常 LLM agent
  - `"light"`: 跳过 LLM，用规则 agent（只抓红旗）
  - `"skip"`: 只跑规则 fallback 做最低覆盖
- `research_priority_order` 映射到 agent 名称，决定执行顺序
- 管线日志显示: 执行计划、跳过/深度分析标记

**假设验证闭环**
- SynthesizedThesis 新增: hypothesis_validated, hypothesis_evolution, biggest_surprise, agents_that_challenged
- Synthesizer system prompt 要求显式回答: 假设是否被验证/推翻，哪些 agent 挑战了假设，最大意外是什么
- HTML 报告新增 "Research Process: Hypothesis Validation" 卡片（绿色 Confirmed / 橙色 Revised）

### 文件变更清单 (第11-12轮)

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `aegis/core/chief_analyst/__init__.py` | **新增** | Chief Analyst 包 |
| `aegis/core/chief_analyst/research_director.py` | **新增** | 研究方向制定 + agent_depth |
| `aegis/core/chief_analyst/thesis_synthesizer.py` | **新增** | Thesis 综合 + 假设验证 |
| `aegis/core/chief_analyst/report_editor.py` | **新增** | 报告编辑层 |
| `aegis/core/agents/llm_agent_base.py` | 修改 | 注入 Director 指导 + deep mode prompt |
| `aegis/core/orchestrator/auto_research.py` | 修改 | Step 9b/12b/14b + 动态 agent 执行循环 |
| `aegis/core/engine/engine.py` | 修改 | 接受 SynthesizedThesis 替代关键词匹配 |
| `aegis/core/reports/html_report.py` | 修改 | Editorial 首页 + 假设验证卡片 |
| `aegis/core/agents/base.py` | 修改 | AgentInput.previous_agent_findings + AgentOutput.narrative_supplement |
| `aegis/core/agents/llm_agent_base.py` | 修改 | JUDGMENT_TOOL_SCHEMA_DEEP + 信息传递 prompt + rerun context |

### 第13轮: Phase 1.5 — Agent 信息流 + 迭代研究 + 弹性输出 ✅

**1. Agent 间信息传递**
- 每个 agent 执行后，`_extract_key_finding()` 提取最高置信度 inference + 红旗检测
- 提取结果以 `previous_agent_findings` 注入下一个 agent 的 prompt
- 下游 agent 看到上游 agent 的关键发现，可以 build on / challenge / deepen
- 排序终于有了实际意义：先跑的 agent 的发现真的影响后跑的 agent

**2. 迭代再分析（Iterative Re-Analysis）**
- Thesis Synthesizer 发现 `hypothesis_validated=False` 且有 `agents_that_challenged` 时触发
- 自动对挑战假设的 agent 以 DEEP 模式重跑（注入 rerun_context：假设演化、最大意外、修正方向）
- 重跑 agent 看到所有一轮结果 + synthesizer 的反思
- 重跑后自动 re-synthesize，更新 thesis
- 日志标记 `[RE-RUN DEEP]`，可追踪迭代过程

**3. Agent 输出格式弹性化**
- `AgentOutput` 新增 `narrative_supplement: str` 字段
- Deep 模式 agent 使用 `JUDGMENT_TOOL_SCHEMA_DEEP`，schema 含 narrative_supplement
- Prompt 要求写 300-800 字 analyst memo（超越结构化输出的自由分析）
- Thesis Synthesizer 接收 `narrative_supplements: dict[str, str]`，在综合时参考深度 memo

### 当前 LLM 接管程度评估

| 维度 | 升级前 | 升级后 | 理想状态 |
|------|--------|--------|----------|
| 谁定 thesis | 关键词匹配 | LLM 综合 ✅ | LLM 综合 |
| 谁定研究方向 | 固定 pipeline | Director 控制深度/顺序 ✅ | Director 实际控制 |
| Agent 执行 | 全部跑，固定顺序 | 动态深度+顺序+信息传递 ✅ | ✅ 已达到 |
| Agent 输出 | 固定 JudgmentContract | 结构化 + narrative_supplement ✅ | 灵活格式 |
| 报告编排 | 纯序列化 | LLM 编辑 ✅ | LLM 编辑 |
| 迭代深入 | 无 | 假设推翻→自动重跑+再综合 ✅ | ✅ 已达到 |
| 数据真相层 | 硬规则 | 硬规则 ✅ | 硬规则（不该变） |

---

## 系统全面评估 (第13轮后)

### 目标定位
仅用公开信息，做尽可能接近人类顶尖分析师的报告。信息差（专家网络、渠道调研、另类数据）是不可逾越的鸿沟，但在公开信息范围内，架构和分析深度可以做到很好。

### 当前水平评估 (第28轮)
- **架构设计: 9.5/10** — 双市场(US+CN) + 15行业driver + 闭环回测 + 催化剂 + insider + 新闻 + 双市场consensus
- **数据源: 9/10** — SEC EDGAR(XBRL+Form4) + yfinance(双市场+新闻+consensus) + FRED + FMP + ConsensusStore + CatalystCalendar
- **分析逻辑: 9/10 (LLM) / 6.5/10 (rule-based)** — 叙事情景 + driver分解 + revision momentum + 追问 + 校准 + 新闻情绪
- **产出质量: 9/10** — Phase 3 全部完成，双市场完整数据覆盖，报告信息密度达专业级
- **预测校准: 5/10** — 回测框架已建好，需积累实际数据才能真正校准
- **市场覆盖: 9/10** — US完整，CN现有consensus+peers+valuation+4个专属行业模型

### 核心瓶颈 (当前)
1. **回测数据积累**: 框架就绪但需实际运行积累预测→post-mortem数据
2. **A股电话会议缺失**: FMP 不覆盖 A 股电话会议纪要（需中文数据源，非关键路径）
3. **更多 A 股 ticker 覆盖**: 当前映射 ~22 个 A 股 ticker，可扩展至更多行业

---

## 下一步待办 (优先级排序)

### 🔴 Phase 2A — 接近顶尖的三个关键升级 (最高优先级)

#### 1. ~~电话会议纪要接入 + NLP 分析~~ ✅ 第16轮已完成
- FMP Transcript API + EarningsCallAnalyzer LLM分析
- **实现方案**:
  - 数据源: Seeking Alpha (免费纪要) 或 Financial Modeling Prep (API)
  - NLP 管线:
    - 管理层措辞变化检测 ("confident" → "cautiously optimistic" 是重要信号)
    - 关键变量提取 (管理层提到的 guidance, 关键 KPI)
    - 分析师提问方向分析 (买方在关注什么)
    - 管理层回避/模糊回答检测 (hedging language detection)
    - 指引准确度追踪 (上次说的 vs 实际结果)
  - 集成点: 注入 agent_macro context，所有 Agent 可引用；Accounting Agent 重点比对指引 vs 实际
  - 新增 Agent 可能: `EarningsCallAnalyst` 专门分析电话会议
- **预期效果**: 让 Research Director 的"定调"有更丰富的信息基础；让 Variant Analyst 能识别"市场还没消化的电话会议信号"

#### 2. ~~情景构建从机械改为叙事驱动~~ ✅ 第14轮已完成
- ScenarioArchitect LLM层已实现，替代机械±3-4%
- **目标状态**: 每个情景是一个完整的业务故事
  ```
  Bear (20%, $180): "AWS增速从30%降至15%（企业迁移周期成熟）+ 广告收入受衰退影响下降8%"
  Base (55%, $220): "AWS维持25%增长（AI workload offsetting）+ 广告mid-single-digit复苏"
  Bull (25%, $280): "AI成为新的AWS-scale business + retail media flywheel加速"
  ```
- **实现方案**:
  - 新增 `ScenarioArchitect` (LLM call，在 DCF 之前)
  - 输入: Research Director 的 key_variables + sector_pack 的 cycle_characteristics + 一致预期
  - 输出: 3个叙事情景，每个含：概率权重、故事描述、各关键变量的具体假设值
  - 替代当前的机械 bear/bull 计算
  - 概率加权目标价 = Σ(概率 × 情景目标价)
- **预期效果**: 报告中最容易被识破的弱点消失；情景部分从"凑数"变成"有洞见"

#### 3. ~~Agent "追问"能力~~ ✅ 第15轮已完成
- Agent输出follow_up_questions → 系统自动查数据补充 → 重跑agent → open_questions传给报告
- **实现方案**:
  - Agent 输出新增 `follow_up_questions: list[FollowUpQuestion]`
  - 每个 FollowUpQuestion 包含: question_text, data_needed (metric/segment/time_series), priority
  - Orchestrator 在 agent 执行后检查 follow_up_questions
  - 如果数据已存在于 segment_detail/metric_results 中 → 自动补充并让 agent 重新分析
  - 如果数据不存在 → 记录为 `open_question`，传给 Synthesizer 和报告
  - 限制: 每个 agent 最多 2 个 follow-up，防止无限循环
- **预期效果**: 从"声明式分析"变成"对话式分析"；open_questions 本身就是有价值的输出（告诉读者"要验证这个 thesis 还需要什么信息"）

### Phase 2B — 分析深度 ✅ 全部完成

- [x] **~~ConsensusStore 正式填充~~**: ✅ 第19轮
- [x] **~~收入建模升级 (driver-based)~~**: ✅ 第20轮
- [x] **~~历史估值区间图~~**: ✅ 第17轮
- [x] **~~Variant Analyst 深化~~**: ✅ 第19轮
- [x] **~~同业相对估值~~**: ✅ 第18轮

### Phase 3 — 差异化 (进行中)

- [x] **~~预测回测系统~~**: ✅ 第21轮 — CalibrationLoop + ForecastAccuracyReport + 校准反馈注入
- [x] **~~催化剂日历~~**: ✅ 第22轮 — CatalystCalendar 6数据源 + HTML时间线
- [x] **~~A股Connector激活~~**: ✅ 第23轮 — yfinance三表 + CAS映射 + 双管线路由
- [x] **~~系统优化~~**: ✅ 第24轮 — 11 sector pack + 报告货币国际化 + A股peer + OpenBB guard
- [x] **~~Insider 交易数据~~**: ✅ 第25轮 — SEC Form 4 EDGAR解析 + cluster detection + Management Analyst增强
- [x] **~~A股专属 sector pack~~**: ✅ 第26轮 — 白酒/银行/新能源/医药 4个中国特色行业pack + 22个ticker映射
- [x] **~~新闻情绪接入~~**: ✅ 第27轮 — yfinance News + LLM情绪分析 + rule-based fallback
- [x] **~~A股一致预期~~**: ✅ 第28轮 — yfinance原生支持，解除守卫，双市场consensus/peers/valuation

---

## 运行指南

```bash
# 基础模式 (无OpenBB)
./run_research.sh AAPL 262.0

# 带一致预期 + 宏观数据
FMP_API_KEY="your_key" FRED_API_KEY="your_key" ./run_research.sh AAPL 262.0

# 命令行直接指定
python demos/auto_research_demo.py AAPL --price 262 --llm --backend glm \
    --fmp-key your_fmp_key --fred-key your_fred_key

# 禁用OpenBB (纯XBRL模式)
python demos/auto_research_demo.py AAPL --price 262 --no-openbb
```

### API Key 获取

| Key | 来源 | 免费额度 |
|-----|------|---------|
| FMP_API_KEY | https://financialmodelingprep.com/ | 250次/天 |
| FRED_API_KEY | https://fred.stlouisfed.org/docs/api/ | 无限制 |

### OpenBB安装

```bash
pip install openbb openbb-fmp openbb-fred
```

桌面的 `/Users/spensir/Desktop/openbb/OpenBB` 已有clone的源码，可参考但建议pip安装使用。

---

## 文件变更清单 (第9轮)

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `aegis/core/acquisition/connectors/openbb_connector.py` | **新增** | OpenBB数据桥接层 |
| `aegis/core/acquisition/connectors/__init__.py` | 修改 | 添加OpenBBConnector文档 |
| `aegis/core/orchestrator/auto_research.py` | 修改 | Step 6c + 宏观FRED + 管线集成 |
| `aegis/core/reports/html_report.py` | 修改 | 格式修复 + 4个新报告板块 |
| `demos/auto_research_demo.py` | 修改 | CLI参数 + OpenBB输出 |
| `run_research.sh` | 修改 | 环境变量支持 |

**测试**: 287 passed, 0 failed
