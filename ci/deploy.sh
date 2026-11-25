aye__escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' '
}

aye__labels() {
  aye__out=""
  for aye__pair in "project=${AYE_PROJECT:-}" "repo=${AYE_REPO:-}" "branch=${AYE_BRANCH:-}" "pipeline=${AYE_PIPELINE:-}" "job=${AYE_JOB:-}" "$@"; do
    aye__key=${aye__pair%%=*}
    aye__value=${aye__pair#*=}
    if [ -n "$aye__value" ]; then
      aye__out="${aye__out:+$aye__out,}$aye__key=\"$(aye__escape "$aye__value")\""
    fi
  done
  printf '%s' "$aye__out"
}

aye__url() {
  aye__base=${AYE_METRICS_URL%/}
  case "$aye__base" in
    */api/v1/import/prometheus) printf '%s' "$aye__base" ;;
    *) printf '%s/api/v1/import/prometheus' "$aye__base" ;;
  esac
}

aye__fail() {
  echo "aye report: $1" >&2
  if [ "${AYE_STRICT:-0}" = 1 ]; then return 1; fi
  return 0
}

aye__body() {
  aye__now=$(date +%s)
  aye__ms="${aye__now}000"
  aye__success=0
  if [ "$3" = ok ]; then aye__success=1; fi
  printf 'aye_step_duration_seconds{%s} %s %s\n' "$(aye__labels "result=$3" "step=$1")" "$2" "$aye__ms"
  printf 'aye_step_success{%s} %s %s\n' "$(aye__labels "step=$1")" "$aye__success" "$aye__ms"
  printf 'aye_step_finished_timestamp_seconds{%s} %s %s\n' "$(aye__labels "step=$1")" "$aye__now" "$aye__ms"
  printf 'aye_report_info{%s} 1 %s\n' "$(aye__labels "commit=${AYE_COMMIT:-}" "name=$1" "report=step" "result=$3")" "$aye__ms"
}

aye_report_step() {
  if [ -z "${AYE_METRICS_URL:-}" ]; then
    aye__fail "AYE_METRICS_URL is not set, $1 not pushed"
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    aye__fail "curl not found, $1 not pushed"
    return
  fi
  if [ -n "${AYE_METRICS_TOKEN:-}" ]; then
    aye__body "$@" | curl -fsS --max-time 10 --retry 3 --retry-connrefused -H "Authorization: Bearer $AYE_METRICS_TOKEN" -H "Content-Type: text/plain" --data-binary @- "$(aye__url)" >/dev/null || aye__fail "push of $1 failed"
  else
    aye__body "$@" | curl -fsS --max-time 10 --retry 3 --retry-connrefused -H "Content-Type: text/plain" --data-binary @- "$(aye__url)" >/dev/null || aye__fail "push of $1 failed"
  fi
}

aye_run() {
  aye__name=$1
  shift
  aye__start=$(date +%s)
  if "$@"; then aye__code=0; else aye__code=$?; fi
  aye__result=fail
  if [ "$aye__code" -eq 0 ]; then aye__result=ok; fi
  aye_report_step "$aye__name" "$(( $(date +%s) - aye__start ))" "$aye__result" || return 1
  return "$aye__code"
}
