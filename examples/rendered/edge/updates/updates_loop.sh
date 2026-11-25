set -u
state="${AYE_UPDATES_STATE:-/state}"
limit="${AYE_UPDATES_TIMEOUT:-7200}"
while :; do
  touch "$state/loop.alive" 2>/dev/null
  if [ -e "$state/run.request" ]; then
    rm -f "$state/run.request" "$state/report.json"
    started=$(date +%s)
    timeout "$limit" renovate
    code=$?
    printf '%s %s %s\n' "$started" "$(date +%s)" "$code" > "$state/run.last.tmp"
    mv -f "$state/run.last.tmp" "$state/run.last"
    touch "$state/loop.alive" 2>/dev/null
  fi
  sleep 15
done
