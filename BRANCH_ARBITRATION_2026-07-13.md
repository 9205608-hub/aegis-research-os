完整请求被截断了，我先读完整 prompt 拿到 B 支 diff 与细节，再按你要求的 7 节做对抗性裁定（只评审、不改代码）。# 对抗性裁定：A 支 vs B 支（L4 置信度分叉）

**裁定官立场**：不吹捧。两支都在同一正确方向上（观察框架诚实化），但一个在**旧尺子上真的挪了 0.25 分**，一个在**新尺子上挪了 0.3 分却把轨迹弄脏了**。整合目标是取长，不是平均平庸。

---

## 1. A 支优劣

### 工程维度

| | 优势 | 劣势 |
|---|---|---|
| **1** | **与决策引擎同源**：`_synthesis_evidence_gap_hits` 直接调 `evidence_gap_hits` + `EVIDENCE_GAP_CONFLICT_THRESHOLD`，合成期 strict 与 B3 判定对齐，避免「标签说 gap、清洗不知 gap」。位置：`thesis_synthesizer.py` R6-1。 | **gap 检查发生在 LLM 吐字之后**：blob 扫的是 `raw` 已生成字段。注入面（R6-3）只靠 openq 计数，真正的「主张与缺口重叠」仍靠事后 scrub——和 R1「清洗打不赢措辞变异」同构。 |
| **2** | **`merged_open_questions` 是真双源**：合成输出在前、orchestrator 追问补后、去重 + `OPEN_QUESTION_CAP=20`；形态计数 / 合约字段 / reason 同源。位置：`persistence.py` + `auto_research.py` 形态派生。 | **blob 字段拼接在两处复制**（synthesizer + orchestrator 的 `_edge_blob_pf`），阈值/字段漂移风险未收口到单一 helper。 |
| **3** | **评测工程诚实**：`r5_pipeline.sh → eval_pipeline.sh` 参数化轮号，冻结 thesis 尺可比；re-synth 补传 `open_questions` 修真实死路径。 | **确定性兜底面窄**：`_magnitude_evidence_gap_disclosure` 只换 `variant_magnitude`；软话术（「显著低估/修复空间巨大」）、`counter_thesis` 裸数字、驱动树错配不在本支射程。 |

### 方法论维度

| | 优势 | 劣势 |
|---|---|---|
| **1** | **「降级诚实」可被旧尺子识别**：失配/gap 同族 deterministic disclosure，审计奖励「不装成可下单 thesis」。R6 均分 3.25→**3.50**、无单票&lt;3.0，是**同一仪器**上的真改善。 | **注入策略偏禁令式**（`observation_framing_block`：「勿开目标价/上行%」），不定义观察框架**应长成什么样**。禁令式 prompt 在措辞军备中脆弱。 |
| **2** | **触发语义正确**：evidence-gap =「核心叙事引用自承未知」；比「问题条数多」更贴近投研诚实。 | **R6-3 阈值 `DOWNGRADED_OPEN_QUESTION_THRESHOLD=3` 过松且与 gap 触发脱节**：openq≥3 就注入，但 strict/magnitude 要 gap-hits≥引擎阈值——可能「说了观察框架话」却未降幅度，或反之。 |
| **3** | **产品标签与执行开始闭合**：比亚迪首句从「135.52/+50.6%」→「若经营利润率…」是内容层证据，不是 meta 粉饰。 | **仍未形成「观察框架产物定义」**：监控合约价值、判别检验、双假设结构缺席；残敌正是「标签对了、研究产品形态仍半 thesis」。 |

**一句话**：A 是「决策后确定性执行」的扎实工程支，测量干净，但生成形态仍偏「禁止坏句」而非「生产好框架」。

---

## 2. B 支优劣

### 工程维度

