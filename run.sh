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
# Deux verrous séparés, comme les groupes de concurrence "recording" et "summary" des workflows:
# un seul enregistrement à la fois, un seul résumé à la fois, mais un enregistrement
# peut démarrer pendant qu'un résumé de la veille tourne encore.
exec 8>.lock-recording
if ! flock -n 8; then echo "Un enregistrement est déjà en cours: abandon."; exit 0; fi
# timeout-minutes: 355 du workflow
timeout 355m python record.py 2>&1 | tee -a "logs/record-$(date -u +%F).log"
rc=${PIPESTATUS[0]}
flock -u 8   # l'enregistrement est fini: le suivant pourra démarrer pendant le résumé
if [ "$rc" -eq 0 ] && [ "${SUMMARY:-0}" = "1" ] && [ "$TEST_MINUTES" = "0" ]; then
  exec 9>.lock-summary
  # comme "cancel-in-progress: false": le résumé suivant attend (au plus 6 h) la fin du précédent
  if flock -w 21600 9; then
    timeout 350m python summarize.py 2>&1 | tee -a "logs/summary-$(date -u +%F).log"
  else
    echo "Résumé précédent toujours en cours après 6 h: résumé abandonné."
  fi
fi
exit "$rc"
