# Standard CPython plus gthread is appropriate for this network/filesystem-bound service.
FROM python:3.14.7-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies separately for effective layer caching.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app

COPY flask-app.py index.html leaderboard_preview.html ./
COPY originals/leaderboard_preview.html ./originals/leaderboard_preview.html
COPY ste ./ste
COPY static ./static
RUN chown -R app:app /app
USER app

# Parse every shipped HTML document as Jinja syntax, including documents that do
# not use template expressions, then compile all application modules.
RUN python -c "from pathlib import Path; from jinja2 import Environment; environment = Environment(); [environment.parse(path.read_text(encoding='utf-8')) for path in Path('.').rglob('*.html')]" \
    && python -m compileall -q flask-app.py ste

# Start the production server during the image build and smoke-test both the
# Cloud Run health endpoint and the root application document.
RUN set -eu; \
    gunicorn --bind 127.0.0.1:8080 --worker-class gthread --workers 1 \
      --threads 2 --timeout 30 flask-app:app & \
    server_pid=$!; \
    trap 'kill "$server_pid" 2>/dev/null || true' EXIT; \
    sleep 2; \
    python -c "import json, urllib.request; url='http://127.0.0.1:8080/api/healthz'; health=json.load(urllib.request.urlopen(url, timeout=5)); assert health == {'status': 'ok'}; page=urllib.request.urlopen('http://127.0.0.1:8080/', timeout=5).read().decode(); assert 'Jim Salsman' in page"

EXPOSE 8080
# gthread efficiently overlaps provider and disk waits; exec preserves signal forwarding.
CMD exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers "${WORKERS:-1}" --threads "${THREADS:-4}" --timeout 270 --graceful-timeout 30 flask-app:app
