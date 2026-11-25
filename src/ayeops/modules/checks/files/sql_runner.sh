#!/bin/sh
set -u
STATE=${AYE_SQL_STATE:-/tmp/ayeops-sql}
METRICS=${AYE_SQL_METRICS:-/app/sql_metrics.awk}
TEXTFILE=$AYE_CHECKS_TEXTFILE
PARTIAL=$(dirname "$TEXTFILE")/.$(basename "$TEXTFILE").tmp
TAB=$(printf '\t')
mkdir -p "$STATE"

run_once() {
  exec 9>"$STATE/lock"
  flock 9
  eval "password=\${$9:-}"
  begin=$(cut -d' ' -f1 /proc/uptime)
  PGPASSWORD=$password PGSSLMODE=${10} PGCONNECT_TIMEOUT=$4 PGAPPNAME=ayeops-check-sql \
    timeout "$4" psql -X -q -A -t -F "$TAB" -v ON_ERROR_STOP=1 \
    -h "$5" -p "$6" -d "$7" -U "$8" -f "$STATE/$1.sql" >"$STATE/$1.out" 2>"$STATE/$1.err"
  code=$?
  end=$(cut -d' ' -f1 /proc/uptime)
  [ "$code" -eq 0 ] || sed "s/^/$1: /" "$STATE/$1.err" >&2
  awk -F "$TAB" -v check="$1" -v service="$2" -v code="$code" -v begin="$begin" \
    -v end="$end" -v now="$(date +%s)" -f "$METRICS" "$STATE/$1.out" \
    >"$STATE/$1.prom.tmp" && mv "$STATE/$1.prom.tmp" "$STATE/$1.prom"
  exec 9>&-
}

loop() {
  while :; do
    started=$(date +%s)
    run_once "$@"
    pause=$(($3 - ($(date +%s) - started)))
    [ "$pause" -ge 1 ] || pause=1
    sleep "$pause"
  done
}

check() {
  cat >"$STATE/$1.sql"
  loop "$@" &
}

. "$AYE_CHECKS_CONFIG"

while :; do
  {
    printf 'ayeops_checks_heartbeat_timestamp_seconds{runner="check-sql"} %s\n' "$(date +%s)"
    cat "$STATE"/*.prom 2>/dev/null
  } >"$PARTIAL" && mv "$PARTIAL" "$TEXTFILE"
  sleep 15
done
