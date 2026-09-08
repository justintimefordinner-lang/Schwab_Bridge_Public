#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
#  Commit as:  schwab-bridge/docker-entrypoint.sh   (mode 755)
#
#  Small jobs before the bridge starts. Everything here is best-effort and
#  runs as whatever unprivileged user compose chose, so nothing needs root.
#
#  1. Create the inbox directories under /state so the dashboard's markers have
#     somewhere to land even before Settings has been opened, and keep the two
#     secret files owner-only however the umask created them.
#
#     Skipped when /state is not a writable mount. The build-from-source
#     compose file in the dashboard repo runs this same image with the repo
#     mounted at /bridge and working_dir /bridge, and never mounts /state.
#
#  2. Re-create the /opt/bridge -> /state symlinks if they are absent. They are
#     baked into the image, but a developer bind-mounting a working copy over
#     /opt/bridge hides them, and this puts them back. A real file at one of
#     those names is the developer's own and is left alone.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

if [ -d /state ] && [ -w /state ]; then
  mkdir -p /state/reauth_inbox /state/task_inbox
  [ -f /state/credentials.env ] && chmod 600 /state/credentials.env 2>/dev/null || true
  [ -f /state/token.json ]      && chmod 600 /state/token.json      2>/dev/null || true
elif [ "$(pwd)" = "/state" ]; then
  echo "[entrypoint] WARNING: /state is not writable. Mount ./bridge-state at /state" \
       "and run the container as the user that owns it (see docker-compose.release.yml)." >&2
fi

link() {
  # $1 = name under /state, $2 = name inside /opt/bridge
  [ -L "/opt/bridge/$2" ] && return 0
  [ -e "/opt/bridge/$2" ] && return 0
  ln -s "/state/$1" "/opt/bridge/$2" 2>/dev/null || true
}

if [ -d /opt/bridge ]; then
  link .env             .env
  link credentials.env  credentials.env
  link token.json       token.json
  link .auth_state.json .auth_state.json
  link reauth_inbox     reauth_inbox
  link task_inbox       task_inbox
fi

exec "$@"
