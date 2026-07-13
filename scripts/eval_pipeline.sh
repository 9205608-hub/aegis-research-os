#!/bin/bash
# 评测轮流水线：5 票串行重生成（防 DeepSeek 限流），每票生成完立刻
# 后台起 Grok 审计（AUDIT_RUNS 独立采样，与下一票生成重叠省墙钟），
# 全部完成后 audit_scores.py 分轨汇总均值。
#
# 双轨（2026-07-13 Grok 仲裁）：
#   主 KPI 轨 = FORCE_RUBRIC=thesis → logs/grok_audits_round{N}/
#     （冻结 thesis 尺，与 R1-R6 历史轨迹同仪器可比）
#   副轨（形态 QA）= 观察框架票按 FRAMEWORK_RUBRIC_V1 → logs/grok_audits_round{N}_fw/
#     （框架尺分数禁止与主轨迹接龙——测量红线）
#
# 用法: bash scripts/eval_pipeline.sh <轮号>    # 如 7
#   AUDIT_RUNS   每票每轨采样次数（默认 2）
#   SKIP_FW=1    跳过副轨（省 grok 调用）
# 注意 macOS 自带 bash 3.2 无 declare -A，用平行数组。
set -u
cd "$(dirname "$0")/.." || exit 1

N="${1:?need round number, e.g. 7}"
RUNS="${AUDIT_RUNS:-2}"
ROUND="eval_round${N}"
AUD="logs/grok_audits_round${N}"
AUD_FW="logs/grok_audits_round${N}_fw"
mkdir -p "logs/$ROUND" "$AUD"
PROGRESS="logs/$ROUND/_pipeline_progress.log"

TICKERS=(300750 300502 600519 002371 002594)
NAMES=(宁德时代 新易盛 贵州茅台 北方华创 比亚迪)

echo "[$(date +%H:%M:%S)] R${N} pipeline start (AUDIT_RUNS=$RUNS)" >> "$PROGRESS"
AUDIT_PIDS=""
for idx in 0 1 2 3 4; do
  code="${TICKERS[$idx]}"
  name="${NAMES[$idx]}"
  echo "[$(date +%H:%M:%S)] GEN_START $code $name" >> "$PROGRESS"
  ./run_research.sh "$code" > "logs/$ROUND/${code}.log" 2>&1
  rc=$?
  ver=$(tail -1 ".cache/thesis/${code}.jsonl" 2>/dev/null | python3 -c "import sys,json;print(json.loads(sys.stdin.read()).get('version','?'))" 2>/dev/null)
  pf=$(tail -1 ".cache/thesis/${code}.jsonl" 2>/dev/null | python3 -c "import sys,json;print(json.loads(sys.stdin.read()).get('thesis',{}).get('product_form','?'))" 2>/dev/null)
  echo "[$(date +%H:%M:%S)] GEN_DONE $code rc=$rc thesis_v=$ver product_form=$pf" >> "$PROGRESS"
  if [ "$rc" -eq 0 ] && [ -f ".cache/thesis/${code}.jsonl" ]; then
    # 主 KPI 轨：强制 thesis 尺（与历史轨迹同仪器）
    ( AUDIT_RUNS="$RUNS" AUDIT_OUT_DIR="$AUD" FORCE_RUBRIC=thesis \
        bash scripts/grok_audit_stock.sh "$code" "$name" \
        && echo "[$(date +%H:%M:%S)] AUDIT_DONE $code (thesis)" >> "$PROGRESS" ) &
    AUDIT_PIDS="$AUDIT_PIDS $!"
    # 副轨：观察框架票按 FRAMEWORK_RUBRIC_V1（形态 QA，不进主均值）
    if [ "${SKIP_FW:-0}" != "1" ] && [ "$pf" = "observation_framework" ]; then
      mkdir -p "$AUD_FW"
      ( AUDIT_RUNS="$RUNS" AUDIT_OUT_DIR="$AUD_FW" FORCE_RUBRIC=framework \
          bash scripts/grok_audit_stock.sh "$code" "$name" \
          && echo "[$(date +%H:%M:%S)] AUDIT_DONE $code (framework)" >> "$PROGRESS" ) &
      AUDIT_PIDS="$AUDIT_PIDS $!"
    fi
  else
    echo "[$(date +%H:%M:%S)] AUDIT_SKIP $code (gen failed or no thesis)" >> "$PROGRESS"
  fi
done

echo "[$(date +%H:%M:%S)] all generations done, waiting for audits:$AUDIT_PIDS" >> "$PROGRESS"
for pid in $AUDIT_PIDS; do wait "$pid"; done
echo "[$(date +%H:%M:%S)] ALL_AUDITS_DONE" >> "$PROGRESS"
{
  echo "===== 主 KPI 轨（冻结 thesis 尺，可与 R1-R6 轨迹比） ====="
  python scripts/audit_scores.py "$AUD"
  if [ -d "$AUD_FW" ]; then
    echo ""
    echo "===== 副轨（FRAMEWORK_RUBRIC_V1，形态 QA，禁止与主轨迹接龙） ====="
    python scripts/audit_scores.py "$AUD_FW"
  fi
} | tee "logs/$ROUND/_scores_summary.txt"
echo "R${N}_PIPELINE_COMPLETE"
