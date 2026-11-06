#!/usr/bin/env bash
#
# One-shot republish pipeline (build -> leak-check -> atomic swap -> rsync).
#
# The single entry point wired into notes-publish.service. It:
#   1. builds the configured vault's publish scope into a staging dir under this
#      repo via build.py (the deny-by-default filter, the fail-closed leak check
#      and the atomic swap all run inside the build; the leak check is the hard
#      gate),
#   2. on success rsyncs the built tree to the configured target over the
#      restricted deploy key (rrsync forces the docroot, so the target path is
#      typically "/"),
#   3. on ANY failure exits non-zero and never rsyncs (fail-closed): the previous
#      site stays live.
#
# Configuration: every site-specific value is read from config.env next to this
# script (gitignored; copy config.example.env -> config.env and edit). NOTES_*
# environment variables override config.env values (used by tests/CI). If a
# required value is missing the script exits non-zero rather than guessing.
#
# Locking: an exclusive flock serialises runs so rapid systemd.path triggers
# cannot interleave two builds/rsyncs. A run that loses the lock exits 0
# ("already in progress" -- the in-flight run wins; the next write re-triggers).
#
# Flags:
#   --dry-run   build + leak-check only; skip the rsync.
#
# Overridable for tests (never touch a real target outside a real deploy):
#   NOTES_CONFIG_FILE, NOTES_VAULT, NOTES_PUBLISH_SCOPE, NOTES_OUT,
#   NOTES_RSYNC_TARGET, NOTES_DEPLOY_USER, NOTES_DEPLOY_HOST, NOTES_SSH_KEY,
#   NOTES_LOCK, NOTES_PYTHON, NOTES_BUILD_SCRIPT.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load gitignored config.env (paths only; the private key lives at the named
# path). Explicit NOTES_* env vars override everything below.
CONFIG_FILE="${NOTES_CONFIG_FILE:-${SCRIPT_DIR}/config.env}"
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090,SC1091
    . "$CONFIG_FILE"
fi

PYTHON="${NOTES_PYTHON:-python3}"
BUILD_SCRIPT="${NOTES_BUILD_SCRIPT:-${SCRIPT_DIR}/build.py}"
VAULT="${NOTES_VAULT:-}"
PUBLISH_SCOPE="${NOTES_PUBLISH_SCOPE:-web/}"
OUT_DIR="${NOTES_OUT:-${SCRIPT_DIR}/out}"
SSH_KEY="${NOTES_SSH_KEY:-}"
DEPLOY_USER_VAL="${NOTES_DEPLOY_USER:-}"
DEPLOY_HOST_VAL="${NOTES_DEPLOY_HOST:-}"
# rrsync on the target forces the docroot, so the rsync target path is "/"
# (relative to the docroot), NOT the absolute docroot path.
RSYNC_TARGET="${NOTES_RSYNC_TARGET:-}"
if [ -z "$RSYNC_TARGET" ] && [ -n "$DEPLOY_USER_VAL" ] && [ -n "$DEPLOY_HOST_VAL" ]; then
    RSYNC_TARGET="${DEPLOY_USER_VAL}@${DEPLOY_HOST_VAL}:/"
fi
LOCK="${NOTES_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/notes-publish.lock}"

is_remote() {
    case "$1" in
        *:*) return 0 ;;
        *) return 1 ;;
    esac
}

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
    shift
fi
if [ "$#" -ne 0 ]; then
    echo "deploy: unexpected arguments: $*" >&2
    exit 2
fi

# --- validate required configuration (fail closed) -------------------------
if [ -z "$VAULT" ] || [ ! -d "$VAULT" ]; then
    echo "deploy: NOTES_VAULT is unset or not a directory; set it in config.env" >&2
    exit 2
fi
if [ -z "$RSYNC_TARGET" ]; then
    echo "deploy: no rsync target; set NOTES_RSYNC_TARGET (or NOTES_DEPLOY_USER + NOTES_DEPLOY_HOST) in config.env" >&2
    exit 2
fi
if [ "$DRY_RUN" -eq 0 ] && is_remote "$RSYNC_TARGET" \
   && { [ -z "$SSH_KEY" ] || [ ! -f "$SSH_KEY" ]; }; then
    echo "deploy: remote target but NOTES_SSH_KEY is unset or not a file; set it in config.env" >&2
    exit 2
fi

# --- serialise (flock) -----------------------------------------------------
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "deploy: another publish is already in progress; skipping" >&2
    exit 0
fi

# --- build (filter + leak check + atomic swap run inside build.py) ---------
echo "deploy: building ${VAULT} -> ${OUT_DIR}" >&2
"$PYTHON" "$BUILD_SCRIPT" "$VAULT" --out "$OUT_DIR" --allowlist "$PUBLISH_SCOPE"

# --- transfer ---------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
    echo "deploy: --dry-run: build + leak-check passed; skipping rsync" >&2
    exit 0
fi

if ! is_remote "$RSYNC_TARGET"; then
    case "$RSYNC_TARGET" in
        */) ;;
        *) RSYNC_TARGET="${RSYNC_TARGET}/" ;;
    esac
    echo "deploy: rsync ${OUT_DIR}/ -> ${RSYNC_TARGET} (local)" >&2
    rsync -az --delete "${OUT_DIR}/" "$RSYNC_TARGET"
else
    echo "deploy: rsync ${OUT_DIR}/ -> ${RSYNC_TARGET} (ssh)" >&2
    rsync -az --delete -e "ssh -i ${SSH_KEY} -o BatchMode=yes" "${OUT_DIR}/" "$RSYNC_TARGET"
fi

echo "deploy: published" >&2
