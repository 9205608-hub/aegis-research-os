#!/bin/bash
# 置信度评测批：5 票按当前管线重生成 thesis（串行，防限流）。
# 用法: ./scripts/eval_batch.sh <round_name>   # 产物 logs/<round_name>/
set -u
cd "$(dirname "$0")/.."
ROUND="${1:?need round name, e.g. eval_round4}"
mkdir -p "logs/$ROUND"
TICKERS=(300750 300502 600519 002371 002594)
for code in "${TICKERS[@]}"; do
  echo "=== [$(date +%H:%M:%S)] START $code ==="
  ./run_research.sh "$code" > "logs/$ROUND/${code}.log" 2>&1
  rc=$?
  ver=$(tail -1 ".cache/thesis/${code}.jsonl" 2>/dev/null | python3 -c "import sys,json;print(json.loads(sys.stdin.read()).get('version','?'))" 2>/dev/null)
  echo "=== [$(date +%H:%M:%S)] DONE $code rc=$rc thesis_version=$ver ==="
done
echo "ALL_RUNS_COMPLETE"
