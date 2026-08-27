#!/usr/bin/env python3
"""run.py — single entry point for the BPO NPI observation planner.

Self-bootstraps ./.venv on first run (so VSCode's ▶ button and `python run.py`
both work with no terminal setup), then re-execs into it and hands off to
scripts/cli.py.

  python run.py                       # solve inputs/targets.csv, publish to sheet
  python run.py --no-publish          # write local CSVs only
  python run.py --cuts inputs/my_cuts.csv   # skip the solver (explicit n_routes)
  python run.py --obs-csv <frozen.csv> --no-publish   # reproduce a frozen run

See README.md for the full flag list and what each output means.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VENV_PY = VENV / "bin" / "python"


def _in_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def _bootstrap() -> None:
    if not VENV_PY.exists():
        print("[run] creating .venv ...", file=sys.stderr)
        req = HERE / "requirements.txt"
        if shutil.which("uv"):
            # System python here lacks ensurepip; uv builds the venv + installs.
            subprocess.check_call(["uv", "venv", str(VENV), "--python", "3.11"])
            subprocess.check_call(["uv", "pip", "install", "-r", str(req)],
                                  env={**os.environ, "VIRTUAL_ENV": str(VENV)})
        else:
            base = "/opt/.pyenv/versions/3.11.13/bin/python3"
            if not Path(base).exists():
                base = shutil.which("python3.11") or sys.executable
            subprocess.check_call([base, "-m", "venv", str(VENV)])
            subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q",
                                   "--upgrade", "pip", "wheel"])
            subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q",
                                   "-r", str(req)])
        print("[run] .venv ready", file=sys.stderr)


def main() -> int:
    if not _in_venv():
        _bootstrap()
        os.execv(str(VENV_PY), [str(VENV_PY), str(HERE / "run.py"), *sys.argv[1:]])
    sys.path.insert(0, str(HERE / "scripts"))
    import cli
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
