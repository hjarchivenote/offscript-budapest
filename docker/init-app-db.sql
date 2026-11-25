-- Run by the official Postgres entrypoint only for a new database volume.
-- Read the mounted secret inside Postgres; never embed it in image or SQL text.
SELECT format(
    'CREATE ROLE offscript WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    rtrim(pg_read_file('/run/secrets/db_password'), E'\r\n')
) \gexec
GRANT CONNECT ON DATABASE offscript TO offscript;
GRANT USAGE, CREATE ON SCHEMA public TO offscript;
