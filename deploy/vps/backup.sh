#!/usr/bin/env bash
# Nightly logical backup of the engine database, kept 14 days on the host.
# Off-host copy: set GTM_BACKUP_REMOTE (an rclone remote, e.g. "b2:boro-gtm")
# once a bucket exists; until then backups live only on this VPS.
set -euo pipefail
cd /opt/boro_gtm
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p backups
docker compose exec -T db pg_dump -U gtm -d gtm_core -Fc > "backups/gtm_core_${stamp}.dump"
test -s "backups/gtm_core_${stamp}.dump"
chmod 600 "backups/gtm_core_${stamp}.dump"
find backups -name 'gtm_core_*.dump' -mtime +14 -delete
if [ -n "${GTM_BACKUP_REMOTE:-}" ] && command -v rclone >/dev/null; then
  rclone copy "backups/gtm_core_${stamp}.dump" "${GTM_BACKUP_REMOTE}/" --quiet
fi
echo "backup ok: backups/gtm_core_${stamp}.dump ($(stat -c %s "backups/gtm_core_${stamp}.dump") bytes)"
