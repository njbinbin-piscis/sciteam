#!/usr/bin/env bash
# Wait until the MiMo account answers again, then (re)launch an experiment; runs that already
# have result.json are skipped by the runner (docs/60 §11 D7).
set -u
spec=$1 out=$2 digest=$3
set -a; . ./.env; set +a
. .venv/bin/activate
while :; do
  code=$(curl -s -m 30 -o /dev/null -w "%{http_code}" https://api.xiaomimimo.com/v1/chat/completions \
    -H "Authorization: Bearer $MIMO_API_KEY" -H "Content-Type: application/json" \
    -d '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ok"}],"max_tokens":1}')
  echo "$(date -Is) mimo probe http ${code}"
  [ "$code" = "200" ] && break
  sleep 300
done
now=$(python -c "from sciteam.kernel.config_store import interpreter_digest as d; print(d())")
if [ "$now" != "$digest" ]; then
  echo "$(date -Is) interpreter digest changed ($now != $digest); not launching"; exit 1
fi
echo "$(date -Is) launching $spec -> $out (digest ${now:0:12})"
exec sciteam exp run "$spec" --out "$out"
