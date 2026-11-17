#!/usr/bin/env bash
# Idempotent: venv (python 3.12) + listener deps + whisper model warm-up.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/.claude-voice-mode/venv"
MODEL="${VOICE_MODEL:-mlx-community/whisper-large-v3-turbo}"

command -v uv >/dev/null || brew install uv
[ -x "$VENV/bin/python" ] || uv venv "$VENV" --python 3.12
uv pip install --python "$VENV/bin/python" -r "$ROOT/listener/requirements.txt"
"$VENV/bin/python" -c "from huggingface_hub import snapshot_download as d; d('$MODEL'); print('model ready: $MODEL')"
