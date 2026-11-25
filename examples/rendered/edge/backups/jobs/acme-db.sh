#!/bin/sh
set -eu
AYE_BACKUP_OUTPUT="/var/backups/acme/acme-$(date -u +%Y%m%dT%H%MZ).dump"
export AYE_BACKUP_OUTPUT
exec /bin/sh /opt/ayeops/edge/backups/backup_run.sh -d /var/lib/ayeops/textfile -o "$AYE_BACKUP_OUTPUT" acme-db /bin/sh -c 'runuser -u postgres -- pg_dump --format=custom --file="$AYE_BACKUP_OUTPUT" acme && find /var/backups/acme -name '"'"'acme-*.dump'"'"' -mtime +14 -delete'
