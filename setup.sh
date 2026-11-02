#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# PyTorch build: xpu (Intel Arc, default), cu128 (NVIDIA) or cpu.
#   ./setup.sh            Intel Arc
#   ./setup.sh cu128      NVIDIA
#   ./setup.sh cpu        no GPU
TORCH_BACKEND="${1:-xpu}"
case "$TORCH_BACKEND" in
    xpu)   TORCH_VERSIONS=(torch==2.14.1 torchaudio==2.11.0) ;;
    cu*|cpu) TORCH_VERSIONS=(torch torchaudio) ;;
    *) echo "error: unknown backend '$TORCH_BACKEND' (use xpu, cu128 or cpu)"; exit 2 ;;
esac

echo "==> mic-polish setup (PyTorch backend: $TORCH_BACKEND)"

# ------------------------------------------------------------
# Requirements
# ------------------------------------------------------------

for tool in ffmpeg python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool is required"; exit 1; }
done

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required"
    echo "Install it first: https://docs.astral.sh/uv/"
    exit 1
fi

# ------------------------------------------------------------
# Python environment
# ------------------------------------------------------------

echo
echo "==> Creating Python 3.12 environment"

uv venv --python 3.12 .venv

# ------------------------------------------------------------
# PyTorch XPU
# ------------------------------------------------------------

echo
echo "==> Installing PyTorch ($TORCH_BACKEND)"

uv pip install \
    --python .venv/bin/python \
    "${TORCH_VERSIONS[@]}" \
    --index-url "https://download.pytorch.org/whl/$TORCH_BACKEND"

# ------------------------------------------------------------
# Runtime dependencies
# ------------------------------------------------------------

echo
echo "==> Installing runtime dependencies"

uv pip install \
    --python .venv/bin/python \
    -r requirements.txt

# ------------------------------------------------------------
# Resemble Enhance
# ------------------------------------------------------------
#
# Resemble Enhance normally tries to install its own old
# PyTorch / DeepSpeed dependencies. We deliberately skip them
# so the PyTorch build installed above remains intact.
# ------------------------------------------------------------

echo
echo "==> Installing Resemble Enhance"

uv pip install \
    --python .venv/bin/python \
    --no-deps \
    resemble-enhance==0.0.1

# ------------------------------------------------------------
# DeepSpeed inference stub
# ------------------------------------------------------------
#
# DeepSpeed is imported by parts of Resemble Enhance related
# to training, but mic-polish only performs inference.
# ------------------------------------------------------------

echo
echo "==> Creating DeepSpeed inference stub"

SP=$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')

mkdir -p "$SP/deepspeed/runtime"

cat > "$SP/deepspeed/__init__.py" <<'PY'
# Inference-only stand-in for DeepSpeed.
# Training is intentionally unsupported.

class DeepSpeedConfig:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "DeepSpeed stub: training is not supported by mic-polish"
        )

def init_distributed(*args, **kwargs):
    raise RuntimeError(
        "DeepSpeed stub: training is not supported by mic-polish"
    )
PY

cat > "$SP/deepspeed/accelerator.py" <<'PY'
def get_accelerator():
    raise RuntimeError("DeepSpeed stub")
PY

touch "$SP/deepspeed/runtime/__init__.py"

cat > "$SP/deepspeed/runtime/utils.py" <<'PY'
def clip_grad_norm_(*args, **kwargs):
    raise RuntimeError("DeepSpeed stub")
PY

cat > "$SP/deepspeed/runtime/engine.py" <<'PY'
class DeepSpeedEngine:
    pass
PY

# ------------------------------------------------------------
# Resemble Enhance weights
# ------------------------------------------------------------

echo
echo "==> Downloading Resemble Enhance model (~680 MB)"

.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download

snapshot_download(
    "ResembleAI/resemble-enhance",
    local_dir="models/resemble-enhance",
    allow_patterns=[
        "enhancer_stage2/*",
        "enhancer_stage2/**",
    ],
)
PY

# ------------------------------------------------------------
# Hardware test
# ------------------------------------------------------------

echo
echo "==> Checking accelerator"

.venv/bin/python - <<'PY'
import torch

print("PyTorch:", torch.__version__)

if hasattr(torch, "xpu") and torch.xpu.is_available():
    print("Accelerator: Intel XPU -", torch.xpu.get_device_name(0))
elif torch.cuda.is_available():
    print("Accelerator: CUDA -", torch.cuda.get_device_name(0))
else:
    print("Accelerator: none, Resemble Enhance will run on the CPU (slower)")
PY

echo
echo "========================================"
echo " mic-polish setup complete"
echo "========================================"
