#!/bin/sh
set -eu

dir="${AYE_BACKUP_DIR:-/backups}"
textfile="${AYE_TEXTFILE_DIR:-/textfile}"
hour="${AYE_BACKUP_HOUR:-3}"
keep="${AYE_BACKUP_KEEP_DAYS:-14}"
interval="${AYE_BACKUP_INTERVAL:-300}"
pg_dump="${AYE_PG_DUMP:-pg_dump}"
run="${AYE_BACKUP_RUN:-/backup_run.sh}"
alive="${AYE_BACKUP_ALIVE:-/tmp/backup.alive}"
clock="${AYE_BACKUP_CLOCK:-date -u +%Y%m%dT%H%MZ}"
job=errors
prefix=glitchtip

umask 077

newest() {
  ls -1 "$dir" 2>/dev/null | grep -E "^$prefix-[0-9]{8}T[0-9]{4}Z[.]dump\$" | sort | tail -n 1 || true
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

dump() {
  stamp=$now
  tmp="$dir/.$prefix-$stamp.dump.tmp"
  file="$dir/$prefix-$stamp.dump"
  if sh "$run" -d "$textfile" -o "$file" "$job" \
    sh -c '"$1" --format=custom --file="$2" && [ -s "$2" ] && mv "$2" "$3"' dump "$pg_dump" "$tmp" "$file"; then
    status=0
  else
    status=$?
  fi
  rm -f "$tmp"
  if [ -s "$file" ]; then
    find "$dir" -maxdepth 1 -name "$prefix-*.dump" -mtime +"$keep" -exec rm -f {} +
    echo "backup: wrote $prefix-$stamp.dump"
  fi
  if [ "$status" -ne 0 ]; then
    echo "backup: backup_run.sh exited $status, retrying in ${interval}s" >&2
  fi
}

while :; do
  now=$(eval "$clock")
  if due; then
    dump
  fi
  touch "$alive"
  if [ "${AYE_BACKUP_ONCE:-0}" = "1" ]; then
    exit 0
  fi
  sleep "$interval"
done