| | 优势 | 劣势 |
|---|---|---|
| **1** | **生成前形态驱动**：`meta_facts["__provisional_product_form"]` 在 synthesizer 之前盖章；`valuation_constraint_block` 用 F1–F5 取代仅 mismatch 的 rule-7。位置：`auto_research.py` ~4065、`thesis_synthesizer.py`。 | **provisional 触发过粗**：`mismatch OR len(open_questions)≥8`。openq 是 agent 倾倒量，非「主张证据闭合度」——高产追问的 published 票可被误打成框架。 |
| **2** | **最终真源合流 `product_form`**：`STANDALONE_OPEN_QUESTION_THRESHOLD=8` 与 provisional 单向兼容（provisional=框架 ⇒ 最终不得漂回论点标签），避免 F1–F5 叙事 vs 论点标签撕裂。 | **R6-2 是 fallback 不是 merge**：`primary or _extract(fallback)`——synth 有 2 条、orch 有 16 条时**只留 2 条**。R5 点名的「reason 16 vs 字段空」只修了半边；A 的 merge 严格更优。 |
| **3** | **审计脚本可测形态**：`FORCE_RUBRIC`、pass 后缀、`product_form` 路由尺子——工程上把「仪器」做成可切换配置。 | **无合成期 gap strict、无 magnitude 确定性替换、无 re-synth openq 修复**。F1–F5 失败时，目标价/上行% 仍可能直达合约。 |

### 方法论维度

| | 优势 | 劣势 |
|---|---|---|
| **1** | **F1–F5 是正确定义**：中心问题 + 双竞争假设 + 判别检验 + 条件幅度 + 无 edge 宣称 + 披露时间表——这才是「观察框架」产品，不是「降调后的多头 memo」。宁德首句形态是真产品进步。 | **缺确定性执行层**：方法论先进、执法薄弱。R1 教训对 B 同样适用：prompt 结构赢不了顽固目标价话术。 |
| **2** | **双轨尺子诊断正确**：thesis 尺第 3/6 节结构性惩罚观察框架（要 analytical edge、发布匹配）——B 文档「提及率 1/15」是仪器错配的诚实记录，不是借口编造。 | **把诊断直接变成 KPI 换轨**：框架尺均分 **3.87** 与历史 3.x 轨迹不可加。见第 3 节。 |
| **3** | **扣分面转向可工程项**（阈值三源、主指标进 must_monitor、闭合时间表）——说明框架尺在测「监控合约质量」，方向对。 | **开放问题密度≠证据缺口**：≥8 是运维启发式，不是研究认识论。A 的 gap-hits 在「是否把未知当事实」上更严。 |

### 专项对决

**provisional（openq≥8）vs A 的 gap-hits 同源触发——谁更可靠？**

| 判据 | 胜者 | 理由 |
|---|---|---|
| 认识论正确性 | **A（gap-hits）** | 惩罚的是「叙事把 open_question 当事实」，不是「问题很多」。 |
| 生成前可用性 | **B（密度）** | gap-hits 需要 thesis 文本；合成前只能用 mismatch / openq 密度 / 发布门信号。 |
| 假阳性/假阴性 | **A 更稳** | openq≥8 对「认真拆问题的票」误伤；gap 对「话少但瞎断言」更敏感。 |
| **裁定** | **分层，不二选一** | **生成前 provisional** = mismatch ∨ openq≥`DOWNGRADED`(3) ∨（可选）≥`STANDALONE`(8) 强标签；**生成后执法** = gap-hits≥引擎阈值 → strict + magnitude 替换；**最终 form** = 完整 `derive_product_form`（含 gap 真计数 + standalone 8 + review 状态）。B 的 8 可作最终形态补刀，不可作唯一生成前真理。 |

**F1–F5 结构化生成 vs A「注入禁令 + 确定性清洗」——谁更能抗 LLM 军备？**

- **生成结构：B 胜。** 正规定义输出形状，比「不许写目标价」更难被同义改写绕开。
- **残余执法：A 胜。** 军备竞赛的终局永远是 deterministic scrub + 字段级替换；prompt 只是第一道。
- **单独用任一支都会输**：只 F1–F5 → 软目标价漏网（A 实测残敌会重演）；只 scrub → 首句可能洗掉数字但仍是半 thesis，审计仍打「执行不彻底」。
- **裁定：F1–F5 主生成 + A 的 gap/mismatch 确定性兜底，缺一不可。**

