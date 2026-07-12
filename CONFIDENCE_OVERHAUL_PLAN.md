# 研报置信度整改计划（2026-07-12）

> **目标**：Grok 对抗性审计「可用于真实投研的可信度」从均分 **3.1/10** 提升到 **≥ 6/10**（先在 5 只真 watchlist 票上达标，再全 20 只复审确认）。
> **依据**：2026-07-11 的 20 份独立审计（`logs/grok_audits/`）+ 本日两路代码侦查（全部结论带 file:line）。
> **分支**：`claude/research-report-confidence-f1dac6`（本 worktree）。

---

## 一、诊断：3.1 分不是 20 个问题，是 4 个系统级 bug 的重复投影

Grok 在新易盛审计里给出了关键的评分解剖：**「作为空头问题清单有 6 分素材；作为可下单的 thesis 只有 4 分」**。差距全部在数字诚信与契约可执行性，不在研究方向。四大硬伤及代码级根因如下。

### 硬伤 1｜估值链多真源 + 数量级失配 + override 残留（击穿全部幅度结论）

审计表现：宁德 DCF ¥4000+ vs 市价 ¥349；被 scrubber 删掉的编造公允价残影仍在正文（"¥406"、占位符旁挂着由被删数字算出的下行%）。

代码根因（按危害排序）：

1. **没有"上行方向"的数量级 sanity check**。`auto_research.py:2519-2540` 的估值一致性检查只在 `base < price×0.20` 时告警——**base 是市价 11 倍时静默通过**。terminal_value_gate（`publish_gate/gate.py:511-563`）只拦"高 capex/负 FCF"名字，盈利正 FCF 的宁德完全不触发。
2. **% 清洗在极端失配时自我关闭**。`thesis_synthesizer.py:549-555`：只有当 sanctioned 回报中存在 |r|<90% 时才执行百分比清洗——DCF 失配 10 倍时三档回报全部 |>90%|，**清洗恰好在最需要时跳过**，于是"由被删数字算出的下行%"永远存活。
3. **scrubber 只覆盖 6/12+ 叙述字段**。`_VALUE_CLAIM_FIELDS`（`thesis_synthesizer.py:29-36`）不含 `key_assumption_disagreement / counter_thesis / conviction_narrative / management_quality_summary / capital_allocation_assessment / edge_source` 等，而 HTML 直接逐字渲染这些未清洗字段（`html_report_v2.py:1557-1558, 1911-1918`）。另 `replace(token, _tag, 1)`（`:517`）只替换首个出现。
4. **净负债低估放大股权**。`fact_bridge.py:159-164` 债项映射不全 → net_debt 偏深负 → `equity = EV - net_debt` 虚增（`dcf_engine.py:497`）。
5. **增长路径 30× 封顶后 Gordon 终值仍可爆炸**（`auto_research.py:5457-5472`，注释自证"phantom ¥4475/股"）。
6. **估值数字 11 个来源并存**（DCF 三档/概率加权/reverse-DCF/前沿/敏感性/相对估值/synthesizer 自由发挥/editor 自由发挥/账面锚/dcf_gap），分部 DCF 与 flat 输入两套口径靠比例补丁对齐（`auto_research.py:2347-2359, 2592-2596`）。

### 硬伤 2｜Kill Criteria 名实倒置（风控层不可执行）

审计表现：确认信号写成 kill，真证伪条件挤在 disconfirming；同指标三套阈值；must_monitor 被 TODO 污染。

代码根因——**倒置发生在管道里，不是 LLM 写错**：

1. `decision_engine/engine.py:413-423`：把 risk_analyst 输出的 `disconfirming_triggers` 逐条搬运改名成 **kill_criteria**（threshold 恒为字符串 `"trigger event"`）；而合约的 `disconfirming_triggers` 字段只塞一句 `what_would_change_my_mind` 软叙事（`thesis/persistence.py:322`）。kill/monitor/disconfirm 三者源头同物、下游贴不同标签（`engine.py:425-439` 同源）。
2. `thesis_synthesizer.py:1120-1124`：喂 LLM 时把 disconfirming_triggers 打上 **"Kill Criteria:"** 表头——上下文本身错标。
3. **prompt 从未定义方向**：`llm_agent_base.py:84-95` 的 schema 对 disconfirming_triggers 连语义说明都没有；全仓无"kill 必须是证伪方向"的生成后校验（唯一相关检查 `evidence_critic/critic.py:163-175` 只查非空）。
4. **must_monitor 污染是设计使然**：`thesis/monitorables.py:289-292` 显式把 `open_questions` 列为 must_monitor 构建来源，挂不上目录就降级成"人工关注：<原问题>"（`:263-268, 374-376`）。
5. 三套阈值机制：monitorables 目录阈值 / kill 恒 `"trigger event"` / agent 自由文本，无对齐去重。

