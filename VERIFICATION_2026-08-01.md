# VERIFICATION_2026-08-01 — 六路并行优化批 mock 端到端实证

> 验证 agent 独立 worktree run。零真实 LLM 消耗（全部调用被伪造 key 401 拒绝，未产生任何计费）。
> 分支: `claude/project-progress-commercialization-ae872e`
> Worktree: `~/Desktop/智能投研助手/.claude/worktrees/agent-a9a6a362e68ff9135`
> 结论：**六项全部通过，未发现 wiring 级 bug，零代码改动。**

## 1. 跑法（离线/零 LLM 花费）

`--smoke` 是纯规则 agent（不产生 `is_llm_fallback` 盖章，无法压测 degraded 识别），故不采用。
实际采用 **backend=sdk + 伪造 ANTHROPIC_API_KEY** 方案：

- `_check_llm_backend_health`（auto_research.py:5310）对 sdk 后端只查环境变量存在性 → 健康检查放行；
- 每次 LLM 调用打到真实 api.anthropic.com 被 401 拒绝（实测 0.4s/次，401 不在 SDKClient 可重试错误列表，无退避风暴，零计费）；
- 每个 agent 走 llm_agent_base.py:458 的 MockLLMClient 兜底分支 → `is_llm_fallback=True` 盖章 + `[规则模板兜底·鉴权失败]` 文本标记 → **degraded 识别双层通道全部点亮 = 极限压力测试**。

命令原文（scripts/_verify_e2e_run.sh，worktree 内未提交）：

```bash
unset CLAUDE_CODE_OAUTH_TOKEN DEEPSEEK_API_KEY GROK_API_KEY XAI_API_KEY
export ANTHROPIC_API_KEY="sk-ant-fake-verify-401"
export SEC_USER_AGENT="Aegis Research research@aegis.ai"
export AEGIS_LLM_CACHE=0
python demos/auto_research_demo.py 300502 --wacc 0.095 --period latest \
    --llm --backend sdk --model sonnet > verification_run.log 2>&1
```

- 票：**300502（新易盛）**——Wave 2/3 接口均实测过的票
- 数据准备：从主仓库**只读拷贝** `.cache/annual_reports/300502_2025_1225172598.pdf` 至 worktree（主仓库零写入）；其余全部真实网络拉取（akshare/东财/巨潮）
- 耗时：**2 分 15 秒**（14:01:01 → 14:03:16），exit code 0
- 产物：`demos/300502_fy2025_auto_report.html`（121KB）、`.cache/300502_replay_state.pkl`、`verification_run.log`

## 2. 六项验证逐项结论

### ① Wave 3 盖章 — ✅ 通过

run 日志两条盖章行均出现（akshare 实时拉取成功）：

```
[14:02:39] Restricted release (L1): 下一批 无，未来12月待解禁占总市值 0.00%（东财个股限售解禁批次（截至 2026-08-01））
[14:03:02] Equity pledge (L1): 全股质押比例 0.00%（东财股权质押专题（300502：中登周频质押比例+重要股东质押明细））
```

顺带 Wave 1/2 同 run 复验：`Segment composition (L1): 3 轴`、`Customer concentration (L1): top5 72.34%`（PDF 命中 worktree 缓存零下载）。

meta_facts 盖章内容（replay pkl 摘录）：

```
__restricted_release: [解禁日历] 已公告限售批次均已解禁，未来 12 个月无已公告解禁批次（最近一批 2025-06-13 已解禁，占总市值 0.15%）
__equity_pledge:     [股权质押] 截至 2026-07-31 中登质押登记未见该股，整体质押比例可视为接近零
                     [股权质押] 东财重要股东质押明细与中登口径不符（含疑似未更新的历史「未解押」记录），以上述中登质押比例为准
```

负面证据注入（"接近零质押"）+ `detail_stale` 降级（东财 2018/2021 陈旧"未解押"记录被中登口径否决）与 HANDOFF 描述完全一致。

### ② definition_gate 解封 — ✅ 通过（skips 列表整个消失，最优结果）

- 全 log **不存在** `Gate skips (missing inputs)` 行（grep 零命中）；
- gate_result_first_pass 中 definition_gate 检查原文：

```
definition_gate: passed=True severity=warn
    msg=No registry provided — definition gate not armed
```

