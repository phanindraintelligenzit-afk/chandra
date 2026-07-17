#!/bin/bash
# User-data script for Chandra inference instance.
# Runs once on first boot to install the LLM inference server.
set -euo pipefail

exec > >(tee /var/log/chandra-inference-bootstrap.log) 2>&1

echo "=== Chandra Inference Bootstrap: $(date) ==="

# ── Configuration (injected by Terraform template) ──────────────────────────
LLM_PROVIDER="${llm_provider}"
MODEL_NAME="${model_name}"
HF_TOKEN="${hf_token}"
API_PORT="${api_port}"
GPU_COUNT="${gpu_count}"
VLLM_ARGS="${vllm_args}"

# ── System dependencies ────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq \
    python3-pip python3-venv python3-dev \
    build-essential git curl wget \
    nvidia-driver-545 nvidia-utils-545 \
    || echo "NVIDIA driver install may have failed — check if GPU AMI is used"

# Install NVIDIA container toolkit (for Docker-based deployments)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit || true

# ── Mount model storage volume ──────────────────────────────────────────────
MODEL_DIR="/models"
if [ -e /dev/sdf ]; then
    mkdir -p "$MODEL_DIR"
    # Check if already formatted (ext4)
    BLKID=$(blkid -o value -s TYPE /dev/sdf 2>/dev/null || echo "")
    if [ "$BLKID" != "ext4" ]; then
        mkfs.ext4 /dev/sdf
    fi
    mount /dev/sdf "$MODEL_DIR"
    echo "/dev/sdf $MODEL_DIR ext4 defaults,nofail 0 2" >> /etc/fstab
    echo "Mounted model volume at $MODEL_DIR"
fi

# ── Install inference backend ───────────────────────────────────────────────
case "$LLM_PROVIDER" in
    vllm)
        echo "=== Installing vLLM ==="
        pip3 install --upgrade pip
        pip3 install vllm torch --extra-index-url https://download.pytorch.org/whl/cu124

        # Create systemd service
        cat > /etc/systemd/system/vllm.service << 'SERVICEEOF'
[Unit]
Description=vLLM Inference Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment="CUDA_VISIBLE_DEVICES=0"
Environment="HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}"
ExecStart=/usr/local/bin/vllm serve ${MODEL_NAME} \
    --host 0.0.0.0 \
    --port ${API_PORT} \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.95 \
    --tensor-parallel-size ${GPU_COUNT} \
    ${VLLM_ARGS}
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

        systemctl daemon-reload
        systemctl enable vllm
        systemctl start vllm
        echo "vLLM service started"
        ;;

    ollama)
        echo "=== Installing Ollama ==="
        curl -fsSL https://ollama.com/install.sh | sh

        # Pull the model and create systemd service
        ollama pull "$MODEL_NAME"

        cat > /etc/systemd/system/ollama.service << 'SERVICEEOF'
[Unit]
Description=Ollama Inference Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment="OLLAMA_HOST=0.0.0.0:${API_PORT}"
Environment="OLLAMA_KEEP_ALIVE=24h"
Environment="OLLAMA_NUM_PARALLEL=4"
ExecStart=/usr/local/bin/ollama serve
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

        systemctl daemon-reload
        systemctl enable ollama
        systemctl restart ollama
        echo "Ollama service started"
        ;;

    llama-cpp)
        echo "=== Installing llama.cpp ==="
        pip3 install --upgrade pip
        pip3 install llama-cpp-python==0.3.6 --extra-index-url https://download.pytorch.org/whl/cu124

        # Download GGUF model
        mkdir -p "$MODEL_DIR/gguf"
        cd "$MODEL_DIR/gguf"

        # Parse model name for GGUF download
        # e.g., "Qwen/Qwen2.5-32B-Coder-Instruct" → "Qwen2.5-32B-Coder-Instruct"
        MODEL_SHORT=$(echo "$MODEL_NAME" | awk -F'/' '{print $NF}')
        GGUF_FILE="${MODEL_SHORT}-Q4_K_M.gguf"
        GGUF_URL="https://huggingface.co/${MODEL_NAME}/resolve/main/${GGUF_FILE}"

        if [ -n "$HF_TOKEN" ]; then
            curl -L -o "$GGUF_FILE" -H "Authorization: Bearer $HF_TOKEN" "$GGUF_URL"
        else
            curl -L -o "$GGUF_FILE" "$GGUF_URL"
        fi

        # Create systemd service
        cat > /etc/systemd/system/llamacpp.service << 'SERVICEEOF'
[Unit]
Description=llama.cpp Inference Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment="CUDA_VISIBLE_DEVICES=0"
ExecStart=/usr/local/bin/llama-server \
    --model ${MODEL_DIR}/gguf/${GGUF_FILE} \
    --host 0.0.0.0 \
    --port ${API_PORT} \
    --ctx-size 32768 \
    --n-gpu-layers 99 \
    --cont-batching \
    --parallel 4
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

        systemctl daemon-reload
        systemctl enable llamacpp
        systemctl start llamacpp
        echo "llama.cpp service started"
        ;;

    *)
        echo "ERROR: Unknown LLM_PROVIDER=$LLM_PROVIDER"
        exit 1
        ;;
esac

# ── Health check ────────────────────────────────────────────────────────────
sleep 15
echo "=== Health check ==="
curl -s "http://localhost:${API_PORT}/v1/models" || echo "Health check failed — check logs"

echo "=== Bootstrap complete: $(date) ==="
echo "API endpoint: http://$(curl -s http://checkip.amazonaws.com):${API_PORT}/v1"