#!/bin/sh
set -eu

root="${AYE_HOST_ROOT:-/host}"
release_file="${AYE_OSRELEASE_FILE:-/proc/sys/kernel/osrelease}"
dir="${AYE_TEXTFILE_DIR:-/textfile}"
interval="${AYE_REBOOT_INTERVAL:-600}"

newest_kernel() {
  for base in "$root/usr/lib/modules" "$root/lib/modules"; do
    if [ -d "$base" ]; then
      ls -1 "$base"
      return 0
    fi
  done
}

collect() {
  flag=0
  if [ -e "$root/run/reboot-required" ] || [ -e "$root/var/run/reboot-required" ]; then
    flag=1
  fi
  running=$(tr -d '\n' < "$release_file")
  kernel=$(newest_kernel | awk -v running="$running" '
    function newer(a, b,   x, y, n, m, i, k) {
      n = split(a, x, /[^0-9A-Za-z]+/)
      m = split(b, y, /[^0-9A-Za-z]+/)
      k = n > m ? n : m
      for (i = 1; i <= k; i++) {
        if (x[i] == y[i]) continue
        if (x[i] ~ /^[0-9]+$/ && y[i] ~ /^[0-9]+$/) return x[i] + 0 > y[i] + 0
        return x[i] > y[i]
      }
      return 0
    }
    /^[0-9]/ { if (best == "" || newer($0, best)) best = $0 }
    END { print (best != "" && newer(best, running)) ? 1 : 0 }
  ')
  echo "ayeops_reboot_required{reason=\"flag\"} $flag"
  echo "ayeops_reboot_required{reason=\"kernel\"} $kernel"
}

while :; do
  collect > "$dir/reboot.prom.tmp"
  mv "$dir/reboot.prom.tmp" "$dir/reboot.prom"
  if [ "${AYE_REBOOT_ONCE:-0}" = "1" ]; then
    exit 0
  fi
  sleep "$interval"
done
