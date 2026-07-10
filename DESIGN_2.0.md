# Aegis 2.0 — 时效性智能投研系统 顶层设计

> 定稿日期: 2026-07-10 | 起因: 康达新材(002669) 全量 LLM 报告输出"83% 下行空间"，与市场定价严重脱节，用户质疑框架是否该推倒重来
> 方法: 设计草案 → 三视角对抗性评审（量化研究员 / 务实工程师 / 重建派辩护人，各自独立读码取证）→ 合入全部 major 意见后定稿
> 评审结论: 3/3 sound_with_changes，一致支持"绞杀式改造"而非新开项目

---

## 一、诊断：为什么分析"偏离太远"

对康达新材，系统输出 DCF ¥2.15 vs 现价 ¥13.76 → blocked。这个输出**数学上没错**（公司扣非仅 1672 万、CFO −12 亿、真实失血），但**框架上答非所问**。三个结构性问题：

1. **方法论错位**。单锚 DCF 回答的是"按已实现现金流值多少钱"，而市场定价的是"预期"。对茅台类稳态资产两者收敛，对转型/题材/亏损股结构性发散。系统把发散当"下行空间"输出，等于每次都在宣判市场是错的——这不是研究，是自说自话。
2. **信息断层（时效性的真正含义）**。系统只吃年报。康达 FY2025 年报之后的一切——2026Q1 营收 11.56 亿（增速已明显放缓，实测可得）、业绩预告、并购公告、订单、调研——系统全部失明。A 股一致预期整体被跳过。系统根本不知道"市场为什么给 ¥13.76"。
3. **形态错位**。一次性 ~25 分钟全量报告，观点不持久、不随信息流更新。投研的时效性 = 观点随信息流持续修正，不是每次从零跑一遍。

**重要澄清**（评审纠偏）：康达最终决策是 blocked"暂不评级"（terminal_value_gate 正确拦截），"83% 下行"主要是 Editor 叙事层文案。问题比"系统性宣判市场错误"轻，但叙事框架 + 信息断层是真实的。

## 二、判决：拯救，不新开项目

重建派辩护人为 greenfield 做了最强论证后自我裁决：**绞杀式改造**。真实论据（评审修正后的版本，不是"网络坑不可移植"这种弱论据）：

- **970 个测试是 AI-only 开发模式的唯一护栏**。本项目所有代码由 Claude session 编写，测试护栏是 AI 改码不翻车的唯一保障。重写即作废全部护栏。
- **校准证据不可移植**：DCF 修复经独立数值复核吻合（338.87→263.48）、康达/NVDA/茅台/圣邦四个实盘 ground truth、scrubber 实战调参记录。这些是两个月积累的"系统输出可信"的证据链，重写全部归零。
- **greenfield 的招牌卖点对本部署为零价值**：全新 async 事件架构？全仓零行 async、瓶颈是 LLM 延迟、部署是一台 Mac + 每日定时任务，同步轮询绰绰有余。此条写死在这里，防止未来 session 被"重写成 async"带偏。
- 连接器（5357 行，大陆网络坑的全部积累）实测只依赖 2 个内部模块，**无论走哪条路都可整体搬迁**——它不构成绞杀的理由，但也不构成重写的成本。

**屎山浓度实测**：4.1 万行生产代码中，真屎山集中在 `auto_research.py` 单体（5195 行 43 方法，Step 间靠局部变量传状态）+ `html_report_legacy.py`（2711 行死代码）≈ 15%。其余模块化良好。

## 三、核心理念：三个转变

### A. 估值哲学：从"算公允价值宣判市场"→"反推市场预期评估其可信度"

Expectations investing（Mauboussin 式）。核心产品从"DCF 说值多少钱"变为：

> **"当前价格隐含了什么样的预期？该预期与可验证事实是否相容？证伪它需要看什么信号？"**

