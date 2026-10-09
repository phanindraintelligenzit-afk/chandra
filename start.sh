#!/bin/bash
# Do NOT use set -e — background processes failing should not kill the whole container

# Export FRONTEND_URL if set (for CORS configuration)
if [ -n "$FRONTEND_URL" ]; then
    export FRONTEND_URL="$FRONTEND_URL"
    echo "FRONTEND_URL set to: $FRONTEND_URL"
else
    export FRONTEND_URL="http://localhost:3000"
    echo "FRONTEND_URL not set, defaulting to: $FRONTEND_URL"
fi

# Ensure PYTHONPATH includes both /app (for src.chandra.* imports) and /app/src
export PYTHONPATH="/app:/app/src${PYTHONPATH:+:$PYTHONPATH}"
echo "PYTHONPATH set to: $PYTHONPATH"

# ── Database migrations ───────────────────────────────────────────────────────
echo "[start.sh] Checking/applying database migrations..."
if [ -f "/app/.venv/bin/alembic" ]; then
    /app/.venv/bin/alembic upgrade head 2>&1 || echo "[start.sh] Note: Alembic migration skipped or failed (will auto-create on startup)"
elif command -v alembic &>/dev/null; then
    alembic upgrade head 2>&1 || echo "[start.sh] Note: Alembic migration skipped or failed (will auto-create on startup)"
fi

# ── FastAPI on port 6001 ──────────────────────────────────────────────────────
start_fastapi() {
    echo "[start.sh] Starting FastAPI on port 6001..."
    UVICORN_BIN="/app/.venv/bin/uvicorn"
    if [ ! -f "$UVICORN_BIN" ]; then
        UVICORN_BIN="uvicorn"
    fi
    $UVICORN_BIN fastapi_app:app \
        --host 0.0.0.0 \
        --port 6001 \
        --workers "${UVICORN_WORKERS:-1}" \
        --limit-max-requests 2000 \
        --limit-max-requests-jitter 200 \
        --timeout-keep-alive 65 \
        --log-level info \
        2>&1 &
    FASTAPI_PID=$!
    echo "FastAPI  started (PID $FASTAPI_PID) → http://0.0.0.0:6001"
}

start_fastapi

# Give FastAPI 5 seconds to bind before starting other services
sleep 5

# Check FastAPI actually came up
if ! kill -0 $FASTAPI_PID 2>/dev/null; then
    echo "ERROR: FastAPI process (PID $FASTAPI_PID) died during startup. Check logs above."
fi

# ── Gradio on port 7861 ───────────────────────────────────────────────────────
echo "[start.sh] Starting Gradio on port 7861..."
PYTHON_BIN="/app/.venv/bin/python"
[ -f "$PYTHON_BIN" ] || PYTHON_BIN="python"
$PYTHON_BIN app.py 2>&1 &
GRADIO_PID=$!
echo "Gradio   started (PID $GRADIO_PID)  → http://0.0.0.0:7861"

# ── Next.js frontend on $PORT (Render assigns this) ──────────────────────────
FRONTEND_PORT=${PORT:-3000}
echo "[start.sh] Starting Next.js frontend on port $FRONTEND_PORT..."
cd /app/frontend && HOST=0.0.0.0 npm start -- -p $FRONTEND_PORT 2>&1 &
FRONTEND_PID=$!
echo "Frontend started (PID $FRONTEND_PID) → http://0.0.0.0:$FRONTEND_PORT"

# ── Process supervisor loop ──────────────────────────────────────────────────
# Prevents backend dropping by monitoring and auto-restarting services.
trap "kill $FASTAPI_PID $GRADIO_PID $FRONTEND_PID 2>/dev/null || true; exit 0" SIGINT SIGTERM

while true; do
    # Supervise FastAPI: revive immediately if dropped
    if ! kill -0 $FASTAPI_PID 2>/dev/null; then
        echo "[start.sh] WARNING: FastAPI process ($FASTAPI_PID) dropped! Auto-restarting in 2s..."
        sleep 2
        start_fastapi
    fi

    # Supervise Frontend
    if ! kill -0 $FRONTEND_PID 2>/dev/null; then
        echo "[start.sh] WARNING: Next.js frontend ($FRONTEND_PID) exited. Restarting frontend..."
        sleep 2
        cd /app/frontend && HOST=0.0.0.0 npm start -- -p $FRONTEND_PORT 2>&1 &
        FRONTEND_PID=$!
    fi

    sleep 5
done
