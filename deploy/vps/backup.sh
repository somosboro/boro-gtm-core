#!/usr/bin/env bash
# Nightly logical backup of the engine database, kept 14 days on the host.
# Off-host: set GTM_BACKUP_REMOTE in .env to an rclone remote (an encrypted
# "crypt" remote over Google Drive is the documented default, see
# docs/RUNBOOK_COMMERCIAL_ENGINE.md); copies there are kept 30 days.
# The outcome is recorded in ce_controls ('last_backup') for the cutover report.
set -euo pipefail
cd /opt/boro_gtm
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="backups/gtm_core_${stamp}.dump"
mkdir -p backups
docker compose exec -T db pg_dump -U gtm -d gtm_core -Fc > "$file"
test -s "$file"
chmod 600 "$file"
find backups -name 'gtm_core_*.dump' -mtime +14 -delete
size=$(stat -c %s "$file")
remote=$(grep -E '^GTM_BACKUP_REMOTE=' .env 2>/dev/null | cut -d= -f2- || true)
remote_ok=false
if [ -n "$remote" ]; then
  if command -v rclone >/dev/null && rclone copy "$file" "${remote}/" --quiet; then
    rclone delete "${remote}/" --min-age 30d --quiet || true
    remote_ok=true
  else
    echo "OFF-HOST COPY FAILED to ${remote}" >&2
  fi
fi
docker compose exec -T db psql -U gtm -d gtm_core -qc "INSERT INTO ce_controls (key, value, updated_at)
  VALUES ('last_backup', jsonb_build_object('at', now(), 'file', '${file}', 'bytes', ${size},
          'remote', '${remote}', 'remote_ok', ${remote_ok}), now())
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
echo "backup ok: ${file} (${size} bytes) off-host=${remote_ok}"
