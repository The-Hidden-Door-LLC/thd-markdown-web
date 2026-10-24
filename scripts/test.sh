#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
export PYTHONPATH="$project_dir${PYTHONPATH:+:$PYTHONPATH}"
python_bin="${PYTHON:-$project_dir/.venv/bin/python}"
"$python_bin" -m unittest discover -s tests -v
export GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none
timeout 90s xvfb-run -a -s '-screen 0 1600x1000x24' dbus-run-session -- "$python_bin" tests/gui_checks.py
timeout 90s xvfb-run -a -s '-screen 0 1600x1000x24' dbus-run-session -- "$python_bin" tests/web_gui_checks.py
timeout 60s xvfb-run -a -s '-screen 0 1600x1000x24' dbus-run-session -- "$python_bin" tests/vault_gui_checks.py
