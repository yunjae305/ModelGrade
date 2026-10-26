#!/usr/bin/env bash
# 설치 — macOS · Linux
#   한 줄 설치(아무 폴더에서):  curl -fsSL https://raw.githubusercontent.com/yunjae305/ModelGrade/main/install.sh | bash
#   받아 둔 폴더에서:           bash install.sh
# 하는 일: (한 줄 설치면 ~/ModelGrade 로 받음) → 파이썬 · Chrome · Node.js 확인(macOS 는 없으면 Homebrew 로 설치) → .venv · 패키지 → .env
#          → Codex CLI · Claude CLI(npm) → 'modelgrade' 명령(~/.local/bin) → 처음 설정(modelgrade setup: 키 · 로그인 · 점검)
# 환경 변수(선택): MODELGRADE_HOME 받을 폴더 · MODELGRADE_SKIP_CODEX · MODELGRADE_SKIP_CLAUDE 설치 건너뜀 · MODELGRADE_NONINTERACTIVE 묻지 않음
set -euo pipefail
REPO="https://github.com/yunjae305/ModelGrade"
OS="$(uname -s)"
has() { command -v "$1" >/dev/null 2>&1; }
brew_install() {   # brew_install "<brew 인자>" "<이름>" — macOS 에서 Homebrew 가 있을 때만
  if [ "$OS" = "Darwin" ] && has brew; then echo "  $2 설치 (brew install $1) …"; brew install $1; return $?; fi
  return 1
}
no_brew_hint() {
  if [ "$OS" = "Darwin" ]; then
    echo "  Homebrew 가 있으면 자동으로 설치합니다 — Homebrew 설치: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  fi
}

# 0) 한 줄 설치 — 저장소 밖에서 불렸으면 받아서 그 폴더의 install.sh 를 실행 (curl | bash 면 키 입력은 터미널에서 받게)
SRC="${BASH_SOURCE[0]:-}"
if [ -z "$SRC" ] || [ ! -f "$(dirname "$SRC")/tools/modelgrade.py" ]; then
  DEST="${MODELGRADE_HOME:-$HOME/ModelGrade}"
  echo "ModelGrade 를 $DEST 에 받습니다"
  if [ -f "$DEST/tools/modelgrade.py" ]; then
    if [ -d "$DEST/.git" ] && has git; then git -C "$DEST" pull --ff-only || true; fi
  else
    has git || brew_install git Git || true
    if has git; then git clone --depth 1 "$REPO.git" "$DEST"
    else
      mkdir -p "$(dirname "$DEST")"
      curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$(dirname "$DEST")"
      mv "$(dirname "$DEST")/ModelGrade-main" "$DEST"
    fi
  fi
  if [ -t 1 ] && { : </dev/tty; } 2>/dev/null; then exec bash "$DEST/install.sh" </dev/tty; else exec bash "$DEST/install.sh"; fi
fi
HERE="$(cd "$(dirname "$SRC")" && pwd)"
cd "$HERE"
QUIET="${MODELGRADE_NONINTERACTIVE:-${CI:-}}"

echo "1/6 파이썬 3.10 이상"
find_py() {
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if has "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then echo "$c"; return 0; fi
  done
  return 1
}
PY="$(find_py || true)"
if [ -z "$PY" ]; then brew_install python@3.12 "파이썬 3.12" && PY="$(find_py || true)"; fi
if [ -z "$PY" ]; then
  echo "  파이썬 3.10 이상이 필요합니다 — macOS: brew install python@3.12 · Ubuntu: sudo apt install python3 python3-venv"; no_brew_hint; exit 1
fi
echo "  $("$PY" --version)"

