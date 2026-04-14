#!/usr/bin/env bash
# start.sh — one-step setup & launch for K.Y.L.E. NBA Stats
set -e

VENV_DIR=".venv"
PORT=5000

# 0. Kill any process already using port 5000
if lsof -ti ":$PORT" &>/dev/null; then
    echo "Stopping existing process on port $PORT..."
    lsof -ti ":$PORT" | xargs kill -9
    sleep 1
fi

# 1. Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# 2. Install / update dependencies
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q -r requirements.txt

# 3. Start the app (db is initialised automatically inside app.py)
echo "Starting app at http://127.0.0.1:5000"
"$VENV_DIR/bin/python" app.py