落地形态（评审修正后）：
- **预期前沿求解器**（新引擎，`solve_expectations_frontier`）：一个价格反解不出"增速+利润率"两个未知数（不可辨识）。正确做法是**条件化反解**：固定 2-3 档终年利润率情景（维持现状 / 行业中位·从 sector pack 取 / 管理层目标档），每档解一个隐含增速，输出小表：
  `"¥13.76 隐含：若利润率维持 2.9% → 需 XX% 增速；若达行业中位 8% → 需 YY% 增速"`
  求解用 **(g × m) 二维网格扫描 + 变号检测**（DCF 单次求值毫秒级），天然规避一维 bisection 的非单调陷阱（BUG-Y13 根治）。隐含增速附 WACC±1% 三列（复用 sensitivity_analyzer），否则单点精度是假的。
- **DCF 保留为锚之一**：稳态资产仍是主锚。DCF-vs-price 差值**永远展示**——差值本身就是信息。
- **定价体制感知**（不是硬分类器）：以 terminal_value_gate 的既有信号（缺口幅度、FCF 符号、盈利质量）为种子特征，输出**连续权重 + 迟滞带**，只决定叙事框架与验证点清单，**永不抑制估值差的展示**（防循环论证：不许用"估值贵"作为"不谈估值"的理由）。上线前对 ~20 个手工标注 ticker（茅台/长电=稳态、寒武纪=题材、康达=转型…）跑混淆矩阵校验。波动率特征推迟到有历史行情数据后再加。
- **相对估值锚补位**：A 股同业 P/E / P/B 分位（东财行业估值 + tencent 行情驱动，弃用 yfinance peer 路径）。没有这个锚之前，体制感知只改叙事、不改估值输出结构。

### B. 信息架构：单时点年报快照 → 时点数据库（PIT store）

- 数据分层：**慢**（年报/深度）· **中**（季报 TTM、业绩预告/快报、公告、一致预期、龙虎榜/股东户数/两融）· **快**（行情/资金流）。
- **PIT 语义（评审修正后的严谨版）**：双时间戳——`as_of`（系统摄取时间，诚实的 knowledge time）+ `announce_date`（东财披露日字段，economic knowledge time）。历史回填数据显式标 `backfilled=true`。**量化回测级 PIT 只对 announce_date 回填后的数据成立**；摄取时间戳只保证 forward-looking 正确——不夸大"附带收益"。
- 存储：**sqlite3**（标准库零依赖；这台代理+conda 环境少一个依赖是实打实收益；schema 不变日后可换 DuckDB）。表结构**直接翻译已存在的 `AtomicAccountingFact` 死合同**（accepted_at/effective_at/restatement_flag/fact_version 全齐，两个月前就设计好了从未接线）。concept 列绑定已有 `MetricDefinition`/metric_registry，禁自由字符串。
- 业绩预告是区间+类型：`value_low/value_high/forecast_type` 字段，快报打 `unaudited` 标签、正式报告到达时替换。
- **TTM 引擎规格**（A 股特有坑，写死）：季报是**年初累计值**，TTM = FY_prev + YTD_cur − YTD_prev_same；BS 科目取时点值不做 TTM；归母/扣非**双轨**（康达：归母 1.25 亿 vs 扣非 1672 万，差 7 倍）；FY 边界与跨重述窗口各配回归测试；上线前对 2-3 个 ticker 与东财 F10 公示 TTM 做 golden 对账。
- **meta_facts 退役棘轮**（防"永久带病"）：pit 层为新数据唯一事实源；`meta_facts` 降级为 `pit.as_legacy_view()` 单向生成（禁反写）；CI 棘轮测试——允许直接读 meta_facts 的文件白名单**只许缩短**。

### C. 产品形态：一次性报告 → 持久观点 + 事件驱动增量

