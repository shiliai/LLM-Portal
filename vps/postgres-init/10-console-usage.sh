#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v usage_password="$CONSOLE_USAGE_PASSWORD" <<'SQL'
CREATE ROLE console_usage LOGIN PASSWORD :'usage_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE litellm TO console_usage;
GRANT USAGE ON SCHEMA public TO console_usage;
GRANT SELECT ON TABLE public."LiteLLM_SpendLogs" TO console_usage;
SQL
