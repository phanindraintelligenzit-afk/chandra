#!/bin/sh
set -e

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