- **thesis 持久化直接序列化已有 `ThesisContract`**（market_implied_story/my_variant/Monitorable/KillCriterion/版本链字段全齐，又一个未接线的死合同）——不新设计 JSON 结构。版本历史用 append-only JSON 链，**不建正式状态机**（单人项目维护税）。
- **monitorables 封闭目录制**：预置 8-12 个可执行检查器型号（应收增速 / CFO净利比 / 存货增速 / 商誉 / 预告vs预期缺口 / 公告关键词〔并购·减值·订单〕 / 股价偏离阈值…），LLM 生成时**必须选型号+填阈值**（复用 B4 枚举归一化容错）；目录外观察点保留纯文本"人工关注"，不做可执行承诺。
- **调度用 launchd 不用 cron**（Mac 睡眠后补跑）；扫描用**水位线**（since last_seen_announcement_id）天然幂等；delta 复核强制 light 档 + **每日 LLM 预算熔断**（复用 BUG-Y40 成本追踪）；务实兜底——dashboard（server/ FastAPI 雏形已在）打开即触发一轮扫描。SLA 表述为"唤醒后首扫更新"，不承诺 24h。
- **校准闭环**：激活 postmortem_schema（又一死合同）——每份 thesis 90 天后自动回看关键假设兑现情况。这既是产品质量闭环，也是"新框架是否真的更准"的唯一证据来源。

## 四、资产盘点（评审修正版）

| 类别 | 资产 | 处置 |
|---|---|---|
| 直接复用 | acquisition 连接器（网络坑积累）、fact_bridge/market_adapter、truth/（DCF+反推+敏感性，240+ 测试锁定）、llm/ 栈、critics+publish gate、agents 基类 | 抽成内部子包 + import-linter 分层契约（绞杀防腐层 + greenfield 对冲，~1 session） |
| **已设计未接线（纯接线，近零改造成本）** | `ThesisContract`（观点持久化全字段）、`AtomicAccountingFact`（PIT 双时间戳）、`event_schema`、`postmortem_schema`、`MetricDefinition`/metric_registry | Phase 2 直接消费，不重新发明 |
| 需改造 | 7 agent prompt 框架（评估公司→评估预期）、chief_analyst 四件套、reports、rule-based zh 模板叙事框架 | Phase 0 |
| 真屎山 | auto_research.py 5195 行单体 | Phase 4 挂闸拆解（见红线 7） |
| 死代码 | html_report_legacy.py 2711 行 + 锁它的测试 | **Phase 0 第一个 commit 直接删** |
| 判决失实项（评审纠偏） | "JudgmentContract 与报告耦合"——不存在（reports 零引用）；dunder 键方言仅 ~10 键单点生产 | 前者摘除；后者 Phase 2 顺手换 typed FactsContext |

## 五、分期路线图（评审修正版）

### Phase 0 · 方法论纠偏（估 2-3 session）
1. 删 legacy 渲染器（第一个 commit）
2. `solve_expectations_frontier`：(g×m) 网格 iso-price 求解器 + WACC±1% + 单测（含康达参数集：负 FCF、6.4× 价差必须有解）
3. 提前抽 Phase 1 最薄一片：近 90 天公告标题 + 业绩预告一次性拉取（API 已实测可达），注入 macro_context 作"已知催化剂"事实源——否则 LLM 只能幻觉"并购故事"
4. 定价体制感知 v1（连续权重、terminal_value_gate 信号种子、只改叙事）+ 20 ticker 人工校验
5. 报告主轴改造：预期前沿表成为第一公民；评级语义 blocked→"预期无法验证"（8 文件 22 处断言迁移）；rule-based zh 模板同步新叙事
6. **scrubber/critic 白名单扩展**：预期前沿输出注册为 sanctioned numbers（"¥13.76 隐含…"句式与 _FAIR_VALUE_CONTEXT 门的碰撞已知，必须处理）

**验收**：康达报告主结论 = 条件化预期表 + 体制框架 + 验证点清单（标注"未核验"状态——核验能力是 Phase 1 交付物）。不再出现裸"83% 下行"。

### Phase 1 · A 股中频数据层（估 4-6 session，按价值串行交付，每接一源跑康达实盘验收）
顺序：**业绩预告/快报 → 一致预期 → 公告标题流 → 季报+TTM 引擎 → 龙虎榜/股东户数/两融**
- 四条主源已实测可达（2026-07-10：Q1 利润表 5215 行、中报预告 631 条、东财盈利预测 RPT_RES_PROFITPREDICT、巨潮公告）；公告流**主走东财 datacenter 同族接口**，巨潮反爬降为备选
- 一致预期定位为**旁证**：入库带元数据（机构家数/最近报告日/区间宽度），使用门槛 ≥3 家且 ≤90 天，不满足显示"无有效一致预期"；中小盘零覆盖时 reverse-DCF 前沿是唯一预期锚（显式降级路径）
- ~~北向个股持仓~~（港交所 2024-08 起停止披露，DOA）→ 龙虎榜/股东户数/十大流通股东替代
- 新数据**从第一天写入 pit 层**（sqlite3），不先进 meta_facts 再迁移
- 相对估值锚（同业倍数分位）在此期落地

