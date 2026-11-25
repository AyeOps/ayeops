#!/bin/sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
script="$here/../aye-report.py"
python=$(command -v python3 || command -v python || true)
if [ -n "${AYE_INPUT_URL:-}" ]; then export AYE_METRICS_URL="$AYE_INPUT_URL"; fi
if [ -n "${AYE_INPUT_TOKEN:-}" ]; then export AYE_METRICS_TOKEN="$AYE_INPUT_TOKEN"; fi
if [ -n "${AYE_INPUT_PROJECT:-}" ]; then export AYE_PROJECT="$AYE_INPUT_PROJECT"; fi
if [ -z "${AYE_METRICS_URL:-}" ] && [ -n "${AYE_OUT_DIR:-}" ]; then
  mkdir -p "$AYE_OUT_DIR"
  export AYE_REPORT_FILE="$AYE_OUT_DIR/report.prom"
fi

report() {
  kind=$1
  shift
  if [ -z "$python" ]; then
    echo "aye report: python3 not found, $kind not reported" >&2
    return 0
  fi
  if [ "${AYE_INPUT_STRICT:-false}" = true ]; then set -- --strict "$@"; fi
  if [ -n "${AYE_INPUT_LABELS:-}" ]; then set -- --labels "$AYE_INPUT_LABELS" "$@"; fi
  if [ -n "${AYE_SUITE:-}" ] && [ "$kind" != run ] && [ "$kind" != pr ]; then
    set -- --suite "$AYE_SUITE" "$@"
  fi
  "$python" -I "$script" "$kind" "$@"
}

case "${1:-}" in
  run)
    if [ -z "$python" ]; then
      echo "aye report: python3 not found, $AYE_STEP runs unreported" >&2
      exec bash --noprofile --norc -eo pipefail -c "$AYE_RUN"
    fi
    report run --name "$AYE_STEP" -- bash --noprofile --norc -eo pipefail -c "$AYE_RUN"
    ;;
  uploads)
    status=0
    if [ -n "${AYE_JUNIT:-}" ]; then
      report junit $AYE_JUNIT || status=1
    fi
    if [ -n "${AYE_COVERAGE:-}" ]; then
      report coverage "$AYE_COVERAGE" || status=1
    fi
    if [ -n "${AYE_MUTANTS:-}" ]; then
      report mutants "$AYE_MUTANTS" || status=1
    fi
    if [ "${AYE_PR:-false}" = true ]; then
      report pr || status=1
    fi
    exit "$status"
    ;;
  *)
    echo "usage: aye.sh run|uploads" >&2
    exit 2
    ;;
esac
