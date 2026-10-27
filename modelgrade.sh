#!/usr/bin/env bash
# 옛 이름 — aiona-chat-eval.sh 와 같은 프로그램. 실행:  aiona-chat-eval start   (설치는 bash install.sh — 설치하면 어느 폴더에서나 'aiona-chat-eval' 로 실행, 옛 이름 modelgrade 도 됨)
# ~/.local/bin/aiona-chat-eval 같은 바로가기(심볼릭 링크)로 불려도 진짜 설치 폴더를 찾는다
SRC="$0"
while [ -L "$SRC" ]; do
  DIR="$(cd -P "$(dirname "$SRC")" && pwd)"
  SRC="$(readlink "$SRC")"
  case "$SRC" in /*) ;; *) SRC="$DIR/$SRC" ;; esac
done
HERE="$(cd -P "$(dirname "$SRC")" && pwd)"
export MODELGRADE_HOME="$HERE"   # 다른 폴더에서 실행해도 설치 폴더의 .env 를 읽게
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="$HERE/.venv/Scripts/python.exe"   # Windows 의 Git Bash
if [ ! -x "$PY" ]; then   # .venv 가 없으면 3.10 이상인 파이썬을 찾는다 (Windows 의 python3 는 스토어 안내 프로그램일 수 있음)
  PY=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
  done
  [ -n "$PY" ] || { echo "파이썬 3.10 이상을 찾지 못했습니다 — 설치: bash install.sh" >&2; exit 1; }
fi
exec "$PY" -X utf8 "$HERE/tools/modelgrade.py" "$@"
