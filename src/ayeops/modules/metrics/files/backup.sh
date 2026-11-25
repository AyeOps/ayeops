#!/bin/sh
set -eu

dir="${AYE_BACKUP_DIR:-/backups}"
storage="${AYE_STORAGE_DIR:-/storage}"
textfile="${AYE_TEXTFILE_DIR:-/textfile}"
hour="${AYE_BACKUP_HOUR:-2}"
keep="${AYE_BACKUP_KEEP_DAYS:-2}"
days="${AYE_RESTORE_TEST_DAYS:-7}"
retry="${AYE_RESTORE_RETRY:-21600}"
interval="${AYE_BACKUP_INTERVAL:-300}"
delay="${AYE_BACKUP_DELAY:-120}"
store="${AYE_VM_URL:-http://victoriametrics:8428}"
vm="${AYE_VM_BINARY:-/victoria-metrics-prod}"
listen="${AYE_RESTORE_LISTEN:-127.0.0.1:8438}"
patience="${AYE_RESTORE_WAIT:-600}"
fetch="${AYE_BACKUP_FETCH:-wget -q -T 60 -O -}"
run="${AYE_BACKUP_RUN:-/backup_run.sh}"
alive="${AYE_BACKUP_ALIVE:-/tmp/backup.alive}"
clock="${AYE_BACKUP_CLOCK:-date -u +%Y%m%dT%H%MZ}"
seconds="${AYE_RESTORE_CLOCK:-date +%s}"
self=$0
job=metrics
prefix=victoriametrics
scratch="$dir/.restore"
pending="$dir/.snapshot"
report="$textfile/ayeops_backup_restore_$job.prom"
label="backup=\"$job\""
day=86400
query="count%28last_over_time%28up%5B1h%5D%29%29"

umask 077

forget() {
  if $fetch "$store/snapshot/delete?snapshot=$1" > /dev/null; then
    return 0
  fi
  listed=$($fetch "$store/snapshot/list") || return 1
  case "$listed" in
    *"\"$1\""*) return 1 ;;
  esac
}

archive() {
  answer=$($fetch "$store/snapshot/create")
  name=$(printf '%s' "$answer" | sed -n 's/.*"snapshot":"\([0-9A-Za-z_-]*\)".*/\1/p')
  if [ -z "$name" ]; then
    echo "backup: /snapshot/create answered without a snapshot: $answer" >&2
    return 1
  fi
  printf '%s\n' "$name" > "$pending"
  if tar -chf - -C "$storage/snapshots/$name" . > "$1" && [ -s "$1" ]; then
    status=0
  else
    status=1
  fi
  if forget "$name"; then
    rm -f "$pending"
  else
    echo "backup: cannot delete the snapshot $name yet, the next pass tries again" >&2
  fi
  if [ "$status" -ne 0 ]; then
    echo "backup: tar of the snapshot $name failed" >&2
    return 1
  fi
  mv "$1" "$2"
}

if [ "${1:-}" = archive ]; then
  archive "$2" "$3"
  exit 0
fi

leftover() {
  if [ ! -s "$pending" ]; then
    return 0
  fi
  name=$(head -n 1 "$pending")
  if forget "$name"; then
    rm -f "$pending"
    echo "backup: deleted the snapshot $name a stopped run left behind"
  fi
}

newest() {
  ls -1 "$dir" 2>/dev/null | grep -E "^$prefix-[0-9]{8}T[0-9]{4}Z[.]tar\$" | sort | tail -n 1 || true
}

due() {
  latest=$(newest)
  if [ -z "$latest" ]; then
    return 0
  fi
  case "$latest" in
    "$prefix-${now%%T*}"T*) return 1 ;;
  esac
  at=${now#*T}
  at=${at%??Z}
  [ "${at#0}" -ge "$hour" ]
}

busy() {
  (while :; do touch "$alive"; sleep 60; done) > /dev/null 2>&1 &
  toucher=$!
}

idle() {
  kill "$toucher" 2>/dev/null || true
  wait "$toucher" 2>/dev/null || true
}

dump() {
  stamp=$now
  tmp="$dir/.$prefix-$stamp.tar.tmp"
  file="$dir/$prefix-$stamp.tar"
  if sh "$run" -d "$textfile" -o "$file" "$job" sh "$self" archive "$tmp" "$file"; then
    status=0
  else
    status=$?
  fi
  rm -f "$tmp"
  if [ -s "$file" ]; then
    find "$dir" -maxdepth 1 -name "$prefix-*.tar" -mtime +"$keep" -exec rm -f {} +
    echo "backup: wrote $prefix-$stamp.tar"
  fi
  if [ "$status" -ne 0 ]; then
    echo "backup: backup_run.sh exited $status, retrying in ${interval}s" >&2
  fi
}