---

## 3. 测量学裁定（最重要）

### 3.1 双轨尺子是「仪器匹配」还是 Goodhart？

**两者都是，比例约 60/40：仪器匹配为主动机，Goodhart 为已发生风险。**

**仪器匹配成立的部分**
- 观察框架的职责不是 analytical edge / 可下单匹配度；用 thesis 尺第 3/6 节打 OF，是**考卷与答卷品类错配**。
- B 自己的对照：框架尺 3.87 vs 同产物 thesis 参照 **3.40**——说明分数差主要来自尺子，不是内容一夜飞升半档。
- 框架尺极差更稳、扣分可工程化——说明新尺测到了真实合约质量维度。

**Goodhart 已发生的部分**
- KPI 轨迹写成「…→3.57→**3.87**」却混用不同仪器，是**换考卷抬分**。
- 历史目标「均分 ≥6」若在 thesis 尺上设，则框架尺 3.87 **不得**记入该轨迹的突破。
- `FORCE_RUBRIC` 存在意味着分数可选轨——没有强制「主 KPI 锁 thesis、副 KPI 锁 framework」的制度护栏时，组织行为会自然漂向高分尺。

### 3.2 3.87 与 3.50 能否进同一条 KPI 轨迹？

**不能。禁止相加、禁止接龙。**

| 分数 | 尺子 | 含义 | 可进哪条轨迹 |
|---|---|---|---|
| A R6 **3.50** | 冻结 thesis 尺（07-11） | 作为「可引用投研产物」在旧标准下的进步 | **主 KPI 轨迹**（与 R1–R5 可比） |
| B 框架尺 **3.87** | FRAMEWORK_RUBRIC_V1 | 作为观察框架/监控合约的质量 | **副 KPI / 形态 QA 轨迹**（从本轮新建） |
| B thesis 参照 **3.40** | 同 thesis 尺 | 与 A 最接近的对照 | 主 KPI 的旁证（单次采样噪声大） |

A 的 +0.25（3.25→3.50）是**主轨迹上可信增量**。B 的 +0.30（3.57→3.87）是**副轨迹首点**，不是主轨迹突破。

### 3.3 为何 A 称 5/5 形态裁定、B 称 1/15 提及？

**不是「A 形态更好、B 形态被无视」这么简单。推断差异来源：**

1. **问的问题不同**：thesis 尺第 6 节「决策匹配度」会主动逼审计写「形态是否匹配」→ A R6 判词 5/5 出裁定行。B 的「1/15」指：在**未换尺**时审计员**打分时无视形态声明**（仍按 thesis 内容扣），不是「没看见 product_form 字段」。
2. **采样定义不同**：A 是 5 票×形态句出现率；B 是 15 份审计（5×3）里对形态声明的**实质性采纳率**——分母与事件定义都更严。
3. **产物阶段不同**：A R6 已有 gap 清洗 + 首句改写，形态与内容更对齐，第 6 节更好写「匹配」。B 在换尺前内容仍偏 thesis 形状，审计选择**忽略标签、按正文打**——这正是 B 换尺的动机，也证明标签层 ≠ 内容层。

**哪个测量更可信？**
- **形态是否被「接受」**：A 的 5/5 在 thesis 尺第 6 节下更可复核，但是**标签认可**，不是分数含义改变。
- **产物是否变好**：A 的 3.50（冻结 thesis + 2 次均值 + 比亚迪首句质变）**主轨迹更可信**。
- **OF 产品质量**：B 框架尺更可信，但**不能替换**主 KPI。

### 3.4 KPI 应挂哪把尺？

**主 KPI 必须挂冻结 thesis 尺（07-11）。**

理由（CTO 级）：
1. 平台目标「可用于真实投研」在评测集上原先就是按 thesis 可信度定义的；换尺 = 改目标函数。
2. L4 的价值主张是「数据差时不装 thesis」——若换尺后分数上涨，分不清是**更诚实**还是**考得更松**。
3. 观察框架应在 thesis 尺上**少被形态性误杀**，但仍应因「无 edge、无幅度、软推荐」拿中低分——这是诚实定价，不是 bug。主 KPI 卡住 ~3.5 逼你做 L1 数据闭合，而不是在框架尺上刷到 4+ 自我安慰。

