#!/bin/sh
# Reloading local launcher; install requirements-dev.txt separately before use.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

# Standard CPython gthread mirrors the production I/O-concurrency model.
exec gunicorn --reload --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread \
  --workers 1 --threads "${THREADS:-4}" --timeout 270 --graceful-timeout 30 flask-app:app
