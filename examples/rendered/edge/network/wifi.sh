#!/bin/sh
set -eu

dir="${AYE_TEXTFILE_DIR:-/textfile}"
interval="${AYE_WIFI_INTERVAL:-30}"
port="${AYE_WIFI_PORT:-19102}"
exporter="${AYE_WIFI_EXPORTER:-/bin/node_exporter}"
url="${AYE_WIFI_URL:-http://127.0.0.1:$port/metrics}"
fetch="${AYE_WIFI_FETCH:-wget -q -T 10 -O -}"

"$exporter" "--web.listen-address=127.0.0.1:$port" --collector.disable-defaults --collector.wifi &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
trap 'exit 143' INT TERM

keep="node_wifi_interface_frequency_hertz node_wifi_station_info node_wifi_station_signal_dbm"
keep="$keep node_wifi_station_receive_bits_per_second node_wifi_station_transmit_bits_per_second"
keep="$keep node_wifi_station_connected_seconds_total node_wifi_station_beacon_loss_total"
keep="$keep node_wifi_station_transmit_retries_total node_wifi_station_transmit_failed_total"

collect() {
  $fetch "$url" > "$dir/wifi.raw" || return 1
  awk -v keep="$keep" 'BEGIN { n = split(keep, names, " "); for (i = 1; i <= n; i++) wanted[names[i]] = 1 }
  {
    name = $1
    sub(/[{].*/, "", name)
    if (!(name in wanted)) next
    print
    if (name == "node_wifi_station_signal_dbm" && match($1, /device="[^"]*"/)) {
      printf "ayeops_wifi_signal_dbm{%s} %s\n", substr($1, RSTART, RLENGTH), $2
    }
  }' "$dir/wifi.raw"
}

tries=0
while :; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "wifi: node_exporter exited" >&2
    exit 1
  fi
  if collect > "$dir/wifi.prom.tmp"; then
    mv "$dir/wifi.prom.tmp" "$dir/wifi.prom"
    tries=0
  else
    tries=$((tries + 1))
    echo "wifi: cannot read $url" >&2
    if [ "$tries" -ge "${AYE_WIFI_RETRIES:-10}" ]; then
      exit 1
    fi
    sleep 1
    continue
  fi
  rm -f "$dir/wifi.raw"
  if [ "${AYE_WIFI_ONCE:-0}" = "1" ]; then
    exit 0
  fi
  sleep "$interval"
done