**副 KPI（强制并行）**：对 `product_form==observation_framework` 的票，另报 FRAMEWORK_RUBRIC_V1 均值；目标可另设（如框架票 ≥5、阈值三源一致率 100%）。  
**跨轨诊断**：保留 B 的 thesis 参照采样（FORCE_RUBRIC=thesis），但标注 n 与极差，不进主均值。

**红线**：任何把 3.87 写进「五轮总轨迹」接龙的文档，视为测量事故。

---

## 4. 冲突清单（逐文件「取谁」）

| 文件 / 点 | 冲突形态 | 判决 |
|---|---|---|
| `thesis_synthesizer.py`：注入 | A：`observation_framing_block`（禁令+待验假设）；B：`valuation_constraint_block` 内 F1–F5（provisional/mismatch） | **取 B 的 F1–F5 为主体**；A 的「禁止首句目标价/上行%」压成 F1 的硬约束 bullet，**删独立 `observation_framing_block`** 避免双段重复。 |
| `thesis_synthesizer.py`：合成后执法 | A：gap-hits → strict + `_magnitude_evidence_gap_disclosure`；B：无 | **取 A 全套**；mismatch 与 gap 同族 `elif` 链保留。 |
| `auto_research.py`：形态 | B：生成前 `__provisional_product_form`；A：决策后 gap 真计数 + `merged_open_questions` 喂 `derive_product_form`；A 修 re-synth `open_questions` | **两段都取**：provisional（B）在 synth 前；final form（A 的 gap 真计数 + merge 计数）在决策后；re-synth 传参取 A。 |
| `persistence.py`：R6-2 | A：`merged_open_questions` + `extra_open_questions` + CAP；B：`_open_questions(..., fallback)` + `open_questions_fallback` | **取 A（merge）**；丢 B 的 or-fallback。API 名单用 `extra_open_questions`（语义是并集不是失败回退）。 |
| `product_form.py` | A：reason 措辞「系统性缺口 N 处」；B：`STANDALONE_OPEN_QUESTION_THRESHOLD=8` | **两边都取**：措辞 A + standalone 阈值 B。 |
| `scripts/*pipeline*` | A：`eval_pipeline.sh` 参数化；B：无对等增量（基点已有 r5） | **取 A 的 `eval_pipeline.sh`**。 |
| `scripts/grok_audit_stock.sh` | B：双轨 FRAMEWORK_RUBRIC_V1 + FORCE_RUBRIC + pass 后缀；A：保持单 thesis 尺 | **取 B 的双轨实现**，但**默认汇总脚本主 KPI 只读 thesis 尺产物**（见蓝图）。 |
| 测试：`test_product_form.py` vs `test_confidence_overhaul.py::TestArtifactFormL4` | A 测 merge/framing/gap；B 测 F1–F5/provisional/fallback/standalone | **合并进 `test_product_form.py`（或统一 confidence 套件）**；fallback 用例改写成 merge 语义；保留 F1–F5 与 standalone 断言。 |
| 命名：`extra_open_questions` vs `open_questions_fallback` | 同问题两 API | **`extra_open_questions` + `merged_open_questions`**（并集真源，防 synth 非空时丢 orch 清单）。 |

---

## 5. 整合蓝图（以谁为底、port 什么）

### 底支选择

**以 A 为整合底支（continuation 语义：测量诚实 + 确定性执法 + merge 双源）。**  
从 B port：**F1–F5、provisional 盖章、standalone≥8、双轨审计脚本（副 KPI）。**  
丢：B 的 fallback-R6-2、A 的独立 framing 长文（并入 F1–F5）、任何把框架尺接进主轨迹的文档叙事。

### 文件级操作序列（精确到函数）

