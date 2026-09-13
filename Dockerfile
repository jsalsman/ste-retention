# Dockerfile
# Deployed on Google Cloud Run.

FROM python:3.14.4-trixie

# Prevent Python from buffering stdout/stderr (important for Cloud Run logging)
ENV PYTHONUNBUFFERED=1

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir --uploaded-prior-to P7D -r requirements.txt

# Copy application code
COPY flask-app.py index.html /app/
COPY static/ /app/static/

# Python compilation check
RUN python -m compileall . -q -j 0

# Validate Jinja templates
RUN python -c "print('import sys\nfrom jinja2 import Environment, FileSystemLoader\nenv = Environment(loader=FileSystemLoader(\".\"))\nfailed = False\nfor template in env.list_templates():\n    if template.endswith(\".html\"):\n        try:\n            env.get_template(template)\n        except Exception as e:\n            print(f\"Syntax error in {template}: {e}\")\n            failed = True\nif failed:\n    sys.exit(1)')" \
    > validate_templates.py \
    && python validate_templates.py \
    && rm validate_templates.py

# Devserver smoketest
RUN set -eux; \
    echo 'Starting dev server' && \
    python -u -m flask --app flask-app run --host=0.0.0.0 -p 8080 & \
    server_pid=$! && \
    trap "kill $server_pid || true" EXIT && \
    echo 'Making sure dev server responds with expected content' && \
    success=0 && \
    i=0; \
    sleep 6; \
    while [ $i -lt 10 ]; do \
      if curl -sS http://localhost:8080 > /tmp/response.html && grep -q "Jim Salsman" /tmp/response.html; then \
        echo "Smoke test passed: expected content found in response."; \
        success=1; \
        break; \
      fi; \
      echo "Waiting for server to start..."; \
      sleep 3; \
      i=$((i+1)); \
    done && \
    if [ $success -eq 0 ]; then \
      echo 'Head and tail of bad response:'; \
      head /tmp/response.html || true; \
      tail /tmp/response.html || true; \
      echo 'Smoke test failed: dev server did not serve expected content.'; \
      exit 1; \
    fi

# Ownership of everything by the non-root user
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

# Keep the sole worker alive for the documented two-minute upload window while
# application-level atlas caps bound synchronous PocketSphinx work per request.
CMD ["python", "-m", "gunicorn", "-b", ":8080", "-k", "gevent", "-w", "1", "--timeout", "120", "flask-app:app"]
