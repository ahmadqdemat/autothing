#!/bin/bash
set -e
echo "[INFO] Starting Mentor Home Assistant Add-on..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --no-access-log
