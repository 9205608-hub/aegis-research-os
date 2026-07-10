# Aegis Research OS — 智能投研助手

## 项目一览

端到端自动化股票研报生成系统。从 ticker 出发，拉财报数据 → 归一化 → 计算指标 → DCF 估值 → 7 位 LLM 智能体分析 → 批评审核 → 发布门槛 → 论点合成 → 报告编辑 → HTML 输出。

**数据覆盖**: 美股 (SEC EDGAR XBRL) + A 股 (akshare / eastmoney / yfinance fallback)
**LLM 后端**: DeepSeek (默认) / Grok / subprocess (claude CLI)
**入口**: `./run_research.sh <TICKER>`

## 开工前必读

**每次新会话开始前，先 Read 下面这两个文件**：

1. **[HANDOFF.md](HANDOFF.md)** (项目根目录，**不是** `.claude/HANDOFF.md`) — 完整的 bug 追踪历史和待办清单。
   - 最新条目按日期倒序排在顶部，每条都有：根因 / 修复位置 / 验证结果 / 遗留问题
   - 标红的 **P0** 条目是系统级未完成事项，优先处理
   - **忽略 `.claude/HANDOFF.md`**，那是一份过期的旧副本
2. **`demos/` 目录**下最新修改的报告 —— 快速看输出长什么样

跳过这两个文件直接上手 = 会反复踩已经记在 HANDOFF 里的坑。

## 重要规则（铁律）

### 🔴 时效性铁律

**所有股票分析必须反映当前可得的最新财报期**。以 2026-04-15 为例：
- 美股：调用 SEC EDGAR probe 自动选最新 10-K（通常 FY2026）
- A 股：乐观尝试 FY(当前年−1)，由 akshare 的真实 `fiscal_year` 回写 `config.period`
- 若数据源落后 > 15 个月，报告顶部必须显示橙色时效性警告 banner

违反这条铁律 = 报告里写 FY2024 分析 NVDA $189 股价 = 前提全错。详见 HANDOFF BUG-21。

### 🔴 中文化铁律（A 股）

A 股报告**所有**自然语言内容必须是简体中文，包括：
- Agent 叙述、合成器 thesis、Report Editor headline、情景 narrative
- HTML 模板 label、表头、按钮、footer、页面 title
- 唯一可保留英文：国际通用缩写 (ROIC, WACC, EBITDA)、产品名 (Aegis)

新增任何 LLM agent 时，必须把 macro_context 里的 `language="zh-CN"` 指令透传。[详见 llm_agent_base.py:466 附近]

### 🔴 不要轻信 LLM 输出的数字

LLM 会在叙述里编算术式不闭合的数字（例："净负债 47 亿 = 总债务 75 亿 − 现金 15 亿"，75−15=60≠47）。agent prompt 里已加 NUMERIC CONSISTENCY 硬约束，但仍需 critic 兜底。

## 关键文件地图

| 路径 | 作用 |
|---|---|
| `run_research.sh` | 入口脚本，设置环境变量 |
| `demos/auto_research_demo.py` | CLI 参数解析 |
| `aegis/core/orchestrator/auto_research.py` | **主流程 (3500+ 行)**，所有步骤编排 |
| `aegis/core/acquisition/connectors/` | 数据源：cninfo / akshare / openbb / sec |
| `aegis/core/market_adapter/` | CAS (中国会计准则) ↔ US GAAP 概念映射 |
| `aegis/core/acquisition/fact_bridge.py` | adapted → meta_facts 归一化 + 衍生字段 |
| `aegis/core/truth/scenario_engine/dcf_engine.py` | **DCF 引擎**（D&A 比率模型，2026-07-09 修复） |
| `aegis/core/truth/scenario_engine/sensitivity_analyzer.py` | 敏感性分析 |
| `aegis/core/agents/llm_agent_base.py` | LLM agent 基类 + 系统 prompt |
| `aegis/core/agents/llm_agents.py` | 7 位专家 agent 定义 |
| `aegis/core/chief_analyst/` | 首席分析师四件套：director / synthesizer / editor / scenario_architect |
| `aegis/core/reports/html_report.py` | **HTML 报告渲染 (2400+ 行)**，中文 i18n 在此 |
| `scripts/replay_from_cache.py` | 从缓存快速重生成报告（不重跑 LLM） |
| `HANDOFF.md` | **项目日志**，每次开工必读 |

## 常用操作

```bash
# 跑美股报告（自动最新期、实时价）
./run_research.sh NVDA

# 跑 A 股报告（akshare 主数据源，中文输出）
./run_research.sh 301358

# 从 cache 快速重渲染（不重跑 LLM agent，约 1 秒）
export DEEPSEEK_API_KEY="..."  # Editor 需要
python scripts/replay_from_cache.py 301358 --allow-stale
python scripts/replay_from_cache.py 301358 --allow-stale --editor  # 顺带重跑 Report Editor
```

## 环境与依赖

- Python 3.12 (miniforge)
- 关键包: `yfinance` `akshare` `openbb` `pydantic` `pandas`
- LLM: DEEPSEEK_API_KEY / GROK_API_KEY 从环境注入（可放本地专用 `run_research.local.sh`，已 gitignore），不要把真实 key 写进仓库文件
- **网络环境**: 用户在中国大陆 + Clash Verge 代理。`.cn` / `eastmoney.com` 域名需要 bypass 代理 —— `akshare_connector._no_proxy()` 里有处理。`push2.eastmoney.com` 在当前代理配置下仍然不可达（已知遗留问题）
- **LLM 偏好**: 优先用 Claude Max 订阅（SDK/OAuth），不要提议申请新的独立 API key

## 当前系统状态 (2026-07-09)

- ✅ **AUDIT_2026-07 路线图 50 项批量修复落地**（阶段 A-E 全部完成，详见 HANDOFF 2026-07-09 条目）
- ✅ DCF D&A 口径修复（P0）：比率模型 + 终值年 D&A≈capex 守恒，资本密集股 DCF base 下降 20-30%（合成基准 338.87→263.48/股）
- ✅ A 股口径三连：total_debt 补应付债券、净利润切归母、所得税率真实派生（不再固定美国 21%）
- ✅ LLM 恢复链接通（原是死代码，首次失败即退 mock）+ LLM 缓存质量门（降级壳不再投毒）；2026-07-10 起弃用的 Moonshot 后端整体移除，恢复链统一由 DeepSeekClient 承载（GrokClient 继承复用）
- ✅ 性能接线：API 后端 agent 并发 2→4、LLM 磁盘缓存默认开启、超时按 backend 分档（预期 ~40min→~20min，待实测）
- ✅ 测试 673→913 passed（新增 240 个回归用例，补齐 _coerce/html_report_v2/critics 中文路径/DCF 口径四大零覆盖）
- ⚠ `FAST_AGENTS=1`（前 4 个 specialist 降 flash 档，预期再省 ~10min）已接线但默认关——需先在 2-3 个真实 ticker 上 A/B 验证 flash 输出质量
- ⚠ 审计遗留未修 P2（不在路线图内）：宏观 PMI 假数字、peer_fundamentals dataclass 死代码、sina spot 兜底死代码、Editor front_page_numbers 无 scrub、hypothesis_validated 字符串真值、logic_critic 中文利润率关键词、replay_from_cache 敏感性表缺参（新发现）——清单见 HANDOFF 待办
