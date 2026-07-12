#!/usr/bin/env bash
# Grok 对单票 Aegis thesis 做对抗性审计（demo 反馈用）。
# 用法: bash logs/grok_audit_stock.sh <code> <name>
# 只读：抽取 .cache/thesis/{code}.jsonl 最新版本 → 拼审计 prompt → grok headless 单轮。
# 产物: logs/grok_audits/{code}_audit.md (+ _prompt.md 输入留档 / _audit.err 错误)
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="$HOME/.grok/bin:$PATH"

CODE="${1:?need code}"
NAME="${2:-$CODE}"
OUT="${AUDIT_OUT_DIR:-logs/grok_audits}"
mkdir -p "$OUT"
PROMPT="$OUT/${CODE}_prompt.md"
AUDIT="$OUT/${CODE}_audit.md"
ERR="$OUT/${CODE}_audit.err"

# 1) 抽取 thesis 关键字段 → 审计 prompt 文件
python - "$CODE" "$NAME" "$PROMPT" <<'PY'
import json, sys, pathlib
code, name, promptpath = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(f".cache/thesis/{code}.jsonl")
if not p.exists():
    pathlib.Path(promptpath).write_text(f"[NO_THESIS] {code} 无 thesis 文件，无法审计。\n", encoding="utf-8")
    print("NO_THESIS"); sys.exit(0)
rec = json.loads([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()][-1])
t = rec.get("thesis", {})
anchor = rec.get("anchor_price")
ver = rec.get("version")

def fld(k):
    v = t.get(k)
    if v in (None, "", []):
        return "(空)"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, indent=2)
    return str(v)

sections = [
    # R5-L4：产品形态自我声明放最前——观察框架票按"问题清单/监控框架"的
    # 尺子读，而不是按可下单 thesis 的尺子（送审材料新增字段，审计指令不变）
    ("产品形态声明 product_form_reason", "product_form_reason"),
    ("核心论点 core_thesis", "core_thesis"),
    ("为何是现在 why_now", "why_now"),
    ("市场隐含预期 market_implied_story", "market_implied_story"),
    ("我的变异 my_variant", "my_variant"),
    ("变异幅度 variant_magnitude", "variant_magnitude"),
    ("关键假设分歧 key_assumption_disagreement", "key_assumption_disagreement"),
    ("反方论点(steelman) counter_thesis", "counter_thesis"),
    ("脆弱点 fragility_points", "fragility_points"),
    ("证伪触发 disconfirming_triggers", "disconfirming_triggers"),
    ("Kill Criteria", "kill_criteria"),
    ("必须监控 must_monitor", "must_monitor"),
    ("待解问题 open_questions", "open_questions"),
    ("估值假设表 valuation_assumptions（DCF 可审计附录）", "valuation_assumptions"),
    ("边缘分类 edge_classification", "edge_classification"),
    ("定价体制 sector_cycle_position", "sector_cycle_position"),
    ("管理层评价 management_quality_summary", "management_quality_summary"),
    ("资本配置 capital_allocation_assessment", "capital_allocation_assessment"),
]
body = [
    f"你是一名极其严格、坦诚、不吹捧的卖方研究质检 / 对抗性审稿人（grok-4.5）。",
    f"下面是 Aegis 智能投研系统对 A 股 **{name}（{code}）** 自动生成的研究论点（结构化 thesis contract）。",
    f"建仓价锚 anchor_price = {anchor}；决策 publishing_status = {t.get('publishing_status')}；置信 = {t.get('confidence_bucket')}；产品形态 product_form = {t.get('product_form', 'investment_thesis')}；论点版本 v{ver}；复盘日 = {t.get('review_date')}。",
    "",
    "请**只做审计、不修改任何文件、不调用工具**，基于金融与会计常识做对抗性检查，输出**简体中文 markdown**，严格按下列 7 节：",
    "",
    "1. **数值一致性**：论点内数字是否自洽（增速/利润率/现金流/DCF 差值/杠杆等算术是否闭合、有无自相矛盾）。逐条列出可疑数字与理由。",
    "2. **幻觉/无据数字**：有无凭空出现、无法从公开财报合理推出的具体数字或目标价。",
    "3. **预期优先叙事质量**：'市场隐含预期 → 我的变异 → 验证/证伪信号' 这条弧是否真的成立且有信息量，还是空泛套话。变异是否是**真 analytical edge** 还是通用话术。",
    "4. **Kill Criteria / must_monitor 可证伪性**：是否具体、量化、可查证；指出模糊或不可证伪的项。",
    "5. **体制判定合理性**：sector_cycle_position 与该公司真实基本面 / 行业阶段是否相符。",
    "6. **决策匹配度**：publishing_status（发布/暂不评级）与论点强度、证据充分度是否匹配。",
    "7. **总评**：一句话总评 + 按严重度排序的**最严重 3 个问题** + 一个 **0–10 的『可用于真实投研的可信度』打分**（并给一句打分依据）。",
    "",
    "语气像给主管看的内部质检备忘：直接、具体、能指名问题。不要复述原文，重点是**挑错与判断**。",
    "",
    "---",
    "",
    "## 待审计的研究论点",
    "",
]
for title, key in sections:
    body.append(f"### {title}")
    body.append(fld(key))
    body.append("")
pathlib.Path(promptpath).write_text("\n".join(body), encoding="utf-8")
print("PROMPT_OK", len("\n".join(body)))
PY

# 若无 thesis，跳过 grok，写占位
if grep -q "^\[NO_THESIS\]" "$PROMPT" 2>/dev/null; then
    echo "# ${NAME}(${CODE}) Grok 审计：跳过（无 thesis）" > "$AUDIT"
    exit 0
fi

# 2) grok headless 审计（内容内联，禁网/防挂起）。
# R5-L3：审计员单票采样噪声 ±0.5 > 修复轮间增量——KPI 逼近阈值时单次采样
# 读不出信号。AUDIT_RUNS=N（默认 1）对同一 prompt 独立重复采样，产物
# {code}_audit_run{i}.md；run1 同步复制到 {code}_audit.md 保持旧消费者兼容。
# 分数提取与均值±极差汇总: python scripts/audit_scores.py <dir>
RUNS="${AUDIT_RUNS:-1}"
for i in $(seq 1 "$RUNS"); do
    if [ "$RUNS" -eq 1 ]; then
        RUN_OUT="$AUDIT"; RUN_ERR="$ERR"
    else
        RUN_OUT="$OUT/${CODE}_audit_run${i}.md"
        RUN_ERR="$OUT/${CODE}_audit_run${i}.err"
    fi
    echo "[grok audit] ${CODE} ${NAME} run ${i}/${RUNS} start $(date +%H:%M:%S)" >> "$OUT/_audit_progress.log"
    grok --prompt-file "$PROMPT" \
         --disable-web-search \
         --effort high \
         --permission-mode bypassPermissions \
         > "$RUN_OUT" 2>"$RUN_ERR"
    RC=$?
    echo "[grok audit] ${CODE} ${NAME} run ${i}/${RUNS} done rc=$RC $(date +%H:%M:%S) bytes=$(wc -c <"$RUN_OUT" 2>/dev/null)" >> "$OUT/_audit_progress.log"
    if [ "$RUNS" -gt 1 ] && [ "$i" -eq 1 ]; then
        cp "$RUN_OUT" "$AUDIT"
    fi
done
exit 0
