#!/bin/bash
# R5 评测轮流水线：5 票串行重生成（防 DeepSeek 限流），每票生成完立刻
# 后台起 Grok 审计（AUDIT_RUNS=2 独立采样，与下一票生成重叠省墙钟），
# 全部完成后 audit_scores.py 汇总均值。
# 产物: logs/eval_round5/{code}.log + logs/grok_audits_round5/
# 注意 macOS 自带 bash 3.2 无 declare -A，用平行数组。
set -u
cd "$(dirname "$0")/.." || exit 1

ROUND="eval_round5"
AUD="logs/grok_audits_round5"
mkdir -p "logs/$ROUND" "$AUD"
PROGRESS="logs/$ROUND/_pipeline_progress.log"

TICKERS=(300750 300502 600519 002371 002594)
NAMES=(宁德时代 新易盛 贵州茅台 北方华创 比亚迪)

echo "[$(date +%H:%M:%S)] R5 pipeline start" >> "$PROGRESS"
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
    ( AUDIT_RUNS=2 AUDIT_OUT_DIR="$AUD" bash scripts/grok_audit_stock.sh "$code" "$name" \
        && echo "[$(date +%H:%M:%S)] AUDIT_DONE $code" >> "$PROGRESS" ) &
    AUDIT_PIDS="$AUDIT_PIDS $!"
  else
    echo "[$(date +%H:%M:%S)] AUDIT_SKIP $code (gen failed or no thesis)" >> "$PROGRESS"
  fi
done

echo "[$(date +%H:%M:%S)] all generations done, waiting for audits:$AUDIT_PIDS" >> "$PROGRESS"
for pid in $AUDIT_PIDS; do wait "$pid"; done
echo "[$(date +%H:%M:%S)] ALL_AUDITS_DONE" >> "$PROGRESS"
python scripts/audit_scores.py "$AUD" | tee "logs/$ROUND/_scores_summary.txt"
echo "R5_PIPELINE_COMPLETE"
