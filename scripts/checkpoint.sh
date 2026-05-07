#!/bin/bash
# Polybot study-mode checkpoint — runs analyze + simulate and writes a single
# combined report into logs/checkpoint_<date>.txt for human review.
set -euo pipefail

PROJECT=/root/polybot
PY=$PROJECT/venv/bin/python
OUT=$PROJECT/logs/checkpoint_$(date -u +%Y-%m-%d_%H%M).txt

{
  echo "=== Polybot study-mode checkpoint — $(date -u --iso-8601=seconds) ==="
  echo
  echo "### analyze.py ###"
  "$PY" "$PROJECT/scripts/analyze.py"
  echo
  echo "### simulate.py (filter scenarios) ###"
  "$PY" "$PROJECT/scripts/simulate.py"
  echo
  echo "### simulate.py --low-conf-probe ###"
  "$PY" "$PROJECT/scripts/simulate.py" --low-conf-probe
} >"$OUT" 2>&1

echo "Checkpoint written to $OUT"
