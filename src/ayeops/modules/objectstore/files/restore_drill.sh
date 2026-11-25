#!/bin/sh
set -u
CONFIG=${AYE_DRILL_CONFIG:-/app/drills.sh}
DATA=${AYE_DRILL_DATA:-/data}
SCRATCH=${AYE_DRILL_SCRATCH:-/scratch}
TEXTFILE_DIR=${AYE_DRILL_TEXTFILE_DIR:-/textfile}
WORK=${AYE_DRILL_WORK:-/tmp/drill}
RESTIC=${AYE_RESTIC:-/opt/restic/restic}
RUNAS=${AYE_DRILL_USER-postgres}
SETSID=${AYE_DRILL_SETSID-setsid}
CLOCK=${AYE_DRILL_CLOCK:-date +%s}
POLL=${AYE_DRILL_POLL:-5}
LOOP=${AYE_DRILL_LOOP:-60}
PREFIX=ayeops_offsite_drill_
DAY=86400
SELF=$0

now() {
  eval "$CLOCK"
}

log() {
  printf 'offsite-drill: %s\n' "$*" >&2
}

as_user() {
  if [ -n "$RUNAS" ]; then
    su-exec "$RUNAS" "$@"
  else
    "$@"
  fi
}

value() {
  [ -f "$2" ] || return 0
  sed -n "s/^$PREFIX$1{[^}]*} //p" "$2" | head -n 1
}

drill() {
  printf '%s %s %s %s %s %s\n' "$@" >>"$WORK/clients"
  mkdir -p "$WORK/checks/$1"
}

check() {
  cat >"$WORK/checks/$1/$2.sql"
}

slot() {
  day_start=$(($2 - $2 % DAY))
  at=$((day_start + $1))
  [ "$at" -le "$2" ] || at=$((at - DAY))
  echo "$at"
}

due() {
  [ -n "$1" ] || return 0
  [ "$1" -lt "$4" ] || return 1
  [ -n "$2" ] || return 0
  [ $(($4 - $2)) -ge $(($3 * DAY - DAY / 2)) ]
}

stop_postgres() {
  [ -f "$SCRATCH/pg/postmaster.pid" ] || return 0
  as_user pg_ctl -D "$SCRATCH/pg" -m immediate -w stop >/dev/null 2>&1 || true
}

wipe() {
  stop_postgres
  find "$SCRATCH" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
}

