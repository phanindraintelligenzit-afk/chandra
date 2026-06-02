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

# ── FastAPI on port 6001 ──────────────────────────────────────────────────────
# Redirect stderr to stdout (2>&1) so Render captures the real crash reason.
echo "[start.sh] Starting FastAPI on port 6001..."
/app/.venv/bin/uvicorn fastapi_app:app \
    --host 0.0.0.0 \
    --port 6001 \
    --log-level info \
    2>&1 &
FASTAPI_PID=$!
echo "FastAPI  started (PID $FASTAPI_PID) → http://0.0.0.0:6001"

# Give FastAPI 5 seconds to bind before starting other services
sleep 5

# Check FastAPI actually came up
if ! kill -0 $FASTAPI_PID 2>/dev/null; then
    echo "ERROR: FastAPI process (PID $FASTAPI_PID) died during startup. Check logs above."
    # Continue anyway so Next.js still serves the UI
fi

# ── Gradio on port 7861 ───────────────────────────────────────────────────────
echo "[start.sh] Starting Gradio on port 7861..."
/app/.venv/bin/python app.py 2>&1 &
GRADIO_PID=$!
echo "Gradio   started (PID $GRADIO_PID)  → http://0.0.0.0:7861"

# ── Next.js frontend on $PORT (Render assigns this) ──────────────────────────
FRONTEND_PORT=${PORT:-3000}
echo "[start.sh] Starting Next.js frontend on port $FRONTEND_PORT..."
cd /app/frontend && HOST=0.0.0.0 npm start -- -p $FRONTEND_PORT 2>&1 &
FRONTEND_PID=$!
echo "Frontend started (PID $FRONTEND_PID) → http://0.0.0.0:$FRONTEND_PORT"

# Wait for Next.js (the public-facing service) — if it dies, restart the container
wait $FRONTEND_PID
echo "ERROR: Next.js exited. Shutting down."
kill $FASTAPI_PID $GRADIO_PID 2>/dev/null || true
