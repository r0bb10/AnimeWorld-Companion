#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

getent group "$PGID" >/dev/null 2>&1 || groupadd -g "$PGID" appuser
getent passwd "$PUID" >/dev/null 2>&1 || useradd -u "$PUID" -g "$PGID" appuser

for dir in /data /config; do
  if [ -d "$dir" ]; then
    chown -R "${PUID}:${PGID}" "$dir" || true
  fi
done

exec gosu "${PUID}:${PGID}" "$@"