新措辞 "not armed" 不含 "skipped" 子串 → auto_research.py:4126 的 B4 收割谓词不再命中 → `gate_skipped_count=0` → skip 封顶解除。本 run 其余 13 门全部真实通过（`Publish Gate: PASSED`，blocked_by=[]）。

### ③ 伪警告剥离实战 — ✅ 通过（7/7 judgment 全部识别为 degraded）

warn_accumulation_gate 消息原文（含预期的"另有 N 条"注记，N=3）：

```
warn_accumulation_gate: passed=True severity=block
    msg=Warning count acceptable: 1 (threshold: 20)；另有 3 条输入退化警告（LLM 兜底产物，不计入阈值）
```

`split_issue_counts` 复算（replay pkl）：

```
DegradedIssueSplit(real_warns=1, degraded_warns=3, real_blocks=0, degraded_blocks=0, degraded_judgment_count=7)
```

最终置信度（log 原文）：

```
[14:03:14] Decision: published, confidence=medium
```

- 7 个 judgment 全部被双层标记识别为 degraded；3 条 critic warn 被正确归类为输入退化、不计入阈值、不扣分；
- confidence 分数复算：raw score 74.0（本身落 medium 带）+ degraded 封顶 medium 同时武装——**未被伪警告打到 low/very_low**，发布状态 published（非 blocked）；
- 诚实注记：本 run raw 分数即 medium，封顶帽虽武装但非 binding；"重度兜底 run 从 low 上移 medium"的方向性证据是 published + medium + 伪 warn 不进阈值三件事同时成立。

### ④ 红旗两档 — ✅ 通过（1 个 agent，显著低于历史常态 5 个）

log 原文：

```
[14:03:12]   Inter-agent flow: 6 findings passed between agents
[14:03:12]   ⚠ Red flags from: risk_analyst
```

唯一举旗的 risk_analyst 是弱词分档的**正确**触发：其 mock 观察含一处"风险"（弱词 1 hit）且 inference confidence=="low" → 弱词阈值降为 1 → 举旗。其余 6 个 agent（会计/业务/管理层/估值/变体/宏观）模板同样是温和文本 → 零举旗。旧逻辑下"风险"一词必中会导致 risk_analyst+至少数个 agent 齐举旗（历轮"5 agent 红旗"常态）。1 ∈ 预期带 [0, 2]。

### ⑤ 首页清洗不炸 — ✅ 通过（Editor 走兜底路径，如实记录）

```
[14:03:14]   ⚠ Report Editor failed (Error code: 401 ...), using standard report layout
[14:03:16] HTML report saved to demos/300502_fy2025_auto_report.html
```

- Editor LLM 调用 401 → 走"standard report layout"兜底 → **`_scrub_front_page_numbers` 清洗路径本 run 未被执行**（Editor 未产出 front_page_numbers 即兜底，属 mock 模式预期路径）；该路径的行为背书仍是 +23 单测；
- 全 log 零 Traceback、零未捕获异常；front_page 相关无任何异常栈；
- html_report_v2 无 front_page_numbers 渲染区块（与 HANDOFF 记载一致），HTML 正常产出 121KB。

### ⑥ 报告成品 — ✅ 通过（中文完整；Wave 3 可见于数据层）

HTML（`demos/300502_fy2025_auto_report.html`）检查：

- **中文完整性**：title `Aegis 投研 — 新易盛 (300502.SZ)`；period "FY2025 年报"；评级"回避/概率加权/12 个月/中高"；置信度"中"；7 个 agent 卡角色全中文（业务分析师/管理层分析师/估值分析师/会计分析师/风险分析师/变体分析师/宏观分析师）；10 个 critic 全中文（逻辑批评员…LLM 数值审核员）；quick 栏（市值/市盈率/自由现金流）全中文。兜底文本自身即中文模板（BUG-34 链路正常）。
- **degraded 渲染**：7 张 agent 卡全部 `fallback: true`、stance=neutral、正文带 `[规则模板兜底·鉴权失败]` 前缀——BUG-A13 失败原因分类（鉴权失败）在卡面正确呈现。
- **Wave 3 可见层**：如实记录——盖章数据可见于 **meta_facts / replay pkl 数据层**（上文 ① 摘录）与 agent prompt 注入层（llm_agent_base.py:1165-1191 的 RESTRICTED RELEASE / EQUITY PLEDGE 中文块，本 run 已构建但 mock agent 不消费）；**HTML 正文无 Wave 3 数字**（grep "解禁/质押" 零命中）——因为 HTML 无独立事实渲染区，Wave 3 进报告靠 agents/synthesizer 引用，mock 产物不会引用，符合预期。真实 LLM run 才能验证正文引用。
- **极限退化诚实性**：Director+Synthesizer 双兜底时 headline/lede = "（合成失败：缺少核心论点）"、conviction_narrative = "（Synthesizer 调用失败，本节由 Director 摘要兜底）"（SynthesizedThesis.edge_source='research_director_fallback'）——不装死、不编内容。
- productForm=null（published 票 → investment_thesis → 横幅隐藏，与 L4 规则一致）；staleBanner=null（FY2025 年报距今 7 个月 < 15 个月阈值，正确）。

