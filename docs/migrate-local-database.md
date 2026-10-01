# Move the local development database with a SQL dump

This guide copies the app's tables from the local `spotter` PostgreSQL database, including fuel stations, route cache entries, and Django tables, into another PostgreSQL database. It uses a plain `.sql` file. Run the dump commands from the project root, where `docker-compose.yaml` is located.

## Before you start

- The source is the `db` service in `docker-compose.yaml` (`postgis/postgis:18-3.6`). Start it with `docker compose up -d db`.
- Use a **new, empty target database**. Restoring into one with existing Django tables will fail. Do not run `python manage.py migrate` on the empty target before restoring the dump.
- The target server must have PostGIS installed. PostgreSQL 18 and PostGIS 3.6, matching local development, are the simplest choice. Use a PostgreSQL 18 `psql` client for the commands below.
- The target user needs permission to create tables, indexes, and sequences in `public`. A database administrator or hosting provider must enable the `postgis` extension in the target database before the restore if the target user cannot do so.
- The SQL file contains all database data, possibly including account and session data. Keep it outside the repository and share it securely.

## 1. Create the SQL dump

```sh
umask 077
mkdir -p "$HOME/spotter-backups"
backup_file="$HOME/spotter-backups/spotter-$(date +%Y%m%d-%H%M%S).sql"
docker compose exec -T db sh -c 'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=plain --no-owner --no-acl --exclude-schema=topology --exclude-schema=tiger --exclude-schema=tiger_data --exclude-extension="*" --exclude-table=public.spatial_ref_sys' > "$backup_file"
test -s "$backup_file" && ls -lh "$backup_file"
```

The source database name and user come from the running container's `POSTGRES_DB` and `POSTGRES_USER` settings. `--no-owner --no-acl` lets the target user own the restored app objects without needing the source roles. The exclusions leave PostGIS and its optional topology/geocoder objects to the target server; the app uses only the `postgis` extension. Save the printed file path; use that same file for the restore. If `pg_dump` fails, discard the partial file and run the command again.

## 2. Prepare the target

Set these values for the target server in the same shell. Do not put its password in the command or in this repository.

```sh
TARGET_HOST="db.example.com"
TARGET_PORT="5432"
TARGET_USER="spotter_user"
TARGET_DB="spotter"
```

If the target database does not exist and your user can create databases, create it from `template0`:

```sh
createdb -W -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" -T template0 "$TARGET_DB"
```

If a database administrator or hosting provider creates it, ask for an **empty** database instead. If the target requires TLS, set `PGSSLMODE=require` in the shell before connecting. The commands prompt for the target password; a `.pgpass` file is another option for repeated runs.

Enable PostGIS in that database **before** restoring the app dump. Run this as a role allowed to create the extension, or ask the database administrator to run it:

```sql
CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;
```

The target role also needs `USAGE` and `CREATE` on schema `public` if it does not own that schema. Verify that PostGIS is enabled **in `public`** and that the type used by the station table exists:

```sh
psql -X -W -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" -d "$TARGET_DB" \
  -c "SELECT (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis') AS postgis_schema, to_regtype('public.geography') AS geography_type;"
```

Continue only when the query shows `public` in both columns. If either column is empty, ask the database administrator to enable PostGIS in `public` in this **target database**. Installing PostGIS on the server or in another database is not enough. `CREATE EXTENSION IF NOT EXISTS ... WITH SCHEMA public` will not move an existing extension from a different schema; use a fresh target database with PostGIS created in `public` if that is the case.

## 3. Restore the dump

```sh
psql -X -W -v ON_ERROR_STOP=1 --single-transaction \
  -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" -d "$TARGET_DB" \
  -f "$backup_file"
```

`ON_ERROR_STOP` stops at the first SQL error. `--single-transaction` rolls back this restore if any statement fails. If PostGIS types are missing, enable the extension on the target and retry. A failed restore into an otherwise empty target can be retried after fixing its cause.

### If the restore reports `type "public.geography" does not exist`

The target database has no `public.geography` type. Run the preflight query in step 2 against the same `TARGET_DB` used for restore. Enable PostGIS in `public` there, then retry the **same app-only SQL dump**. If the failed restore used `--single-transaction`, its table changes were rolled back. Otherwise, restore into a new empty target database.

### If an earlier dump reports `permission denied for sequence topology_id_seq`

The earlier whole-database dump includes PostGIS topology data and a `setval` call on `topology.topology_id_seq`. That sequence is owned by the PostGIS topology extension on the target. `setval` needs `UPDATE` privilege on the sequence; `--no-owner --no-acl` does not give the restoring role that privilege. **Create a new dump with the command in step 1 and restore that file**; do not reuse the earlier dump. The app does not need topology or tiger-geocoder data.

If the failed restore used the `--single-transaction` command above, its table changes were rolled back. If it was run without that option, start again with a fresh empty target database because some app objects may already exist. An administrator could instead grant the restoring role `UPDATE` on that sequence, but the old dump may hit further permissions on extension-owned objects.

## 4. Verify the target

```sh
psql -X -W -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" -d "$TARGET_DB" \
  -c "SELECT (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis') AS postgis_schema, to_regtype('public.geography') AS geography_type;" \
  -c 'SELECT COUNT(*) AS stations FROM fuel_stations_fuelstation;' \
  -c 'SELECT COUNT(*) AS migrations FROM django_migrations;'
```

Confirm that PostGIS is present and the station and migration counts look right. To check with Django, point its `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` settings at the target, then run `python manage.py migrate --check`. Do not point a running local app at the target until you intend it to use that database.

Finally, update PostgreSQL's query statistics after the import:

```sh
psql -X -W -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" -d "$TARGET_DB" -c 'ANALYZE;'
```

The route cache is copied too, but its entries still expire one day after they were originally created. A copied entry does not get a new expiration time.

## References

- [PostgreSQL 18: SQL dump and restore](https://www.postgresql.org/docs/18/backup-dump.html)
- [PostgreSQL 18: `pg_dump`](https://www.postgresql.org/docs/18/app-pgdump.html)
- [PostgreSQL 18: sequence functions and their privileges](https://www.postgresql.org/docs/18/functions-sequence.html)
- [PostgreSQL 18: `CREATE EXTENSION`](https://www.postgresql.org/docs/18/sql-createextension.html)
