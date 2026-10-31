#!/usr/bin/env bash
# NetScrollOffline launcher (Linux/macOS)
cd "$(dirname "$0")"

# If this folder lives on a drive mounted with noexec (common for external disks),
# the virtualenv can't be executed from here. Then we fall back to --user install.
use_venv=1
python3 -m venv .venv 2>/dev/null || use_venv=0
if [ "$use_venv" = "1" ]; then
  if ! .venv/bin/pip --version >/dev/null 2>&1; then
    use_venv=0
    rm -rf .venv
  fi
fi

if [ "$use_venv" = "1" ]; then
  PY=.venv/bin/python
  .venv/bin/pip install -q -r requirements.txt
else
  PY=python3
  python3 -m pip install --user -q -r requirements.txt || \
    python3 -m pip install --break-system-packages -q -r requirements.txt
  if ! python3 -c "import flask" 2>/dev/null; then
    cat <<'EOF'
=================================================
Could not set up dependencies.

Most likely this folder is on a drive mounted with
"noexec" (external/USB disks often are).

Fix: move/copy this whole folder somewhere like your
home directory (e.g. ~/NetScrollOffline) and run
./run.sh again from there.
=================================================
EOF
    exit 1
  fi
fi

"$PY" app.py