### 硬伤 3｜结论跑在证据前（edge 无闸）

审计表现："信息缺口的位置正好是 edge 声称的位置"——open_questions 自承未知的数据在 core_thesis 里当已证实事实。

代码根因：**这道闸完全不存在**。

1. publish gate 的 context 不含 open_questions（`auto_research.py:3954-3968`），13 个门无一读取；decision engine 收到了但只存不判（`engine.py:231`）。open_questions 对发布决策的门控权 = **0**。
2. 全仓 grep 无任何 "edge × open_questions 交叉引用检查"；edge 字段由 synthesizer 产出后直升 EdgeAssessment（`engine.py:144-170`），无一步校验。
3. confidence 是纯规则打分（`engine.py:448-516`）：起始 70，`-blocks×2 -warns×0.5 +证据条数`，published 不封顶——**证据"条数"够就 high，与证据"闭合度"无关**。

### 硬伤 4｜决策门双向失守 + 不可复现

审计表现：2 只 published（新易盛/沈飞）恰是最不该发的；茅台同价同天校准轮 published/high、demo 轮 blocked/low。

代码根因：

1. **方差源**：唯一 LLM critic（llm_judge）经 DeepSeek 客户端时温度被硬编码 0.2（`deepseek_client.py:203`，无视 `config.py:35` 的 role=critic 0.0），无 seed、无投票、单次调用（`auto_research.py:3932-3948`）；而 critic_gate 阈值 = 1（`gate.py:55-69, 203-223`）——**一条非确定性 block 翻转整个发布决策**，blocked→conf 封顶 low、published→可 high，全链联动。
2. **误 publish 通道**：数据缺失时 terminal_value_gate / capex_attribution_gate / dcf_integrity_gate 全部 skip-as-warn 放行（`gate.py:517-523, 414-419, 489-494`）——缺数据反而更容易 published。
3. 附：合约 `bias_check_status` 硬编码 `"passed"`（`persistence.py:335`），与真实 bias 计算脱钩。

---

## 二、为什么 6/10 可达（而且不需要"研究变聪明"）

1. **Grok 自己给出了上限证据**：新易盛"问题清单 6 分素材"；绿的谐波"体制判断与隐含增速冲突有 4-5 分信息量"；恒瑞"作风险清单有 4-5 分材料"。**素材分普遍 5-6，被数字硬伤拖到 2-4**。修掉硬伤 ≈ 分数向素材分收敛。
2. **扣分三巨头全部可确定性修复**：估值数字不可信（硬伤 1）、风控不可执行（硬伤 2）、过度承诺（硬伤 3）都是代码/管道 bug，不是 LLM 能力问题。
3. **Grok 奖励诚实**：20 份审计反复强调"blocked 决策本身是诚实/正确的"。当 DCF 失配时诚实降级幅度表述（"方向性假设"替代"70% 下行"）、当关键数据未闭合时收敛 edge 措辞，都是加分项而非减分项。
4. **可审计性是免费分**：Grok 反复抱怨"DCF 点位像黑箱、无假设表"——因为 thesis 合约里根本没有 WACC/g/年限/股本假设附录。把 sanctioned 估值的假设表写进合约 = 直接可审计。
5. **诚实预期**：部分扣分（分部收入、客户集中度等 A 股非强制披露数据）无法闭合——所以 20 只全部 ≥6 不现实。KPI 定为 **5 真票均分 ≥6 且无单票 <5**；这是估计值，需评测循环实测校准。

---

## 三、整改路线（三阶段，验收 KPI = Grok 复审分）

### Phase A｜估值与文本诚信（确定性代码修复，不动 LLM，预估 +1.5~2 分）

| # | 修什么 | 位置 | 验收 |
|---|---|---|---|
| A1 | **双向估值 sanity gate**：\|DCF base/price − 1\| 超阈值（建议 3×）→ variant_magnitude 强制降级为方向性表述，禁止输出目标价/下行% | `auto_research.py:2519-2540` + 新 gate | 宁德 replay 不再出现 ¥4000 系数字 |
| A2 | **修 % 清洗自关闭 bug**：sanctioned 回报全部 \|>90%\| 时应**更严格**清洗而非跳过 | `thesis_synthesizer.py:549-555` | 回归测试：10× 失配样例的 % 全清 |
| A3 | **scrubber 全字段覆盖 + replace_all**：清洗集扩到全部叙述字段（含 editor 输入链） | `thesis_synthesizer.py:29-36, 517`；`report_editor.py:248-251` | grep 报告无未 sanctioned ¥ 数字 |
| A4 | **sanctioned 估值假设表进合约**：WACC/g/预测年限/终值占比/股本/净负债一张表，随 thesis 落库进报告 | `persistence.py` + `html_report_v2.py` | Grok 复审"黑箱"类抱怨消失 |
| A5 | net_debt 债项映射补全复查 + 终值占比披露 | `fact_bridge.py:159-164` | 净现金公司股权桥可勾稽 |

