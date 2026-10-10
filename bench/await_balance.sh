#!/usr/bin/env bash
# Wait until the DeepSeek account is usable again, then launch an experiment (docs/60 §4.1).
# Usage: bench/await_balance.sh <spec> <out>     (from the repo root; never prints the key)
set -u
spec=$1 out=$2
set -a; . ./.env; set +a
. .venv/bin/activate
while :; do
  ok=$(curl -s -m 20 https://api.deepseek.com/user/balance \
         -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
       | python3 -c "import sys,json; print(json.load(sys.stdin).get('is_available'))" 2>/dev/null)
  echo "$(date -Is) balance available: ${ok:-unknown}"
  [ "$ok" = "True" ] && break
  sleep 300
done
python -c "from sciteam.kernel.config_store import interpreter_digest as d; print(d())" > "$out.digest"
echo "$(date -Is) launching $spec -> $out (digest $(head -c 12 "$out.digest"))"
exec sciteam exp run "$spec" --out "$out"
