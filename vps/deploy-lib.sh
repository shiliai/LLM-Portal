#!/usr/bin/env bash

restore_file_same_inode() {
  local backup=${1:?backup path required}
  local target=${2:?target path required}
  [ -f "$backup" ] && [ -f "$target" ] || return 1
  cat "$backup" > "$target"
}

converge_console_usage_role() {
  local postgres_user=${POSTGRES_USER:-litellm}
  local postgres_db=${POSTGRES_DB:-litellm}
  : "${CONSOLE_USAGE_PASSWORD:?CONSOLE_USAGE_PASSWORD required}"

  # Compose copies the named variable from its environment. Keep the password
  # out of argv and let psql read it from stdin via \getenv.
  CONSOLE_USAGE_PASSWORD="$CONSOLE_USAGE_PASSWORD" \
    docker compose exec -T -e CONSOLE_USAGE_PASSWORD postgres \
      psql -v ON_ERROR_STOP=1 -U "$postgres_user" -d "$postgres_db" <<'SQL'
\getenv usage_password CONSOLE_USAGE_PASSWORD
SELECT format('CREATE ROLE console_usage LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT', :'usage_password') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='console_usage') \gexec
ALTER ROLE console_usage PASSWORD :'usage_password';
GRANT CONNECT ON DATABASE litellm TO console_usage;
GRANT USAGE ON SCHEMA public TO console_usage;
GRANT SELECT ON TABLE public."LiteLLM_SpendLogs" TO console_usage;
SQL
}