value() {
  if [ -f "$report" ]; then
    sed -n "s/^ayeops_backup_restore_test_$1{$label} \([0-9][0-9]*\)\$/\1/p" "$report" | head -n 1
  fi
}

load() {
  deadline=$(value deadline_timestamp_seconds)
  success=$(value last_success_timestamp_seconds)
  success=${success:-0}
  attempt=$(value last_attempt_timestamp_seconds)
  ok=$(value ok)
  duration=$(value duration_seconds)
  series=$(value series)
  tested=$(value backup_timestamp_seconds)
}

restore_due() {
  if [ -z "$(newest)" ]; then
    return 1
  fi
  if [ "$success" -gt 0 ] && [ $((epoch - success)) -lt $((days * day)) ]; then
    return 1
  fi
  if [ -n "$attempt" ] && [ $((epoch - attempt)) -lt "$retry" ]; then
    return 1
  fi
  return 0
}

stamp_seconds() {
  stamp=${1#"$prefix"-}
  stamp=${stamp%.tar}
  ymd=${stamp%%T*}
  hm=${stamp#*T}
  hm=${hm%Z}
  md=${ymd#????}
  date -u -d "${ymd%????}-${md%??}-${md#??} ${hm%??}:${hm#??}" +%s
}

opened() {
  "$vm" -storageDataPath="$scratch/storage" -httpListenAddr="$listen" -retentionPeriod=100y \
    > "$scratch/victoria-metrics.log" 2>&1 &
  pid=$!
  waited=0
  while [ "$waited" -lt "$patience" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    if $fetch "http://$listen/health" > /dev/null 2>&1; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

closed() {
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

check() {
  latest=$1
  if ! tested=$(stamp_seconds "$latest"); then
    reason="cannot read the time of $latest"
    return 1
  fi
  mkdir -p "$scratch/storage"
  if ! tar -xf - -C "$scratch/storage" < "$dir/$latest"; then
    reason="cannot extract $latest"
    return 1
  fi
  if ! opened; then
    reason="VictoriaMetrics did not start on $latest"
    tail -n 20 "$scratch/victoria-metrics.log" >&2 || true
    closed
    return 1
  fi
  answer=$($fetch "http://$listen/api/v1/query?query=$query&time=$((tested + 60))" || true)
  closed
  series=$(printf '%s' "$answer" | sed -n 's/.*"value":\[[0-9.]*,"\([0-9][0-9]*\)"\].*/\1/p')
  series=${series:-0}
  if [ "$series" -eq 0 ]; then
    reason="no series of up in the hour before $latest was taken: $answer"
    return 1
  fi
}

restore() {
  latest=$(newest)
  attempt=$epoch
  series=0
  reason=""
  rm -rf "$scratch"
  if check "$latest"; then
    ok=1
    success=$(eval "$seconds")
    echo "backup: restore test passed: $latest holds $series series of up"
  else
    ok=0
    echo "backup: restore test failed: $reason" >&2
  fi
  rm -rf "$scratch"
  duration=$(($(eval "$seconds") - attempt))
}

limit() {
  if [ "$success" -gt 0 ]; then
    deadline=$((success + (days + 1) * day))
  elif [ -z "$deadline" ]; then
    deadline=$((epoch + (days + 1) * day))
  fi
}

lines() {
  echo "ayeops_backup_restore_test_deadline_timestamp_seconds{$label} $deadline"
  echo "ayeops_backup_restore_test_last_success_timestamp_seconds{$label} $success"
  if [ -n "$attempt" ]; then
    echo "ayeops_backup_restore_test_last_attempt_timestamp_seconds{$label} $attempt"
    echo "ayeops_backup_restore_test_ok{$label} ${ok:-0}"
    echo "ayeops_backup_restore_test_duration_seconds{$label} ${duration:-0}"
    echo "ayeops_backup_restore_test_series{$label} ${series:-0}"
    echo "ayeops_backup_restore_test_backup_timestamp_seconds{$label} ${tested:-0}"
  fi
}

publish() {
  limit
  lines > "$report.tmp"
  chmod 0644 "$report.tmp"
  mv "$report.tmp" "$report"
}

load
rm -rf "$scratch"
rm -f "$dir/.$prefix-"*.tar.tmp
epoch=$(eval "$seconds")
publish
touch "$alive"
if [ "${AYE_BACKUP_ONCE:-0}" != "1" ] && [ "$delay" -gt 0 ]; then
  sleep "$delay"
fi

while :; do
  now=$(eval "$clock")
  leftover
  if due; then
    busy
    dump
    idle
  fi
  epoch=$(eval "$seconds")
  if restore_due; then
    busy
    restore
    idle
  fi
  publish
  touch "$alive"
  if [ "${AYE_BACKUP_ONCE:-0}" = "1" ]; then
    exit 0
  fi
  sleep "$interval"
done
