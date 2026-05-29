#!/bin/sh
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
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 &
FASTAPI_PID=$!

# Start Gradio dashboard on port 7861
uv run app.py &
GRADIO_PID=$!

echo "FastAPI  started (PID $FASTAPI_PID) → http://0.0.0.0:6001"
echo "Gradio   started (PID $GRADIO_PID)  → http://0.0.0.0:7861"

# If either process dies, kill the other and exit so Docker can restart the container
wait $FASTAPI_PID $GRADIO_PID
