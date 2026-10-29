#!/bin/sh
# Make the bind-mounted output/ writable (Docker creates it as root), then drop root.
set -e
if [ "$(id -u)" = "0" ]; then
    chown app:app /app/output
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
