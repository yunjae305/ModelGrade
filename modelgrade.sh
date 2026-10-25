#!/usr/bin/env bash
# 실행 — Linux · macOS:  ./modelgrade.sh start   (설치는 bash install.sh)
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
exec "$PY" -X utf8 "$HERE/tools/modelgrade.py" "$@"
