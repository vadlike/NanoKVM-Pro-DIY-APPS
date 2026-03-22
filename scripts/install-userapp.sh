#!/usr/bin/env sh
set -eu

REPO_OWNER="${REPO_OWNER:-vadlike}"
REPO_NAME="${REPO_NAME:-NanoKVM-Pro-DIY-APPS}"
REPO_REF="${REPO_REF:-main}"
DEST_ROOT="${DEST_ROOT:-/userapp}"
BACKUP_ROOT="${BACKUP_ROOT:-$DEST_ROOT/.install-backup}"

TMP_DIR=""

cleanup() {
    if [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ]; then
        rm -rf "${TMP_DIR}"
    fi
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

download_file() {
    url="$1"
    output="$2"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$output"
        return
    fi

    if command -v wget >/dev/null 2>&1; then
        wget -qO "$output" "$url"
        return
    fi

    fail "curl or wget is required on the NanoKVM device"
}

install_app() {
    app_name="$1"
    src_dir="${APPS_ROOT}/${app_name}"
    dest_dir="${DEST_ROOT}/${app_name}"
    stage_dir="${TMP_DIR}/stage-${app_name}"
    backup_dir=""

    [ -d "${src_dir}" ] || fail "app '${app_name}' not found in repo"
    [ -f "${src_dir}/main.py" ] || fail "app '${app_name}' is missing main.py"
    [ -f "${src_dir}/app.toml" ] || fail "app '${app_name}' is missing app.toml"

    mkdir -p "${stage_dir}"
    cp -R "${src_dir}/." "${stage_dir}/"

    if [ -f "${dest_dir}/config.json" ] && [ ! -f "${stage_dir}/config.json" ]; then
        cp "${dest_dir}/config.json" "${stage_dir}/config.json"
    fi

    if [ -d "${dest_dir}" ]; then
        mkdir -p "${BACKUP_ROOT}"
        backup_dir="${BACKUP_ROOT}/${app_name}-$(date +%Y%m%d-%H%M%S)"
        mv "${dest_dir}" "${backup_dir}"
    fi

    mv "${stage_dir}" "${dest_dir}"
    chmod -R a+rX "${dest_dir}"

    echo "Installed ${app_name} -> ${dest_dir}"
    if [ -n "${backup_dir}" ]; then
        echo "Backup saved -> ${backup_dir}"
    fi
    if [ -f "${dest_dir}/config.example.json" ] && [ ! -f "${dest_dir}/config.json" ]; then
        echo "Note: ${app_name} has config.example.json. Create config.json if the app needs local settings."
    fi
}

usage() {
    cat <<EOF
Usage:
  sh install-userapp.sh all
  sh install-userapp.sh <app-name> [<app-name> ...]

Environment variables:
  REPO_OWNER   GitHub owner, default: ${REPO_OWNER}
  REPO_NAME    GitHub repo, default: ${REPO_NAME}
  REPO_REF     Branch/tag/commit, default: ${REPO_REF}
  DEST_ROOT    Install directory, default: ${DEST_ROOT}
  BACKUP_ROOT  Backup directory, default: ${BACKUP_ROOT}
EOF
}

if [ "$#" -lt 1 ]; then
    usage
    exit 1
fi

trap cleanup EXIT INT TERM

TMP_DIR="$(mktemp -d /tmp/nanokvm-userapp.XXXXXX)"
ARCHIVE_PATH="${TMP_DIR}/repo.tar.gz"
ARCHIVE_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/${REPO_REF}"

echo "Downloading ${ARCHIVE_URL}"
download_file "${ARCHIVE_URL}" "${ARCHIVE_PATH}"

tar -xzf "${ARCHIVE_PATH}" -C "${TMP_DIR}"

REPO_DIR="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -n "${REPO_DIR}" ] || fail "failed to unpack repository archive"

APPS_ROOT="${REPO_DIR}/apps"
[ -d "${APPS_ROOT}" ] || fail "apps directory not found in repository archive"

mkdir -p "${DEST_ROOT}"

if [ "$1" = "all" ]; then
    find "${APPS_ROOT}" -mindepth 1 -maxdepth 1 -type d | sort | while IFS= read -r app_dir; do
        app_name="$(basename "${app_dir}")"
        install_app "${app_name}"
    done
else
    for app_name in "$@"; do
        install_app "${app_name}"
    done
fi

echo "Done."
