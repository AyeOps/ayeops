#!/bin/sh
set -eu
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
BASE=/opt/ayeops
LIB=/usr/local/libexec/ayeops
SELF=$LIB/aye-host
RULES=grafana/provisioning/alerting/rules.yaml
USAGE="usage: aye-host <$BASE/name> rules|pull|swap|up|ps|diff|timers"

refuse() {
  echo "aye-host: $*" >&2
  exit 64
}

directory() {
  case $1 in
    "$BASE"/*) name=${1#"$BASE"/} ;;
    *) refuse "$1 is not a directory under $BASE" ;;
  esac
  case $name in
    "" | .* | */* | *[!A-Za-z0-9._-]*) refuse "$1 is not a directory under $BASE" ;;
  esac
  if [ -L "$1" ] || [ ! -d "$1" ]; then
    refuse "$1 is not a directory under $BASE"
  fi
}

from_ssh() {
  command=${SSH_ORIGINAL_COMMAND:-}
  case $command in
    *[!A-Za-z0-9._/\ -]*) refuse "this key runs $SELF only" ;;
  esac
  set -f
  IFS=' '
  set -- $command
  set +f
  if [ $# -ne 5 ] || [ "$1" != sudo ] || [ "$2" != -n ] || [ "$3" != "$SELF" ]; then
    refuse "this key runs sudo -n $SELF <directory> <step> only"
  fi
  exec sudo -n "$SELF" "$4" "$5"
}

step() {
  directory "$1"
  cd "$1"
  case $2 in
    rules)
      if [ -f "$RULES" ] && [ ! -L "$RULES" ]; then
        cat "$RULES"
      fi
      ;;
    pull) exec sh "$LIB/pull.sh" "$1" ;;
    swap) exec sh "$LIB/swap.sh" "$1" ;;
    up) exec docker compose up -d --remove-orphans ;;
    ps) exec docker compose ps --all --format json ;;
    diff) exec sh "$LIB/diff.sh" "$1" ;;
    timers) exec sh "$LIB/timers.sh" "$1" ;;
    *) refuse "$USAGE" ;;
  esac
}

if [ $# -eq 1 ] && [ "$1" = ssh ]; then
  from_ssh
fi
if [ $# -ne 2 ]; then
  refuse "$USAGE"
fi
step "$1" "$2"
