#!/bin/sh
set -eu

dir="${AYE_TEXTFILE_DIR:-/textfile}"
jobs="${AYE_BACKUP_JOBS:-}"
rpos="${AYE_BACKUP_RPO:-}"
interval="${AYE_BACKUP_INTERVAL:-300}"
file="$dir/ayeops_backups.prom"

declared_at() {
  if [ -f "$file" ]; then
    sed -n "s/^ayeops_backup_declared_timestamp_seconds{backup=\"$1\"} \([0-9][0-9]*\)\$/\1/p" "$file" | head -n 1
  fi
}

checked() {
  case "$1" in
    "" | *[!a-z0-9_-]* | [!a-z]*)
      echo "backup-jobs: skipped the job name $1" >&2
      return 1
      ;;
  esac
  case "$2" in
    "" | *[!0-9]*)
      echo "backup-jobs: skipped $1, its $3 $2 is not a number of seconds" >&2
      return 1
      ;;
  esac
}

collect() {
  now=$(date +%s)
  for entry in $jobs; do
    name=${entry%%=*}
    age=${entry#*=}
    checked "$name" "$age" age || continue
    since=$(declared_at "$name")
    echo "ayeops_backup_max_age_seconds{backup=\"$name\"} $age"
    echo "ayeops_backup_declared_timestamp_seconds{backup=\"$name\"} ${since:-$now}"
  done
  for entry in $rpos; do
    name=${entry%%=*}
    rpo=${entry#*=}
    checked "$name" "$rpo" rpo || continue
    echo "ayeops_backup_rpo_seconds{backup=\"$name\"} $rpo"
  done
}

while :; do
  collect > "$file.tmp"
  chmod 0644 "$file.tmp"
  mv "$file.tmp" "$file"
  if [ "${AYE_BACKUP_ONCE:-0}" = "1" ]; then
    exit 0
  fi
  sleep "$interval"
done
