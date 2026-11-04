#!/usr/bin/env bash
# Prove the latest backup restores: load it into a scratch database inside the
# same container, check it against production, then drop the scratch DB.
# The workers keep writing after the dump, so the restored copy must match the
# migration version exactly and hold no more rows than production now does
# (and some): engine rows are only ever added.
set -euo pipefail
cd /opt/boro_gtm
latest=$(ls -1t backups/gtm_core_*.dump | head -1)
docker compose exec -T db psql -U gtm -d postgres -qc "DROP DATABASE IF EXISTS gtm_restore_test"
docker compose exec -T db psql -U gtm -d postgres -qc "CREATE DATABASE gtm_restore_test"
docker compose exec -T db pg_restore -U gtm -d gtm_restore_test --no-owner < "$latest"
q="select (select version_num from alembic_version)||' '||(select count(*) from ce_targets)||' '||(select count(*) from ce_messages)||' '||(select count(*) from ce_signals)||' '||(select count(*) from ce_suppressions)"
prod=$(docker compose exec -T db psql -U gtm -d gtm_core -Atc "$q")
rest=$(docker compose exec -T db psql -U gtm -d gtm_restore_test -Atc "$q")
docker compose exec -T db psql -U gtm -d postgres -qc "DROP DATABASE gtm_restore_test"
echo "backup=$latest production=$prod restored=$rest"
read -r pv pt pm ps pz <<< "$prod"
read -r rv rt rm rs rz <<< "$rest"
ok=false
if [ "$pv" = "$rv" ] && [ "$rt" -gt 0 ] && [ "$rt" -le "$pt" ] && [ "$rm" -le "$pm" ] \
   && [ "$rs" -le "$ps" ] && [ "$rz" -le "$pz" ]; then ok=true; fi
# Recorded for the cutover report ('last_restore_test').
docker compose exec -T db psql -U gtm -d gtm_core -qc "INSERT INTO ce_controls (key, value, updated_at)
  VALUES ('last_restore_test', jsonb_build_object('at', now(), 'backup', '${latest}', 'ok', ${ok},
          'production', '${prod}', 'restored', '${rest}'), now())
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
[ "$ok" = true ] || { echo "RESTORE MISMATCH"; exit 1; }
echo "restore ok"
