#!/bin/sh

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Creating one..."
    python3 -m venv .venv
fi

# Activate the virtual environment
. .venv/bin/activate

if [ -f "requirements.txt" ]; then
    echo "Installing dependencies from requirements.txt..."
    pip install --upgrade pip
    pip install --no-cache-dir --uploaded-prior-to P7D -r requirements.txt
else
    echo "Warning: requirements.txt not found."
fi

echo "Starting Flask debug server"
python -u -m flask --app flask-app run --host=0.0.0.0 -p ${PORT:-8080} --debug
