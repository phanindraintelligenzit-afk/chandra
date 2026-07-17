#!/usr/bin/env bash
# =============================================================================
# Chandra Inference Server — Standalone Setup Script
# =============================================================================
#
# Run this on a fresh EC2 GPU instance (Deep Learning AMI, Ubuntu 22.04).
# This script installs and configures the inference server without requiring
# Terraform — useful for quick experiments or manual deployment.
#
# Usage:
#   chmod +x scripts/setup-inference.sh
#   ./scripts/setup-inference.sh \
#       --provider vllm \
#       --model Qwen/Qwen2.5-32B-Coder-Instruct \
#       --port 8000
#
# Options:
#   --provider   (vllm|ollama|llama-cpp)   default: vllm
#   --model      HuggingFace model ID       default: Qwen/Qwen2.5-32B-Coder-Instruct
#   --port       API port                   default: 8000
#   --gpus       GPU count                  default: 1
#   --hf-token   HuggingFace token          default: (none — for gated models)
#   --quant      Quantization (vLLM only)   default: awq
#   --help       Show this help
# =============================================================================

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────
PROVIDER="vllm"
MODEL="Qwen/Qwen2.5-32B-Coder-Instruct"
PORT=8000
GPUS=1
HF_TOKEN=""
QUANT="awq"

# ── Parse args ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)   PROVIDER="$2";   shift 2 ;;
        --model)      MODEL="$2";      shift 2 ;;
        --port)       PORT="$2";       shift 2 ;;
        --gpus)       GPUS="$2";       shift 2 ;;
        --hf-token)   HF_TOKEN="$2";   shift 2 ;;
        --quant)      QUANT="$2";      shift 2 ;;
        --help)       grep "^#" "$0" | grep -v "^#!/" | sed 's/^# //'; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Chandra Inference Server Setup                             ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Provider: $PROVIDER"
echo "║  Model:    $MODEL"
echo "║  Port:     $PORT"
echo "║  GPUs:     $GPUS"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── Prerequisites ─────────────────────────────────────────────────────────
echo ""
echo "=== Installing system deps ==="
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv curl wget

# Check NVIDIA drivers
if command -v nvidia-smi &>/dev/null; then
    echo "✓ NVIDIA drivers found:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
else
    echo "⚠ NVIDIA drivers not found. Install NVIDIA drivers for GPU acceleration."
    echo "  Or use a Deep Learning AMI from the AWS Marketplace."
fi

# ── Model storage ─────────────────────────────────────────────────────────
MODEL_DIR="/opt/chandra-models"
mkdir -p "$MODEL_DIR"
echo "✓ Model directory: $MODEL_DIR"

# ── Provider-specific install ────────────────────────────────────────────
case "$PROVIDER" in
    vllm)
        echo "=== Installing vLLM ==="
        pip3 install --upgrade pip
        pip3 install vllm torch --extra-index-url https://download.pytorch.org/whl/cu124
        
        # Model quantization flag
        QUANT_FLAG=""
        if [ -n "$QUANT" ]; then
            QUANT_FLAG="--quantization $QUANT"
        fi

        echo "=== Starting vLLM server ==="
        HF_HUB_TOKEN="${HF_TOKEN}" CUDA_VISIBLE_DEVICES="0" nohup \
            vllm serve "$MODEL" \
            --host 0.0.0.0 \
            --port "$PORT" \
            --max-model-len 32768 \
            --gpu-memory-utilization 0.95 \
            --tensor-parallel-size "$GPUS" \
            $QUANT_FLAG \
            > "$MODEL_DIR/vllm.log" 2>&1 &

        echo "vLLM PID: $!"
        echo "Logs: tail -f $MODEL_DIR/vllm.log"
        ;;

    ollama)
        echo "=== Installing Ollama ==="
        curl -fsSL https://ollama.com/install.sh | sh
        export OLLAMA_HOST="0.0.0.0:$PORT"
        
        echo "=== Pulling model: $MODEL ==="
        ollama pull "$MODEL"
        
        echo "=== Starting Ollama server ==="
        systemctl restart ollama 2>/dev/null || \
            OLLAMA_HOST="0.0.0.0:$PORT" OLLAMA_KEEP_ALIVE="24h" \
            nohup ollama serve > "$MODEL_DIR/ollama.log" 2>&1 &
        
        echo "Ollama PID: $!"
        echo "Logs: tail -f $MODEL_DIR/ollama.log"
        ;;

    llama-cpp)
        echo "=== Installing llama.cpp ==="
        pip3 install --upgrade pip
        pip3 install llama-cpp-python==0.3.6 --extra-index-url https://download.pytorch.org/whl/cu124
        
        # Download GGUF
        MODEL_SHORT=$(echo "$MODEL" | awk -F'/' '{print $NF}')
        GGUF_FILE="${MODEL_SHORT}-Q4_K_M.gguf"
        GGUF_URL="https://huggingface.co/${MODEL}/resolve/main/${GGUF_FILE}"
        
        echo "=== Downloading GGUF model ==="
        if [ -n "$HF_TOKEN" ]; then
            curl -L -o "$MODEL_DIR/$GGUF_FILE" -H "Authorization: Bearer $HF_TOKEN" "$GGUF_URL"
        else
            curl -L -o "$MODEL_DIR/$GGUF_FILE" "$GGUF_URL"
        fi
        
        echo "=== Starting llama.cpp server ==="
        CUDA_VISIBLE_DEVICES="0" nohup \
            llama-server \
            --model "$MODEL_DIR/$GGUF_FILE" \
            --host 0.0.0.0 \
            --port "$PORT" \
            --ctx-size 32768 \
            --n-gpu-layers 99 \
            --cont-batching \
            --parallel 4 \
            > "$MODEL_DIR/llamacpp.log" 2>&1 &
        
        echo "llama.cpp PID: $!"
        echo "Logs: tail -f $MODEL_DIR/llamacpp.log"
        ;;
esac

# ── Wait + verify ────────────────────────────────────────────────────────
echo ""
echo "=== Waiting for server to start (30s) ==="
sleep 30

echo "=== Health check ==="
if curl -sf "http://localhost:$PORT/v1/models" > /dev/null 2>&1; then
    echo "✓ Server is running!"
    curl -s "http://localhost:$PORT/v1/models" | python3 -m json.tool 2>/dev/null || true
else
    echo "⚠ Server not responding yet. Check logs:"
    echo "  tail -f $MODEL_DIR/*.log"
    echo "  Or check: systemctl status vllm ollama llamacpp 2>/dev/null"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Setup Complete!                                            ║"
echo "║                                                              ║"
echo "║  API Endpoint:  http://$(curl -s http://checkip.amazonaws.com 2>/dev/null || echo 'localhost'):$PORT/v1"
echo "║  Model:         $MODEL"
echo "║                                                              ║"
echo "║  Update Chandra's .env:                                      ║"
echo "║    LLM_PROVIDER=openai                                       ║"
echo "║    OPENAI_API_BASE=http://<this-instance-ip>:$PORT/v1"
echo "║    LLM_MODEL=$MODEL"
echo "╚══════════════════════════════════════════════════════════════╝"