#!/usr/bin/env bash
# 설치 — Linux · macOS
#   bash install.sh
# 하는 일: 파이썬 3.10+ 확인 → .venv 만들고 패키지 설치 → .env 만들기 → Codex CLI 설치(없으면, npm 이 있을 때) → 환경 점검(doctor)
set -euo pipefail
cd "$(dirname "$0")"

echo "1/4 파이썬 확인"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "  파이썬 3.10 이상이 필요합니다 — macOS: brew install python · Ubuntu: sudo apt install python3 python3-venv"; exit 1
fi
echo "  $("$PY" --version)"

echo "2/4 가상환경(.venv)과 패키지"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || { echo "  가상환경을 못 만들었습니다 — Ubuntu 면: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

echo "3/4 .env"
if [ ! -f .env ]; then cp .env.example .env; echo "  .env 를 만들었습니다 — 열어서 1차 심판 키(OPENAI_API_KEY 또는 TYPESAFE_API_KEY)를 채우세요"; else echo "  .env 있음(그대로 둠)"; fi

echo "4/4 Codex CLI(2차 심판)"
if [ -n "${MODELGRADE_SKIP_CODEX:-}" ]; then echo "  건너뜀(MODELGRADE_SKIP_CODEX)"
elif command -v codex >/dev/null 2>&1; then echo "  있음"
elif command -v npm >/dev/null 2>&1; then
  npm install -g @openai/codex || echo "  설치 실패 — 권한 오류면: sudo npm install -g @openai/codex"
  echo "  'codex login' 으로 ChatGPT 계정에 로그인하세요"
else echo "  Node.js(https://nodejs.org) 를 설치한 뒤: npm install -g @openai/codex → codex login"; fi

chmod +x modelgrade.sh 2>/dev/null || true
echo
.venv/bin/python -X utf8 tools/doctor.py || true
echo
echo "실행:  ./modelgrade.sh doctor   ·   ./modelgrade.sh login   ·   ./modelgrade.sh start"