**验收**：报告数据时效差 <90 天；"最新事件"区块显示近 90 天预告/公告；Phase 0 验证点清单变为"已核验"状态。

### Phase 2 · PIT 库定型 + 观点持久化（估 2-3 session）
1. **第一个 session：最小 stage 化**——沿 replay_from_cache 已验证的缓存缝，把管线切成 数据→估值→agents→报告 四个可断点续跑 checkpoint（--update 增量模式的前置条件）
2. ThesisContract 持久化 + monitorables 封闭目录 + 版本链
3. meta_facts 退役棘轮上线；dunder 方言换 typed FactsContext
4. `--update TICKER` 增量模式（只重算变化层）

**验收**：同 ticker 二次运行 <3 分钟；thesis 带版本历史；棘轮 CI 生效。

### Phase 3 · 事件驱动 + 组合视图（估 2-3 session）
- launchd 扫描器（水位线 + 补扫）→ 触发增量复核 → delta 简报（"什么变了、对论点什么影响"）
- 每日 LLM 预算熔断；dashboard 打开即扫兜底；watchlist 多票视图（server/ 雏形扩展）
- postmortem 90 天回看激活

**验收**：一次预告/公告落地后，唤醒首扫内 thesis 自动更新并说明影响；单日 LLM 成本有上限。

### Phase 4 · 单体拆解（有前置闸门的独立阶段，不再"伴随进行"）
- **闸门**：先用三份现成 replay pkl（康达/NVDA/茅台）建 golden-master 挽具——replay→ResearchResult→规范 JSON，每拆一个 Step diff 一次，非空即回滚
- PipelineContext dataclass 显式化状态；一次只拆一个 Step；16 个耦合单体内部的测试文件改锁 stage 接口
- 硬规则：**任何 Phase 要改的 Step 必须先拆出再改**（否则绞杀速度追不上单体膨胀）

## 六、设计红线（评审沉淀，未来 session 必读）

1. 体制感知**只改叙事框架，永不抑制 DCF-vs-price 差值展示**（防循环论证）
2. 反解输出必须是**条件化的**（"若利润率 X 则需增速 Y"），禁止输出"市场隐含增速 Z%"单点（不可辨识 + WACC 敏感）
3. PIT 双时间戳：`as_of`=摄取时间，`announce_date`=披露日；历史回填必须标 `backfilled`；**别对用户承诺"历史回测无未来函数"直到 announce_date 回填完成**
4. TTM 只对流量科目、必须累计差分、归母/扣非双轨
5. 一致预期是旁证不是主口径；薄覆盖 gate 硬性执行（NaN 穿透有前科）
6. monitorables 封闭目录，LLM 只许选型号填阈值
7. 拆单体必须过 golden-master 闸门
8. meta_facts 引用文件数只减不增（CI 棘轮）
9. 新数字面世必须同步注册 scrubber/critic 白名单（sanctioned numbers 生成器）
10. 不做 async 重构；不建正式状态机；sqlite3 优先于新依赖

## 附录

- 三视角评审全文：workflow `wf_03cf5dcd-a7e` journal（量化 12 issues / 务实 10 issues·1 fatal / 重建派 6 issues，全部已合入上文）
- 数据源实测记录（2026-07-10，Clash 代理环境）：东财季报利润表 ✅ / 业绩预告 ✅ / datacenter 盈利预测 ✅ / 巨潮公告 ✅ / datacenter F10 行业 ✅ / tencent 行情 ✅ / push2 ❌（已知）/ 北向个股持仓 ❌（制度性停更）
- Aegis 1.x 审计与修复历史：AUDIT_2026-07.md + HANDOFF.md 2026-07-09/10 条目
