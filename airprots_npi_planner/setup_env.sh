#!/usr/bin/env bash
# Build the asset's ./.venv once. run.py also self-bootstraps, so this is
# optional — but handy for a clean first setup or CI.
# Prefers `uv` (the system python here lacks ensurepip); falls back to pyenv.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if command -v uv >/dev/null 2>&1; then
  echo "Using uv to build $VENV"
  [ -d "$VENV" ] || uv venv "$VENV" --python 3.11
  VIRTUAL_ENV="$VENV" uv pip install -r "$HERE/requirements.txt"
else
  BASE_PY="${BASE_PY:-/opt/.pyenv/versions/3.11.13/bin/python3}"
  [ -x "$BASE_PY" ] || BASE_PY="$(command -v python3.11 || command -v python3)"
  echo "Using $BASE_PY to build $VENV"
  [ -d "$VENV" ] || "$BASE_PY" -m venv "$VENV"
  source "$VENV/bin/activate"
  python -m pip install --upgrade pip wheel
  python -m pip install -r "$HERE/requirements.txt"
fi

echo "Smoke-checking imports ..."
"$VENV/bin/python" - <<'PY'
import importlib
for m in ["pandas", "h3"]:
    mod = importlib.import_module(m)
    print("  OK  %-8s %s" % (m, getattr(mod, "__version__", "?")))
PY
echo "Env ready. Edit inputs/, then run:  python run.py --help"
