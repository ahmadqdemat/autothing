#!/usr/bin/with-contenv bashio

export GEMINI_API_KEY="$(bashio::config 'gemini_api_key')"
export GEMINI_MODEL="$(bashio::config 'gemini_model')"
export DEBATER_INGRESS_ONLY=1

ln -sfn /data /opt/debater/data
cd /opt/debater
exec /opt/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
