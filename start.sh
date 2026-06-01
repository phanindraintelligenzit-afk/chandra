#!/bin/bash
set -e

# Export FRONTEND_URL if set (for CORS configuration)
if [ -n "$FRONTEND_URL" ]; then
    export FRONTEND_URL="$FRONTEND_URL"
    echo "FRONTEND_URL set to: $FRONTEND_URL"
else
    export FRONTEND_URL="http://localhost:3000"
    echo "FRONTEND_URL not set, defaulting to: $FRONTEND_URL"
fi

# Start FastAPI on port 6001
uv run uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 &
FASTAPI_PID=$!

# Start Gradio dashboard on port 7861
uv run app.py &
GRADIO_PID=$!

# Start Next.js frontend on port provided by Render (or 3000)
FRONTEND_PORT=${PORT:-3000}
echo "Starting Next.js frontend on port $FRONTEND_PORT..."
cd /app/frontend && HOST=0.0.0.0 npm start -- -p $FRONTEND_PORT &
FRONTEND_PID=$!

echo "FastAPI  started (PID $FASTAPI_PID) → http://0.0.0.0:6001"
echo "Gradio   started (PID $GRADIO_PID)  → http://0.0.0.0:7861"
echo "Frontend started (PID $FRONTEND_PID) → http://0.0.0.0:$FRONTEND_PORT"

# If either process dies, exit so Docker can restart the container
wait -n $FASTAPI_PID $GRADIO_PID $FRONTEND_PID
kill $FASTAPI_PID $GRADIO_PID $FRONTEND_PID 2>/dev/null || true