prune_textfiles() {
  for file in "$TEXTFILE_DIR/$PREFIX"*.prom; do
    [ -f "$file" ] || continue
    name=${file##*/"$PREFIX"}
    name=${name%.prom}
    grep -q "^$name " "$WORK/clients" || rm -f "$file"
  done
}

emit() {
  printf '%s%s{repo="%s"} %s\n' "$PREFIX" "$1" "$client" "$2"
}

fail() {
  log "$client: drill failed: $*"
  exit 1
}

last_line() {
  tail -n 1 "$1" 2>/dev/null
}

positive() {
  awk -v v="$1" 'BEGIN { exit !(v ~ /^-?[0-9]+(\.[0-9]+)?$/ && v + 0 > 0) }'
}

restore_postgres() {
  socket="$SCRATCH/run"
  (umask 077 && mkdir "$SCRATCH/pg") || fail "cannot create the data directory"
  (umask 022 && mkdir "$socket") || fail "cannot create the socket directory"
  if [ -n "$RUNAS" ]; then
    chown "$RUNAS:$RUNAS" "$SCRATCH/pg" "$socket" || fail "cannot hand the scratch to $RUNAS"
  fi
  as_user initdb -D "$SCRATCH/pg" -U postgres -A trust -E UTF8 --no-sync \
    >"$SCRATCH/initdb.log" 2>&1 || fail "initdb: $(last_line "$SCRATCH/initdb.log")"
  printf "listen_addresses = ''\nunix_socket_directories = '%s'\n" "$socket" \
    >>"$SCRATCH/pg/postgresql.conf"
  as_user pg_ctl -D "$SCRATCH/pg" -l "$SCRATCH/postgres.log" -w -t 300 start >/dev/null 2>&1 ||
    fail "postgres did not start: $(last_line "$SCRATCH/postgres.log")"
  as_user createdb -h "$socket" -U postgres drill 2>"$SCRATCH/sql.err" ||
    fail "createdb: $(last_line "$SCRATCH/sql.err")"
  as_user pg_restore -h "$socket" -U postgres -d drill --no-owner --no-privileges \
    --exit-on-error -j "$jobs" "$SCRATCH/dump" 2>"$SCRATCH/restore.err" ||
    fail "pg_restore: $(last_line "$SCRATCH/restore.err")"
}

measure_postgres() {
  as_user psql -X -q -A -t -h "$socket" -U postgres -d drill \
    -c "select pg_database_size('drill')" 2>/dev/null
}

query_postgres() {
  as_user psql -X -q -A -t -v ON_ERROR_STOP=1 -h "$socket" -U postgres -d drill -f "$1" \
    2>"$SCRATCH/sql.err"
}

run() {
  client=$1
  jobs=$2
  secret=$3
  eval "RESTIC_PASSWORD=\${$secret:-}"
  export RESTIC_PASSWORD
  [ -n "$RESTIC_PASSWORD" ] || fail "$secret is empty"
  repo="$DATA/$client"
  "$RESTIC" -r "$repo" --no-lock --no-cache ls --json --host "$client" latest \
    >"$SCRATCH/ls.json" 2>"$SCRATCH/restic.err" ||
    fail "restic ls: $(last_line "$SCRATCH/restic.err")"
  snapshot=$(head -n 1 "$SCRATCH/ls.json")
  id=$(printf '%s\n' "$snapshot" | sed -n 's/.*"id":"\([0-9a-f]*\)".*/\1/p')
  sha=$(printf '%s\n' "$snapshot" | sed -n 's/.*"sha256=\([0-9a-f]*\)".*/\1/p')
  path=$(grep '"type":"file"' "$SCRATCH/ls.json" | head -n 1 | sed 's/.*"path":"\([^"]*\)".*/\1/')
  [ -n "$id" ] && [ -n "$path" ] || fail "no snapshot of $client with a file in $repo"
  log "$client: restoring $path from snapshot ${id%"${id#????????}"}"
  started=$(now)
  "$RESTIC" -r "$repo" --no-lock --no-cache dump "$id" "$path" \
    >"$SCRATCH/dump" 2>"$SCRATCH/restic.err" ||
    fail "restic dump: $(last_line "$SCRATCH/restic.err")"
  unset RESTIC_PASSWORD
  fetched=$(now)
  bytes=$(wc -c <"$SCRATCH/dump" | tr -d ' ')
  emit fetch_seconds $((fetched - started))
  emit dump_bytes "$bytes"
  [ "$bytes" -gt 0 ] || fail "the restored file is empty"
  if [ -n "$sha" ]; then
    actual=$(sha256sum "$SCRATCH/dump" | cut -d ' ' -f 1)
    [ "$actual" = "$sha" ] || fail "restored sha256 $actual differs from the recorded $sha"
  fi
  [ "$(head -c 5 "$SCRATCH/dump")" = PGDMP ] ||
    fail "$path is not a pg_dump custom-format archive"
  restore_postgres
  restored=$(now)
  emit restore_seconds $((restored - fetched))
  emit duration_seconds $((restored - started))
  size=$(measure_postgres)
  [ -z "$size" ] || emit restored_bytes "$size"
  failed=0
  for sql in "$WORK/checks/$client"/*.sql; do
    [ -f "$sql" ] || continue
    name=${sql##*/}
    name=${name%.sql}
    result=$(query_postgres "$sql") || result=
    result=$(printf '%s\n' "$result" | head -n 1)
    if positive "$result"; then
      ok=1
    else
      ok=0
      failed=1
      log "$client: check $name returned '${result:-$(last_line "$SCRATCH/sql.err")}'"
    fi
    if printf '%s\n' "$result" | grep -Eq '^-?[0-9]+(\.[0-9]+)?$'; then
      printf '%scheck_value{repo="%s",check="%s"} %s\n' "$PREFIX" "$client" "$name" "$result"
    fi
    printf '%scheck_ok{repo="%s",check="%s"} %s\n' "$PREFIX" "$client" "$name" "$ok"
  done
  [ "$failed" -eq 0 ] || fail "a check did not return a positive number"
  log "$client: drill passed in $((restored - started))s, $bytes bytes restored to ${size:-?} bytes"
}

attempt() {
  client=$1
  every=$2
  max=$4
  jobs=$5
  secret=$6
  prom="$TEXTFILE_DIR/$PREFIX$client.prom"
  since=$7
  success=$8
  begun=$(now)
  wipe
  rm -f "$WORK/result" "$WORK/timed_out"
  $SETSID sh "$SELF" run "$client" "$jobs" "$secret" >"$WORK/result" &
  child=$!
  (
    waited=0
    while [ "$waited" -lt "$max" ]; do
      touch "$WORK/heartbeat"
      sleep "$POLL"
      waited=$((waited + POLL))
    done
    : >"$WORK/timed_out"
    kill -KILL "-$child" 2>/dev/null || kill -KILL "$child" 2>/dev/null
  ) >/dev/null 2>&1 &
  watchdog=$!
  wait "$child"
  code=$?
  kill "$watchdog" 2>/dev/null
  wait "$watchdog" 2>/dev/null
  wipe
  finished=$(now)
  ok=0
  if [ -f "$WORK/timed_out" ]; then
    log "$client: drill failed: stopped after $max seconds"
  elif [ "$code" -eq 0 ]; then
    ok=1
    success=$finished
  fi
  publish "$client" "$every" "$since" "$success" "$begun" "$ok"
}

publish() {
  client=$1
  base=${4:-$3}
  {
    emit since_timestamp_seconds "$3"
    emit deadline_timestamp_seconds $((base + ($2 + 1) * DAY))
    if [ -n "$5" ]; then
      emit last_attempt_timestamp_seconds "$5"
      emit ok "$6"
      grep "^$PREFIX" "$WORK/result" 2>/dev/null
    fi
    [ -z "$4" ] || emit last_success_timestamp_seconds "$4"
  } >"$TEXTFILE_DIR/.$PREFIX$client.prom.tmp" &&
    mv "$TEXTFILE_DIR/.$PREFIX$client.prom.tmp" "$TEXTFILE_DIR/$PREFIX$client.prom"
}

tick() {
  prom="$TEXTFILE_DIR/$PREFIX$1.prom"
  current=$(now)
  since=$(value since_timestamp_seconds "$prom")
  attempted=$(value last_attempt_timestamp_seconds "$prom")
  success=$(value last_success_timestamp_seconds "$prom")
  [ -n "$since" ] || since=$current
  [ -f "$prom" ] || publish "$1" "$2" "$since" "$success" "" ""
  if due "$attempted" "$success" "$2" "$(slot "$3" "$current")"; then
    attempt "$@" "$since" "$success"
  fi
}

main() {
  rm -rf "$WORK"
  mkdir -p "$WORK/checks"
  : >"$WORK/clients"
  . "$CONFIG"
  prune_textfiles
  wipe
  while :; do
    touch "$WORK/heartbeat"
    while read -r name every at max jobs secret <&3; do
      tick "$name" "$every" "$at" "$max" "$jobs" "$secret"
    done 3<"$WORK/clients"
    [ "${AYE_DRILL_ONCE:-}" != 1 ] || return 0
    sleep "$LOOP"
  done
}

if [ "${1:-}" = run ]; then
  shift
  run "$@"
else
  main
fi
