#!/bin/sh
set -e

export PORT=8099
export DATA_DIR="/data"
export FRONTEND_DIR="/app/frontend"

echo "[Power Tracker] Starting..."
exec python3 /app/app/main.py
