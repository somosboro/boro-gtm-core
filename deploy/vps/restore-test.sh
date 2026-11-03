#!/usr/bin/env bash
# Prove the latest backup restores: load it into a scratch database inside the
# same container, compare row counts with production, then drop the scratch DB.
set -euo pipefail
cd /opt/boro_gtm
latest=$(ls -1t backups/gtm_core_*.dump | head -1)
docker compose exec -T db psql -U gtm -d postgres -qc "DROP DATABASE IF EXISTS gtm_restore_test"
docker compose exec -T db psql -U gtm -d postgres -qc "CREATE DATABASE gtm_restore_test"
docker compose exec -T db pg_restore -U gtm -d gtm_restore_test --no-owner < "$latest"
q="select (select count(*) from alembic_version)||'/'||(select count(*) from ce_targets)||'/'||(select count(*) from ce_messages)||'/'||(select count(*) from ce_signals)"
prod=$(docker compose exec -T db psql -U gtm -d gtm_core -Atc "$q")
rest=$(docker compose exec -T db psql -U gtm -d gtm_restore_test -Atc "$q")
docker compose exec -T db psql -U gtm -d postgres -qc "DROP DATABASE gtm_restore_test"
echo "backup=$latest production=$prod restored=$rest"
[ "$prod" = "$rest" ] || { echo "RESTORE MISMATCH (writes may have happened since the dump)"; exit 1; }
echo "restore ok"
