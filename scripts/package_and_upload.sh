#!/usr/bin/env bash
# Package Botcchi for a server deployment and upload it over SSH.
#
# Defaults intentionally use placeholders. Replace them here or pass --host,
# --user and --remote-dir when invoking the script.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/package_and_upload.sh [options]

Options:
  --host HOST            SSH host; default: a.b.c.d
  --user USER            SSH user; default: deploy
  --remote-dir PATH      Remote release directory; default: /opt/botcchi/releases
  --backup PATH          Also upload this database backup to REMOTE-DIR/backups/
  --no-upload            Build and retain the archive without connecting to SSH.
  --remove-local         Delete the local archive after a successful upload.
  -h, --help             Show this help text.

The archive includes source code, runtime configuration, data resources and
web/dist. It excludes virtual environments, cache files, Git metadata, logs,
database backups, legacy source, tests and frontend development dependencies.

Examples:
  scripts/package_and_upload.sh --host 203.0.113.10 --user ubuntu
  scripts/package_and_upload.sh --host a.b.c.d --backup data/backups/botcchi-<timestamp>.dump
  scripts/package_and_upload.sh --host a.b.c.d --user root --remote-dir /root \
    --backup data/backups/botcchi-<timestamp>.dump
EOF
}

host="a.b.c.d"
remote_user="deploy"
remote_dir="/opt/botcchi/releases"
backup_path=""
upload=true
remove_local=false

while (($#)); do
    case "$1" in
        --host)
            host="${2:?--host needs a host name or IP address}"
            shift 2
            ;;
        --user)
            remote_user="${2:?--user needs an SSH user}"
            shift 2
            ;;
        --remote-dir)
            remote_dir="${2:?--remote-dir needs a remote path}"
            shift 2
            ;;
        --backup)
            backup_path="${2:?--backup needs a file path}"
            shift 2
            ;;
        --no-upload)
            upload=false
            shift
            ;;
        --remove-local)
            remove_local=true
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

if [[ -n "$backup_path" && ! -r "$backup_path" ]]; then
    printf 'Database backup cannot be read: %s\n' "$backup_path" >&2
    exit 1
fi

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
project_name=$(basename "$project_dir")
release_dir="$project_dir/data/releases"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$release_dir/${project_name}-deploy-${timestamp}.tar.gz"
destination="${remote_user}@${host}"

mkdir -p "$release_dir"

exclude_args=(
    --exclude-vcs
    --exclude="$project_name/.venv"
    --exclude="$project_name/.pytest_cache"
    --exclude="$project_name/.ruff_cache"
    --exclude="$project_name/__pycache__"
    --exclude="$project_name/**/__pycache__"
    --exclude="$project_name/**/*.pyc"
    --exclude="$project_name/Botcchi"
    --exclude="$project_name/tests"
    --exclude="$project_name/.vscode"
    --exclude="$project_name/dist"
    --exclude="$project_name/web/node_modules"
    --exclude="$project_name/data/cache"
    --exclude="$project_name/data/temp"
    --exclude="$project_name/data/logs"
    --exclude="$project_name/data/backups"
    --exclude="$project_name/data/releases"
    --exclude="$project_name/log"
)

printf 'Creating deployment archive: %s\n' "$archive"
tar --create --gzip --file="$archive" \
    "${exclude_args[@]}" \
    --directory="$(dirname "$project_dir")" \
    "$project_name"
chmod 0600 "$archive"

printf 'Archive size: %s\n' "$(du -h "$archive" | cut -f1)"

if [[ "$upload" == false ]]; then
    printf 'Archive created without upload: %s\n' "$archive"
    exit 0
fi

for command in ssh scp; do
    if ! command -v "$command" >/dev/null; then
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    fi
done

printf 'Uploading to %s:%s/\n' "$destination" "$remote_dir"
ssh "$destination" mkdir -p "$remote_dir"
scp "$archive" "$destination:$remote_dir/"

if [[ -n "$backup_path" ]]; then
    printf 'Uploading database backup to %s:%s/backups/\n' "$destination" "$remote_dir"
    ssh "$destination" mkdir -p "$remote_dir/backups"
    scp "$backup_path" "$destination:$remote_dir/backups/"
fi

printf '\nUploaded archive: %s:%s/%s\n' "$destination" "$remote_dir" "$(basename "$archive")"
printf '%s\n' 'Extract it on the server with:'
printf '  mkdir -p /opt/botcchi && tar -xzf %s/%s -C /opt/botcchi\n' \
    "$remote_dir" "$(basename "$archive")"

if [[ "$remove_local" == true ]]; then
    rm -- "$archive"
    printf 'Removed local archive after upload.\n'
fi
