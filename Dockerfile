# Deployed on Google Cloud Run.
FROM python:3.12-slim

# Prevent Python from buffering stdout/stderr (important for Cloud Run logging)
ENV PYTHONUNBUFFERED=1

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY flask-app.py index.html /app/
COPY ste/ /app/ste/
COPY static/ /app/static/

# Python compilation check
RUN python -m compileall . -q -j 0

# Ownership of everything by the non-root user
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

# Run with Gunicorn using gevent for streaming capabilities
# Keep worker count low, timeout to 120s max for Cloud Run requests
CMD ["sh", "-c", "python -m gunicorn -b :${PORT:-8080} -k gevent -w 1 --timeout 120 flask-app:app"]
