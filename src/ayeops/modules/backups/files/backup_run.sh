#!/bin/sh
set -u

usage() {
  echo "usage: backup_run.sh [-d textfile_dir] -o output name command [argument...]" >&2
  exit 64
}

dir="${AYE_TEXTFILE_DIR:-/var/lib/ayeops/textfile}"
output=""
while getopts d:o: flag; do
  case "$flag" in
    d) dir=$OPTARG ;;
    o) output=$OPTARG ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))
if [ $# -lt 2 ] || [ -z "$output" ]; then
  usage
fi
name=$1
shift
case "$name" in
  *[!a-z0-9_-]* | [!a-z]*)
    echo "backup_run.sh: the job name $name is not like acme-db" >&2
    exit 64
    ;;
esac

file="$dir/ayeops_backup_job_$name.prom"
label="backup=\"$name\""
writable=1
if [ ! -d "$dir" ] || [ ! -w "$dir" ]; then
  writable=0
  echo "backup_run.sh: $dir is not a writable directory, the run of $name is not reported" >&2
fi

kept() {
  if [ -f "$file" ]; then
    grep -v -e '^ayeops_backup_running{' -e '^ayeops_backup_last_start_timestamp_seconds{' "$file" || true
  fi
}

previous_success() {
  if [ -f "$file" ]; then
    sed -n "s/^ayeops_backup_last_success_timestamp_seconds{$label} \([0-9][0-9]*\)\$/\1/p" "$file" | head -n 1
  fi
}

publish() {
  if [ "$writable" != 1 ]; then
    return 0
  fi
  if "$@" > "$file.$$" && chmod 0644 "$file.$$" && mv -f "$file.$$" "$file"; then
    return 0
  fi
  rm -f "$file.$$"
  writable=0
  echo "backup_run.sh: cannot write $file, the run of $name is not reported" >&2
}

size_of() {
  if [ -f "$1" ]; then
    wc -c < "$1" | tr -d ' '
  elif [ -d "$1" ]; then
    du -sk "$1" | awk '{ printf "%.0f\n", $1 * 1024 }'
  else
    echo 0
  fi
}

fresh() {
  if [ -z "$marker" ]; then
    return 0
  fi
  [ -n "$(find "$output" -newer "$marker" 2>/dev/null | head -n 1)" ]
}

started() {
  kept
  echo "ayeops_backup_last_start_timestamp_seconds{$label} $start"
  echo "ayeops_backup_running{$label} 1"
}

finished() {
  echo "ayeops_backup_last_start_timestamp_seconds{$label} $start"
  echo "ayeops_backup_last_end_timestamp_seconds{$label} $end"
  echo "ayeops_backup_last_duration_seconds{$label} $((end - start))"
  echo "ayeops_backup_last_size_bytes{$label} $size"
  echo "ayeops_backup_last_exit_code{$label} $status"
  echo "ayeops_backup_last_run_success{$label} $success"
  echo "ayeops_backup_running{$label} 0"
  if [ -n "$last" ]; then
    echo "ayeops_backup_last_success_timestamp_seconds{$label} $last"
  fi
}

marker=$(mktemp 2>/dev/null) || marker=""
start=$(date +%s)
if [ -n "$marker" ]; then
  touch -d "@$((start - 1))" "$marker" 2>/dev/null || true
fi
publish started
if "$@"; then
  status=0
else
  status=$?
fi
end=$(date +%s)
size=$(size_of "$output")
success=0
if [ "$status" -eq 0 ] && [ "$size" -gt 0 ] && fresh; then
  success=1
fi
if [ -n "$marker" ]; then
  rm -f "$marker"
fi
last=$(previous_success)
if [ "$success" = 1 ]; then
  last=$end
fi
publish finished
if [ "$status" -ne 0 ]; then
  echo "backup_run.sh: $name failed with exit code $status" >&2
  exit "$status"
fi
if [ "$success" != 1 ]; then
  echo "backup_run.sh: $name exited 0 but left no fresh, non-empty $output" >&2
  exit 1
fi
if [ "$writable" != 1 ]; then
  exit 75
fi
exit 0