echo "2/6 가상환경(.venv)과 패키지"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || { echo "  가상환경을 못 만들었습니다 — Ubuntu 면: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

echo "3/6 .env (키 · 로그인 정보 — 값은 마지막 처음 설정에서 묻습니다)"
if [ ! -f .env ]; then cp .env.example .env; echo "  .env 를 만들었습니다"; else echo "  .env 있음(그대로 둠)"; fi

echo "4/6 Google Chrome (답변 수집)"
if .venv/bin/python -X utf8 -c "from tools.chrome_cdp import chrome_path; chrome_path()" 2>/dev/null; then echo "  있음"
elif brew_install "--cask google-chrome" "Google Chrome"; then :
else echo "  Chrome 을 설치하세요 — https://www.google.com/chrome/ (다른 위치면 .env 에 CHROME_PATH)"; no_brew_hint; fi

echo "5/6 Node.js · Codex CLI(2차 심판) · Claude CLI(기준 다듬기 루프, 선택)"
if ! has npm && [ -z "${MODELGRADE_SKIP_CODEX:-}${MODELGRADE_SKIP_CLAUDE:-}" ]; then
  brew_install node Node.js || { echo "  Node.js 를 설치하세요 — https://nodejs.org (Ubuntu: sudo apt install nodejs npm)"; no_brew_hint; }
fi
# 최소 버전 (10-26) — Codex 0.154 는 ChatGPT 계정으로 gpt-6-luna 를 못 부름, Claude Code 2.1.280 미만은 Opus 5.5 없음. 옛 판이면 최신으로 올림
for pair in "codex @openai/codex 0.164.0 ${MODELGRADE_SKIP_CODEX:-}" "claude @anthropic-ai/claude-code 2.1.280 ${MODELGRADE_SKIP_CLAUDE:-}"; do
  set -- $pair
  if [ -n "${4:-}" ]; then echo "  $1: 건너뜀"; continue; fi
  if has "$1"; then
    ver=$("$1" --version 2>/dev/null | head -1 | sed -E 's/^[^0-9]*([0-9]+(\.[0-9]+){1,2}).*$/\1/')
    if [ -n "$ver" ] && [ "$(printf '%s\n%s\n' "$3" "$ver" | sort -V | head -1)" = "$3" ]; then echo "  $1: 있음 ($ver)"; continue; fi
    echo "  $1: ${ver:-?} → 최신으로 올림 ($3 이상 필요)"
  fi
  if ! has npm; then echo "  $1: Node.js 가 없어 못 설치 — Node.js 설치 뒤: npm install -g $2"; continue; fi
  npm install -g "$2@latest" && echo "  $1: 설치함" || echo "  $1 설치 실패 — 권한 오류면: sudo npm install -g $2"
done

echo "6/6 'modelgrade' 명령 (어느 폴더에서나)"
chmod +x modelgrade.sh 2>/dev/null || true
mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/modelgrade.sh" "$HOME/.local/bin/modelgrade"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) echo "  ~/.local/bin/modelgrade — 이미 PATH 에 있음" ;;
  *)
    case "${SHELL:-}" in *zsh) RCS="$HOME/.zshrc" ;; *) RCS="$HOME/.bashrc"; [ "$OS" = "Darwin" ] && RCS="$RCS $HOME/.bash_profile" ;; esac
    for rc in $RCS; do
      grep -qs 'ModelGrade: ~/.local/bin' "$rc" || printf '\n# ModelGrade: ~/.local/bin 을 PATH 에 (modelgrade 명령)\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
    done
    echo "  ~/.local/bin/modelgrade — PATH 에 넣었습니다($RCS). 새 터미널부터 'modelgrade' 로 실행"
    ;;
esac
export PATH="$HOME/.local/bin:$PATH"

echo
if [ -n "$QUIET" ] || ! [ -t 0 ]; then
  .venv/bin/python -X utf8 tools/doctor.py || true
  echo
  echo "다음: 새 터미널에서  modelgrade setup  (키 · 로그인) → modelgrade start"
else
  echo "처음 설정 — 키 · CLI 로그인 · 웹 로그인 · 점검 (나중에 다시: modelgrade setup)"
  ./modelgrade.sh setup || true
  echo
  echo "다음: 새 터미널에서  modelgrade start"
fi
