#!/bin/sh
# Container entrypoint. Fills an empty database in the background, then serves.
set -eu

if [ "${AUTO_INGEST:-1}" = "1" ] && python -m app.bootstrap; then
  echo "start: corpus is empty, running first ingest in the background"
  python -m app.ingest &
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips='*'
