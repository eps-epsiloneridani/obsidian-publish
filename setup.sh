#!/usr/bin/env bash
#
# setup.sh — bootstrap a fresh machine for obsidian-publish.
#
# Validates dependencies, creates config.env from the committed example if
# absent, runs the test suite, optionally generates an ed25519 deploy keypair,
# and PRINTS (never auto-executes) the remaining manual steps: the target-host
# authorized_keys line, the nginx config, and the systemctl --user install
# commands.
#
# Usage:
#   ./setup.sh
#
# This script is read-only with respect to your system except for:
#   - creating config.env if it is missing (from config.example.env), and
#   - generating a deploy keypair when NOTES_GENERATE_DEPLOY_KEY=true.
# Everything else is printed for you to run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

say() { printf '%s\n' "$*"; }
warn() { printf 'setup: WARN: %s\n' "$*" >&2; }
fail() { printf 'setup: ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Dependencies
# ---------------------------------------------------------------------------
say "==> Checking dependencies"
MISSING=0
for cmd in python3 rsync ssh flock; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        warn "missing required command: $cmd"
        MISSING=1
    fi
done

# python3 >= 3.11 is required (build.py uses datetime.fromisoformat).
if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        say "  python3 $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') OK"
    else
        warn "python3 is older than 3.11 (build.py needs datetime.fromisoformat); found $(python3 --version 2>&1)"
        MISSING=1
    fi
fi

# systemd (user units) is optional — the pipeline also runs via ./deploy.sh.
if command -v systemctl >/dev/null 2>&1; then
    say "  systemctl OK (systemd user units available)"
else
    warn "systemctl not found — you can still publish manually with ./deploy.sh"
fi

[ "$MISSING" -eq 0 ] || fail "install the missing dependencies above and re-run"

# ---------------------------------------------------------------------------
# 2. config.env
# ---------------------------------------------------------------------------
if [ ! -f config.env ]; then
    say "==> Creating config.env from config.example.env"
    cp config.example.env config.env
    chmod 600 config.env
    say ""
    say "Edit config.env now and set at least NOTES_VAULT and NOTES_RSYNC_TARGET"
    say "(and NOTES_SSH_KEY for a remote target), then re-run ./setup.sh."
    say ""
    exit 0
fi
chmod 600 config.env
# shellcheck disable=SC1090,SC1091
. ./config.env

# ---------------------------------------------------------------------------
# 3. Test suite
# ---------------------------------------------------------------------------
say "==> Running test suite"
if python3 -c 'import pytest' 2>/dev/null; then
    python3 -m pytest -q
else
    warn "pytest is not installed (test-only dependency: pipx/pip install pytest); skipping"
fi

# ---------------------------------------------------------------------------
# 4. Deploy keypair (optional)
# ---------------------------------------------------------------------------
GENERATE_KEY="${NOTES_GENERATE_DEPLOY_KEY:-false}"
SSH_KEY="${NOTES_SSH_KEY:-}"
PUB_KEY=""
if [ "$GENERATE_KEY" = "true" ]; then
    if [ -z "$SSH_KEY" ]; then
        fail "NOTES_GENERATE_DEPLOY_KEY=true but NOTES_SSH_KEY is empty"
    fi
    if [ -f "$SSH_KEY" ]; then
        say "==> Deploy key already exists at $SSH_KEY (not overwriting)"
        PUB_KEY="${SSH_KEY}.pub"
    else
        say "==> Generating ed25519 deploy keypair at $SSH_KEY"
        mkdir -p "$(dirname "$SSH_KEY")"
        ssh-keygen -t ed25519 -N "" -C "notes-deploy" -f "$SSH_KEY" >/dev/null
        PUB_KEY="${SSH_KEY}.pub"
    fi
elif [ -n "$SSH_KEY" ] && [ -f "${SSH_KEY}.pub" ]; then
    PUB_KEY="${SSH_KEY}.pub"
fi

# ---------------------------------------------------------------------------
# 5. Remaining manual steps (printed, never executed)
# ---------------------------------------------------------------------------
VAULT="${NOTES_VAULT:-}"
SCOPE="${NOTES_PUBLISH_SCOPE:-web/}"
SCOPE="${SCOPE%/}"
VAULT_WEB="${VAULT:+${VAULT%/}/${SCOPE}}"
DOCROOT="${NOTES_DOCROOT:-/var/www/notes}"
RSYNC_TARGET="${NOTES_RSYNC_TARGET:-}"
DEPLOY_USER="${NOTES_DEPLOY_USER:-}"
DEPLOY_HOST="${NOTES_DEPLOY_HOST:-}"
if [ -z "$RSYNC_TARGET" ] && [ -n "$DEPLOY_USER" ] && [ -n "$DEPLOY_HOST" ]; then
    RSYNC_TARGET="${DEPLOY_USER}@${DEPLOY_HOST}:/"
fi

say ""
say "==> Configuration summary"
say "  vault          : ${VAULT:-<unset — set NOTES_VAULT in config.env>}"
say "  publish scope  : ${SCOPE}"
say "  rsync target   : ${RSYNC_TARGET:-<unset — set NOTES_RSYNC_TARGET>}"
say "  docroot        : ${DOCROOT}"
say "  ssh key        : ${SSH_KEY:-<unset>}"
say ""
say "==> Remaining manual steps"

if [ -n "$PUB_KEY" ] && [ -f "$PUB_KEY" ]; then
    PUB="$(cat "$PUB_KEY")"
    say ""
    say "1. Install the deploy PUBLIC key on the target host as the deploy user,"
    say "   in ~/.ssh/authorized_keys (one line; see deploy/authorized_keys.deploy.example):"
    say ""
    say "     restrict,from=\"<BUILD_HOST_IP>\",command=\"/usr/bin/rrsync ${DOCROOT}/\" ${PUB}"
    say ""
    say "   (<BUILD_HOST_IP> is the address this machine connects FROM; ${DOCROOT}"
    say "    must match the docroot the target serves.)"
else
    say ""
    say "1. Generate a deploy keypair (or set NOTES_GENERATE_DEPLOY_KEY=true and re-run):"
    say "     ssh-keygen -t ed25519 -N \"\" -C notes-deploy -f \"${SSH_KEY:-~/.ssh/id_ed25519_deploy}\""
    say "   then install the .pub on the target host per deploy/authorized_keys.deploy.example:"
    say "     restrict,from=\"<BUILD_HOST_IP>\",command=\"/usr/bin/rrsync ${DOCROOT}/\" <public key>"
fi

say ""
say "2. Configure nginx on the target host to serve ${DOCROOT} as static files:"
say "   copy deploy/nginx-notes.conf.example to /etc/nginx/sites-available/notes,"
say "   fill <SERVER_NAME> and <DOCROOT>, symlink into sites-enabled, then"
say "   nginx -t && systemctl reload nginx."

if command -v systemctl >/dev/null 2>&1; then
    say ""
    say "3. Install the systemd USER units (the path unit watches ${VAULT_WEB}):"
    say ""
    say "     mkdir -p ~/.config/systemd/user"
    say "     sed 's|@VAULT_WEB@|${VAULT_WEB}|' ${SCRIPT_DIR}/dist/notes-publish.path > ~/.config/systemd/user/notes-publish.path"
    say "     cp ${SCRIPT_DIR}/dist/notes-publish.service ~/.config/systemd/user/notes-publish.service"
    say "     systemctl --user daemon-reload"
    say "     systemctl --user enable --now notes-publish.path"
    say ""
    say "   (repo is expected at ~/obsidian-publish per the service unit's %h; adjust"
    say "    ExecStart=/WorkingDirectory= in the installed .service if you cloned elsewhere.)"
    say "   For the trigger to fire without a login session: loginctl enable-linger \"\$USER\""
fi

say ""
say "4. Dry-run, then publish:"
say "     ./deploy.sh --dry-run"
say "     ./deploy.sh"
say ""
say "Done. Nothing was executed except config.env creation and (optionally) key generation."
