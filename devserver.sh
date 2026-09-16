#!/bin/sh
# Reloading local launcher; install requirements-dev.txt separately before use.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

# Refuse to imply concurrency parity when the local interpreter still has a GIL.
python -c "import sys, sysconfig; assert sysconfig.get_config_var('Py_GIL_DISABLED') == 1 and not sys._is_gil_enabled(), 'Python 3.14t is required'"
# gthread mirrors production with standard native threads throughout.
exec gunicorn --reload --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread \
  --workers 1 --threads "${THREADS:-4}" --timeout 240 flask-app:app
