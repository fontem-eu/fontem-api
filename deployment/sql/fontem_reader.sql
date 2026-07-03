-- Least-privilege read-only role for the unauthenticated Data Studio query
-- proxies (/api/query/sql). Fixes the DAST pentest CRITICAL: the proxy used
-- the superuser `fontem_stats`, which allowed pg_read_file()/pg_ls_dir() —
-- unauthenticated arbitrary file read + /proc/1/environ secret leak.
--
-- A NOSUPERUSER role without pg_read_server_files membership CANNOT execute
-- the filesystem functions, and read_only transactions block writes. Apply
-- against the fontem-stats Postgres, then point STATS_DATABASE_URL at this
-- role (chart value fontemStats.user=fontem_reader + a secret holding its
-- password). Run once per stats DB instance (staging + prod).
--
-- Usage (password via psql var; keep it out of shell history):
--   psql -U fontem_stats -d fontem_stats -v pw="'$READER_PW'" -f fontem_reader.sql

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fontem_reader') THEN
    CREATE ROLE fontem_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END $$;
ALTER ROLE fontem_reader PASSWORD :pw;
ALTER ROLE fontem_reader SET search_path = fontem_stats, public;

GRANT CONNECT ON DATABASE fontem_stats TO fontem_reader;
GRANT USAGE  ON SCHEMA fontem_stats, public, _timescaledb_internal TO fontem_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA fontem_stats, public, _timescaledb_internal TO fontem_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA fontem_stats, public, _timescaledb_internal
  GRANT SELECT ON TABLES TO fontem_reader;

-- Belt-and-suspenders: ensure it is NOT a member of the file/program roles.
REVOKE pg_read_server_files, pg_execute_server_program, pg_read_all_settings
  FROM fontem_reader;
