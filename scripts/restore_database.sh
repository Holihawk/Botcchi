#!/usr/bin/env bash
# Restore a complete Botcchi PostgreSQL custom-format backup.
#
# Example (the Botcchi service must be stopped first):
#   scripts/restore_database.sh \
#     --dump data/backups/botcchi-20260907T000000Z.dump \
#     --database-url 'postgresql://botcchi:botcchi@127.0.0.1:5432/botcchi' \
#     --yes
#
# `--terminate-connections` is useful for unattended recovery. It disconnects
# every other session from the target database before replacing its contents.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/restore_database.sh --dump PATH [options]

Options:
  --dump PATH                 pg_dump custom-format backup to restore (required)
  --database-url URL          Target PostgreSQL URL. Defaults to DATABASE_URL,
                              then the local Botcchi development database.
  --podman-container NAME     Use psql/pg_restore from this running Podman
                              container (for hosts without PostgreSQL clients).
  --terminate-connections     Disconnect other sessions from the target database.
  --yes                       Confirm that the target database will be overwritten.
  -h, --help                  Show this help text.

The restore uses --clean --if-exists and replaces all schema and data in the
target database. It restores neither PostgreSQL roles nor server configuration.
Stop Botcchi before running it, or use --terminate-connections for a planned
maintenance window.
EOF
}

dump_path=""
database_url="${DATABASE_URL:-postgresql://botcchi:botcchi@127.0.0.1:5432/botcchi}"
terminate_connections=false
confirmed=false
podman_container=""

while (($#)); do
    case "$1" in
        --dump)
            dump_path="${2:?--dump needs a file path}"
            shift 2
            ;;
        --database-url)
            database_url="${2:?--database-url needs a URL}"
            shift 2
            ;;
        --podman-container)
            podman_container="${2:?--podman-container needs a container name}"
            shift 2
            ;;
        --terminate-connections)
            terminate_connections=true
            shift
            ;;
        --yes)
            confirmed=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$dump_path" ]]; then
    printf '%s\n\n' '--dump is required.' >&2
    usage >&2
    exit 2
fi

if [[ ! -r "$dump_path" ]]; then
    printf 'Backup cannot be read: %s\n' "$dump_path" >&2
    exit 1
fi

if [[ "$confirmed" != true ]]; then
    printf '%s\n' 'Refusing to overwrite the target database without --yes.' >&2
    exit 2
fi

# asyncpg URLs are accepted by the app but not by PostgreSQL command-line tools.
if [[ "$database_url" == postgresql+asyncpg://* ]]; then
    database_url="postgresql://${database_url#postgresql+asyncpg://}"
fi

psql_cmd=()
restore_cmd=()
restore_list_cmd=()
cleanup_container_dump=""

if [[ -n "$podman_container" ]]; then
    if ! command -v podman >/dev/null; then
        printf 'Required command not found: podman\n' >&2
        exit 1
    fi
    if ! podman container inspect "$podman_container" >/dev/null 2>&1; then
        printf 'Podman container not found: %s\n' "$podman_container" >&2
        exit 1
    fi
    if [[ "$(podman inspect --format '{{.State.Running}}' "$podman_container")" != true ]]; then
        printf 'Podman container is not running: %s\n' "$podman_container" >&2
        exit 1
    fi
    if ! command -v python3 >/dev/null; then
        printf 'python3 is required when --podman-container is used.\n' >&2
        exit 1
    fi

    # Extract credentials/database from the URL. The database is local to the
    # container, so the container-side client connects to 127.0.0.1:5432.
    mapfile -t url_parts < <(python3 - "$database_url" <<'PY'
from urllib.parse import unquote, urlsplit
import sys
u = urlsplit(sys.argv[1])
print(unquote(u.username or "postgres"))
print(unquote(u.password or ""))
print(unquote(u.path.lstrip("/") or "postgres"))
PY
    )
    db_user="${url_parts[0]}"
    db_password="${url_parts[1]}"
    db_name="${url_parts[2]}"
    container_url="postgresql://${db_user}@127.0.0.1:5432/${db_name}"
    container_dump="/tmp/botcchi-restore-$$.dump"
    podman cp "$dump_path" "$podman_container:$container_dump"
    cleanup_container_dump="$container_dump"
    cleanup() {
        if [[ -n "$cleanup_container_dump" ]]; then
            podman exec "$podman_container" rm -f "$cleanup_container_dump" >/dev/null 2>&1 || true
        fi
    }
    trap cleanup EXIT
    psql_cmd=(podman exec -e "PGPASSWORD=$db_password" "$podman_container" psql)
    restore_cmd=(podman exec -e "PGPASSWORD=$db_password" "$podman_container" pg_restore)
    restore_list_cmd=(podman exec "$podman_container" pg_restore)
    client_database_url="$container_url"
else
    for command in psql pg_restore; do
        if ! command -v "$command" >/dev/null; then
            printf 'Required command not found: %s\n' "$command" >&2
            printf 'Install PostgreSQL client tools or use --podman-container NAME.\n' >&2
            exit 1
        fi
    done
    psql_cmd=(psql)
    restore_cmd=(pg_restore)
    restore_list_cmd=(pg_restore)
    client_database_url="$database_url"
    container_dump="$dump_path"
fi

"${restore_list_cmd[@]}" --list "$container_dump" >/dev/null
target_database=$("${psql_cmd[@]}" "$client_database_url" -v ON_ERROR_STOP=1 -Atqc 'SELECT current_database()')

active_connections=$("${psql_cmd[@]}" "$client_database_url" -v ON_ERROR_STOP=1 -Atqc \
    'SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()')

if [[ "$active_connections" != 0 && "$terminate_connections" != true ]]; then
    printf '%s\n' "Target database '$target_database' has $active_connections other active connection(s)." >&2
    printf '%s\n' 'Stop Botcchi and retry, or rerun with --terminate-connections.' >&2
    exit 1
fi

if [[ "$terminate_connections" == true ]]; then
    "${psql_cmd[@]}" "$client_database_url" -v ON_ERROR_STOP=1 -c \
        'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()' \
        >/dev/null
fi

printf 'Restoring %s into database %s...\n' "$dump_path" "$target_database"
"${restore_cmd[@]}" \
    --dbname="$client_database_url" \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    --exit-on-error \
    --verbose \
    "$container_dump"

printf 'Restore completed successfully.\n'
