#!/bin/sh
# Creates the database used by the integration test suite.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE DATABASE gtm_core_test OWNER $POSTGRES_USER;
SQL