1. **`aegis/core/thesis/persistence.py`**
   - 保留 A：`merged_open_questions`、`OPEN_QUESTION_CAP`、`build_thesis_contract(..., extra_open_questions=)`。
   - 删除 B 对 `_open_questions` 的 fallback 签名污染；`_open_questions` 只抽 synthesizer。
   - 合约字段 = `merged_open_questions(st, extra)`。

2. **`aegis/core/thesis/product_form.py`**
   - 保留 A 的 gap reason 措辞。
   - Port B：`STANDALONE_OPEN_QUESTION_THRESHOLD = 8` 及 `elif n_open >= 8` 分支（在 review 状态之后、downgraded∧≥3 之前或按 B 的 elif 链，**保持 provisional⇒最终单调**）。
   - 导出阈值常量供 orchestrator provisional 复用，**禁止魔法数 8 散落**。

3. **`aegis/core/orchestrator/auto_research.py`**
   - **Synth 前** Port B 逻辑，但阈值收紧为共享函数，建议：
     - `provisional = observation_framework` if `mismatch or len(oq) >= STANDALONE_OPEN_QUESTION_THRESHOLD`  
       （可选：`or len(oq) >= DOWNGRADED_OPEN_QUESTION_THRESHOLD and publishing 已偏弱`——若要更激进再开，默认先与 B 对齐 8，避免 3 导致过度 F1–F5）。
     - 写入 `meta_facts["__provisional_product_form"]`。
   - **Synth / re-synth**：传 `open_questions`（A 的 re-synth 修复）。
   - **决策后 form**：A 的 `_gap_n = len(evidence_gap_hits(...))`（过阈值才计入）+ `open_question_count=len(merged...)`；`build_thesis_contract(extra_open_questions=open_questions)`。
   - 抽私有 helper `_edge_claim_blob(thesis_or_raw) -> str`，消灭双处字段列表复制。

4. **`aegis/core/chief_analyst/thesis_synthesizer.py`**
   - **重写 `valuation_constraint_block` 形态段** = B 的 F1–F5，触发：  
     `__provisional_product_form == observation_framework or sanity.mismatch`  
     并在 F1 下追加 A 的硬禁令一句：core_thesis **首句**禁止 fair value / 目标价 / 上行下行%。
   - **删除** `observation_framing_block`（功能被 F1–F5 吸收）。
   - **保留** A：`_synthesis_evidence_gap_hits`、`_magnitude_evidence_gap_disclosure`、`strict=_mismatch or _gap_observation` 链。
   - 可选增强（整合时一次做掉）：gap/mismatch 时对 `core_thesis` 首句若仍匹配目标价/上行% 模式，deterministic 降级前缀（比纯 scrub 百分比更贴审计「执行不彻底」）——属增强，非两支已有，标 P1。

5. **`aegis/core/thesis/__init__.py`**
   - 导出 `merged_open_questions`（A）；不导出 fallback API。

6. **`scripts/eval_pipeline.sh`**
   - 取 A；扩展环境变量：`AUDIT_RUNS`、`PRIMARY_RUBRIC=thesis`（默认）、可选并行 framework 审计目录 `logs/grok_audits_round{N}_fw/`。

7. **`scripts/grok_audit_stock.sh`**
   - Port B 双轨正文；**默认行为**：`product_form==observation_framework` 时仍可跑 framework 尺，但  
     - `audit_scores.py` 主汇总只聚合 `FORCE_RUBRIC=thesis` 或「主轨迹目录」；  
     - framework 分写入旁路 summary。  
   - 避免「默认跟着 product_form 换尺 → 主 KPI 被污染」。

8. **`scripts/audit_scores.py`**（若需小改）
   - 支持 `--rubric thesis|framework|both`；打印两行均值，禁止静默混合。

9. **测试**
   - 合并 A 的 `TestMergedOpenQuestions` / `TestSynthesisGapEnforcement` 与 B 的 `TestArtifactFormL4` F1–F5 / standalone。
   - 删 B 的 fallback-only 用例或改成「synth 非空 + extra 更多 → 并集」。
   - 增加：provisional=framework 且最终 `derive_product_form` 在 openq≥8 时不得为 investment_thesis（单调性）。

