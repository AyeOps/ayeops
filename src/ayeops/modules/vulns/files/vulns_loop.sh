set -u
state="${AYE_VULNS_STATE:-/state}"
limit="${AYE_VULNS_TIMEOUT:-900}"
scratch="${TMPDIR:-/tmp}"
while :; do
  touch "$state/loop.alive" 2>/dev/null
  if [ -e "$state/run.request" ]; then
    mv -f "$state/run.request" "$state/run.images"
    rm -rf "$state/reports.next"
    mkdir -p "$state/reports.next" "$scratch"
    rm -rf "${scratch:?}"/*
    : > "$state/reports.next/results"
    started=$(date +%s)
    timeout "$limit" trivy image --download-db-only < /dev/null
    db=$?
    while read -r key image; do
      [ -n "${image:-}" ] || continue
      code=$db
      if [ "$db" -eq 0 ]; then
        timeout "$limit" trivy image --skip-db-update --output "$state/reports.next/$key.json" "$image" < /dev/null
        code=$?
      fi
      printf '%s %s %s\n' "$key" "$code" "$image" >> "$state/reports.next/results"
      touch "$state/loop.alive" 2>/dev/null
    done < "$state/run.images"
    rm -rf "${scratch:?}"/* "$state/reports"
    mv -f "$state/reports.next" "$state/reports"
    printf '%s %s %s\n' "$started" "$(date +%s)" "$db" > "$state/run.last.tmp"
    mv -f "$state/run.last.tmp" "$state/run.last"
    touch "$state/loop.alive" 2>/dev/null
  fi
  sleep 15
done
