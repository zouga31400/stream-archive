#!/bin/bash
# Équivalent du workflow "Recording" (record.yml): ./run.sh [minutes]
#   sans argument ou 0 = enregistrement normal du créneau; feed = reconstruit seulement le flux;
#   un nombre = enregistrement de test de cette durée en minutes.
# Le workflow termine (si SUMMARY=1) par le résumé, comme summary.yml après un Recording réussi.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "Il manque .env (cp .env.example .env et remplis-le)."; exit 1; }
[ -d .venv ] || { echo "Lance d'abord: bash setup.sh"; exit 1; }
set -a; . ./.env; set +a
export TEST_MINUTES="${1:-0}"
export TZ=UTC   # le runner GitHub est en UTC; RECORD_TZ gère le fuseau du créneau
. .venv/bin/activate
mkdir -p logs
# timeout-minutes: 355 du workflow
timeout 355m python record.py 2>&1 | tee -a "logs/record-$(date -u +%F).log"
rc=${PIPESTATUS[0]}
if [ "$rc" -eq 0 ] && [ "${SUMMARY:-0}" = "1" ] && [ "$TEST_MINUTES" = "0" ]; then
  timeout 350m python summarize.py 2>&1 | tee -a "logs/summary-$(date -u +%F).log"
fi
exit "$rc"
