#!/bin/sh
set -eu

goalert migrate
out="$(goalert add-user --admin --user "$AYE_ONCALL_ADMIN_USER" --pass "$AYE_ONCALL_ADMIN_PASSWORD" --email "$AYE_ONCALL_PERSON_EMAIL" 2>&1)" && status=0 || status=$?
if [ "$status" -ne 0 ]; then
  case "$out" in
    *already*|*duplicate*|*exists*)
      echo "goalert: admin $AYE_ONCALL_ADMIN_USER already exists"
      ;;
    *)
      echo "$out" >&2
      exit "$status"
      ;;
  esac
else
  echo "goalert: admin $AYE_ONCALL_ADMIN_USER created"
fi
unset AYE_ONCALL_ADMIN_PASSWORD
exec goalert