## 3. 发现并修复 / 发现未修的问题

**修复：无。** 全程未发现 wiring 级 bug（≤10 行修复授权未动用，零代码改动，无回归测试新增必要）。

**观察记录（不修，供后续参考）：**

1. **equity_pledge 拉取耗时高于 HANDOFF 记载**：HANDOFF 写"中登快照拉全市场 ~3s/run"；本次实测 pipeline 内该步骤 23s（14:02:39→14:03:02），独立冷启动测试 75.8s（5 期快照循环 + 明细对账）。方向上佐证 HANDOFF 遗留 #2 的"嫌重可换 datacenter API 按码过滤"，建议真实 run 再计时一次。
2. **risk_analyst 兜底必举旗**（非 bug，行为记录）：中文 mock 模板 risk_analyst 观察含"风险"一词 + inference confidence=low → 弱词阈值 1 → 必然举旗。任何 risk_analyst 单独兜底的真实 run 也会固定 +1 旗，在 0-2 预期带内可接受。
3. **"falling back to mock" 日志行双写**：BUG-A16 有意 stdout+stderr 双流镜像，`2>&1` 合流后每行出现两次。已知设计，非 bug。
4. **本 run 未覆盖的路径**（如实声明）：`_scrub_front_page_numbers`（Editor 兜底跳过）、synthesizer strict 清洗（Synthesizer 兜底跳过）、Wave 3 白名单 % 存活（无 LLM 产文可洗）——这三条只有真实 LLM run 能实证。

## 4. 下一次真实 LLM run 观察清单

1. **Gate skips 行持续缺席** + 若之后武装 definition_gate（HANDOFF 遗留 #3），先统计 agents 自报 `used_metric_ids` 注册率再放行 block。
2. **红旗量级**：`Red flags from:` 行 agent 数应从历史 5 降至 0-2；同时盯 002697（公司名"红旗"）与英文 "risk"⊂"asterisk" 两个已知边角。
3. **high 档可达性**：全 LLM 成功 + 无 degraded + 无 skip 时，confidence 是否首次出现 high（分数 80+ 需 evidence/narrative 抬分）；部分 agent 兜底时应 medium 封顶且 gate message 出现"另有 N 条输入退化警告"。
4. **Editor 首页清洗真实过网**：front_page_numbers 的 value/label 改写剔除、context 原位替换是否正常；Wave 3 解禁/质押 % 在 `_extra_pcts` 共享白名单下应存活。
5. **Wave 3 注入效果**（照 Wave 2 客户集中度闭环范式）：agents 不再把解禁/质押列 open_questions；负面证据（"质押接近零/无待解禁批次"）应被正面引用而非缺口化。
6. **equity_pledge 步骤计时**：若稳定 >20s，落地 datacenter API 按码过滤。
7. **degraded 封顶展示合理性**（HANDOFF 遗留 #4）：真实 run 若重度兜底，medium 置信度 + 7 张兜底卡的组合在报告上是否自洽可读。

## 5. 验证器材清单（worktree 内，未提交）

- `scripts/_verify_e2e_run.sh` — 端到端跑法脚本（命令原文）
- `scripts/_verify_sdk_401.py` — 伪 key 401 速度预检（0.4s，无退避）
- `scripts/_verify_wave3_net.py` — Wave 3 连接器真网预检
- `scripts/_verify_postrun.py` / `_verify_gates.py` / `_verify_conf_math.py` — pkl 证据提取与置信度复算
- `verification_run.log` — 完整 run 日志（231 行）
- `demos/300502_fy2025_auto_report.html` — 成品报告
- `.cache/300502_replay_state.pkl` — 数据层证据
