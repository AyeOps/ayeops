#!/bin/sh
set -eu

root="${AYE_POWER_SUPPLY_DIR:-/host/sys/class/power_supply}"
dir="${AYE_TEXTFILE_DIR:-/textfile}"
interval="${AYE_BATTERY_INTERVAL:-60}"

read_value() {
  if [ -r "$1" ]; then
    tr -d '\n' < "$1"
  else
    printf '%s' "$2"
  fi
}

collect() {
  found_ac=0
  on_ac=0
  for dev in "$root"/*; do
    [ -r "$dev/type" ] || continue
    name=$(basename "$dev")
    case "$(read_value "$dev/type" Unknown)" in
      Mains|USB)
        found_ac=1
        if [ "$(read_value "$dev/online" 0)" = "1" ]; then
          on_ac=1
        fi
        ;;
      Battery)
        capacity=$(read_value "$dev/capacity" "")
        if [ -n "$capacity" ]; then
          echo "ayeops_battery_capacity_percent{battery=\"$name\"} $capacity"
        fi
        echo "ayeops_battery_status{battery=\"$name\",status=\"$(read_value "$dev/status" Unknown)\"} 1"
        ;;
    esac
  done
  if [ "$found_ac" = "1" ]; then
    echo "ayeops_power_on_ac $on_ac"
  fi
}

while :; do
  collect > "$dir/battery.prom.tmp"
  mv "$dir/battery.prom.tmp" "$dir/battery.prom"
  if [ "${AYE_BATTERY_ONCE:-0}" = "1" ]; then
    exit 0
  fi
  sleep "$interval"
done