### 命名二选一

| 冲突名 | 选用 | 理由 |
|---|---|---|
| `extra_open_questions` vs `open_questions_fallback` | **`extra_open_questions`** | fallback 暗示互斥；实际要并集。 |
| `observation_framing_block` vs F1–F5 段 | **F1–F5 嵌在 `valuation_constraint_block`** | 单入口、单触发、可测。 |
| `artifact_form`（B 已弃） | **只认 `product_form`** | 基点已统一。 |

---

## 6. 整合后验证清单

### 必须跑的自动化

| 项 | 理由 |
|---|---|
| `tests/unit/test_product_form.py` + confidence 中 L4/R6 相关类 | merge、standalone、gap reason、F1–F5 块、provisional 触发 |
| 决策引擎 `evidence_gap_hits` 相关单测 | 合成期镜像与 B3 阈值漂移回归 |
| 合约构建：synth 空 / 仅 extra / 双源重叠 / CAP | R5 meta 冲突回潮是最高频事故 |
| re-synth 路径（至少单测 mock 参数断言 `open_questions=`） | A 修的死路径，整合时易再丢 |

### 必须重跑的评测轮

| 轮 | 配置 | 理由 |
|---|---|---|
| **整合冒烟** | 1 票（建议比亚迪）× thesis 尺 × 2 采样 | 验证首句/magnitude/合约 openq 非空 |
| **主 KPI 轮 R_int** | 5 票 × **冻结 thesis 尺** × ≥2 采样（与 A R6 同仪器） | 唯一可宣告「平台是否超过 3.50」的轮次 |
| **副 KPI 轮** | 同产物 × framework 尺 × 3 采样 | 建副轨迹基线；**不与 3.50 比大小** |
| **跨轨诊断** | OF 票 FORCE_RUBRIC=thesis 各 1 次 | 确认「换尺溢价」幅度，防自我欺骗 |

### 整合可能引入的回归点

1. **F1–F5 + gap scrub 双重改写**：`variant_magnitude` 被 F3 条件句生成后又被 disclosure 整段替换——可接受，但审计可能看到「叙事 F3 风格 vs magnitude 模板」风格断裂；需确认 scrub 字段集合一致。  
2. **provisional=框架 但最终=论点**（若 standalone 与 gap 都未触发、仅 openq 在 3–7 且 published）：F1–F5 叙事 + thesis 标签 = 新撕裂。必须保证 provisional 触发 ⊆ 最终 form 触发（单调性测试）。  
3. **openq≥8 误伤高质量追问票**：主 KPI 可能短期下跌——这是诚实波动，不是 bug；用副 KPI 看框架质量是否上升。  
4. **双轨默认污染**：若 `grok_audit_stock.sh` 默认按 product_form 换尺且 `audit_scores` 不分目录，主轨迹会假性上涨——**合并前先锁汇总逻辑**。  
5. **严格 scrub 过杀**：gap 票合法情景价（模型诊断）若未在白名单，可能误伤；对齐 A 已有 `_mfi_pcts` 白名单路径。  
6. **驱动树/软话术/裸数字**：两支都没修，整合后主 KPI 仍可能卡在 ~3.5–3.7——别把残敌算成合流失败。

---

## 7. 一句话总裁定

**A 是更诚实的工程与测量支，B 是更正确的产品形态与生成定义支；主 KPI 锁 A 的 thesis 尺，生成定义 port B 的 F1–F5，执法层用 A 的 gap 同源 deterministic 降级——双轨尺子只做副仪表，谁把 3.87 接进主轨迹谁在做 Goodhart。**

**若只能活一支：活 A。**  
因为在「均分 ≥6、冻结外部质检」的游戏规则下，**不可比的高分比偏低的真分更危险**；A 已在同一尺子上证明内容可动（3.25→3.50、比亚迪首句质变），缺的 F1–F5 可后补，而 B 先换尺再报突破，会把整个置信度项目的反馈回路烧坏。B 的思想遗产（F1–F5、双轨诊断、standalone 单调）必须 port，但**仓库底权给 A**。
