#!/bin/sh
set -eu

# Resolve script directory to allow running from anywhere
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Please create one and install dependencies:"
    echo "python3 -m venv .venv"
    echo ". .venv/bin/activate"
    echo "pip install -r requirements.txt -r requirements-dev.txt"
    # Do not exit here for the bash session to stay alive during testing
fi

# Activate the virtual environment if it exists
if [ -d ".venv" ]; then
    . .venv/bin/activate
fi

echo "Starting Flask debug server"
python -u -m flask --app flask-app run --host=0.0.0.0 -p ${PORT:-8080} --debug
