#!/bin/sh
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
export HOME="${HOME:-/tmp}"

if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
  echo "${TZ}" > /etc/timezone
fi

mkdir -p /config /receipts /import

# Only chown when the mount is not owned correctly yet, so a big archive does
# not get walked on every container start.
for dir in /config /receipts /import; do
  current="$(stat -c '%u:%g' "$dir" 2>/dev/null || echo '?')"
  if [ "$current" != "${PUID}:${PGID}" ]; then
    echo "Setze Rechte auf ${PUID}:${PGID} fuer ${dir}"
    chown -R "${PUID}:${PGID}" "$dir" 2>/dev/null || true
  fi
done

if [ "${1:-}" = "login-web" ]; then
  shift
  exec gosu "${PUID}:${PGID}" /app/login_web.sh "$@"
fi

if [ "${1:-}" = "login" ]; then
  shift
  exec gosu "${PUID}:${PGID}" kaufland --auth-file "${KAUFLAND_AUTH_FILE:-/config/auth.json}" \
       login --country "${KAUFLAND_COUNTRY:-DE}" --language "${KAUFLAND_LANGUAGE:-de}" "$@"
fi

exec gosu "${PUID}:${PGID}" python /app/runner.py "$@"
