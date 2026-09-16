# Build a genuine CPython 3.14 free-threaded interpreter from the verified release source.
FROM python:3.14.7-slim-trixie AS free-threaded-builder

ARG PYTHON_VERSION=3.14.7
ARG PYTHON_SHA256=3b48dac8fb59f62eaa67ac83c1eb12bda1b7a08406dd286e252c11a66be27f81
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      build-essential curl libbz2-dev libffi-dev libgdbm-dev liblzma-dev \
      libncursesw5-dev libreadline-dev libsqlite3-dev libssl-dev libzstd-dev \
      tk-dev uuid-dev xz-utils zlib1g-dev; \
    curl -fsSLo /tmp/python.tar.xz \
      "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz"; \
    echo "${PYTHON_SHA256} */tmp/python.tar.xz" | sha256sum -c -; \
    mkdir -p /usr/src/python; \
    tar -xJf /tmp/python.tar.xz -C /usr/src/python --strip-components=1; \
    cd /usr/src/python; \
    ./configure --prefix=/opt/python-ft --disable-gil --enable-optimizations \
      --enable-shared --with-ensurepip=install; \
    make -j "$(nproc)"; \
    make install; \
    ln -s python3.14t /opt/python-ft/bin/python

# Keep the supported slim Python image as the runtime base, but prefer the 3.14t build.
FROM python:3.14.7-slim-trixie

ENV PATH=/opt/python-ft/bin:$PATH \
    LD_LIBRARY_PATH=/opt/python-ft/lib \
    PYTHON_GIL=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=free-threaded-builder /opt/python-ft /opt/python-ft

# Fail the image build if the selected interpreter is not actually free-threaded.
RUN python -c "import sys, sysconfig; assert sysconfig.get_config_var('Py_GIL_DISABLED') == 1; assert not sys._is_gil_enabled()"

# Install only runtime dependencies before copying frequently changed application files.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app

COPY flask-app.py experiment.py leaderboard.py openrouter.py records.py run_lease.py run_store.py scoring.py statistics_utils.py index.html ./
COPY static ./static
RUN chown -R app:app /app

USER app
# Parse the standalone page with Flask's installed Jinja parser as a deployment check.
RUN python -c "from pathlib import Path; from jinja2 import Environment; Environment().parse(Path('index.html').read_text(encoding='utf-8'))"

# Start the production worker briefly and verify both health and expected root content.
RUN set -eu; \
    EXPERIMENTS_DIR=/tmp/experiments gunicorn --bind 127.0.0.1:8080 \
      --worker-class gthread --workers 1 --threads 2 --timeout 30 flask-app:app & \
    server_pid=$!; \
    trap 'kill "$server_pid" 2>/dev/null || true' EXIT; \
    sleep 2; \
    python -c "import json, urllib.request; health=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)); assert health == {'status': 'ok'}; page=urllib.request.urlopen('http://127.0.0.1:8080/', timeout=5).read().decode(); assert 'Jim Salsman' in page"

EXPOSE 8080
# gthread uses native OS threads; exec preserves Cloud Run signal forwarding.
CMD exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers 1 --threads "${THREADS:-4}" --timeout 240 --graceful-timeout 30 flask-app:app