### Phase B｜契约语义与决策门（预估 +1~1.5 分）

| # | 修什么 | 位置 | 验收 |
|---|---|---|---|
| B1 | **kill/disconfirm 管道正名**：risk_analyst 触发器按语义方向分流（证伪→kill、确认→confirming、观察→monitor），修 synthesizer 错标表头；prompt 补方向定义 + 生成后语义校验 | `engine.py:413-439`；`thesis_synthesizer.py:1120-1124`；`llm_agent_base.py:84-95` | Grok 复审无"装反"类 P0 |
| B2 | **must_monitor 与 open_questions 分离**：open_questions 不再进监控清单，watch_only 降级项单独成"研究待办"区块 | `monitorables.py:289-292` | 监控清单无 TODO 化条目 |
| B3 | **evidence gate（新闸）**：core_thesis/my_variant/edge_source 引用的实体命中 open_questions → edge 不得升格、confidence 封顶 medium、published 需关键 open_questions 为空 | 新模块 + `engine.py:144-170, 448-516` | 新易盛类"缺数据仍 high"不再出现 |
| B4 | **决策门重标定**：critic 温度传递修复（role=critic 强制 0.0）+ block 级 finding 复跑确认（2/2 复现才计入）；数据缺失 skip-as-warn 路径改为"缺数据不得 published+high" | `deepseek_client.py:203`；`gate.py:203-223, 517-523` | 同票同日重跑 3 次决策一致 |
| B5 | bias_check_status 接真值 | `persistence.py:335` | 合约字段与 engine 计算一致 |

### Phase C｜评测收敛循环（KPI 闭环，这才是"loop"该用的地方）

**评测集（4+1 只，各代表一类硬伤）**：
- 300750 宁德（数量级失配代表，2026-07-11 得 3.0）
- 300502 新易盛（误 publish 代表，4.0）
- 600519 茅台（决策方差代表，3.5）
- 002371 北方华创（kill 装反代表，3.0）
- 002594 比亚迪（最低分综合案例，2.0）

**循环体**（每轮成本 ≈ $1 以内 + Grok 订阅额度）：
1. 修复落地 → `replay_from_cache`（输出层修复秒级验证）或真 run（prompt/critic 类修复，~$0.15/票）
2. SuperGrok CLI 用**冻结的同一份审计 prompt** 复审 → 提取 0-10 分
3. 未达标 → 按新扣分点回到修复 → 下一轮
4. **达标判据**：5 票均分 ≥6 且无单票 <5，连续两轮稳定 → 全 20 只复审确认

**防 Goodhart 三条**：审计 prompt 冻结不改；不为讨好审计员改写措辞（只修数字诚信与契约语义）；终极校准是 2026-10-09 的 90 天真实收益回看（Phase 3 postmortem 已接线）。

---

## 四、工具决策：/goal vs /loop

- **/goal**：当前环境不存在这个命令，不可依赖。
- **/loop**：存在，语义是"按间隔重复跑一个 prompt"，适合监控型任务。用它盲跑"重跑→复审"**不会涨分**——均分 3.1 的根因是 4 个确定性工程 bug，同样的 bug 重跑一万次还是 3 分。
- **正确编排**：Phase A/B 是一次性的工程修复（session 内直接做，可用多 agent 并行施工+对抗性复查）；Phase C 的"修复→复审→打分"收敛循环先手动跑 2-3 轮，若需要自动化再用 /loop 挂成自驱循环（此时它才有价值：每轮自动 replay + 审计 + 报分）。

---

## 五、风险与铁律

1. **golden master 闸门**：Phase A/B 触碰引擎/合约装配 → 每步 `scripts/golden_master.py check`；行为预期改变处需重新 record 并在 HANDOFF 记录理由。
2. **校准证据不可移植**：动了估值/决策层后，2026-07-11 的 20 份审计分数只作 baseline，不作回归基准；新分数需同 prompt 重测。
3. **中文化铁律 / 时效性铁律**照旧（CLAUDE.md）。
4. **不碰研究方法论**：预期优先框架、变异质量、监控循环已被审计确认"立住了"，本轮只修数字诚信/契约语义/决策门，不改分析框架。
