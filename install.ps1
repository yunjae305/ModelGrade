# 설치 — Windows PowerShell (PowerShell 5.1 · 7 모두)
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# 하는 일: 파이썬 3.10+ 확인 → .venv 만들고 패키지 설치 → .env 만들기 → Codex CLI 설치(없으면, npm 이 있을 때) → 환경 점검(doctor)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "1/4 파이썬 확인"
$py = $null; $pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py"; $pyArgs = @("-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
if (-not $py) { Write-Host "  파이썬이 없습니다 — https://www.python.org/downloads/ 에서 3.12 이상 설치(설치 화면의 'Add python.exe to PATH' 체크) 뒤 다시 실행하세요"; exit 1 }
& $py @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Host "  파이썬 3.10 이상이 필요합니다"; exit 1 }

Write-Host "2/4 가상환경(.venv)과 패키지"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py @pyArgs -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "  패키지 설치 실패 — 위 오류를 확인하세요"; exit 1 }

Write-Host "3/4 .env"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "  .env 를 만들었습니다 — 메모장으로 열어 1차 심판 키(OPENAI_API_KEY 또는 TYPESAFE_API_KEY)를 채우세요" }
else { Write-Host "  .env 있음(그대로 둠)" }

Write-Host "4/4 Codex CLI(2차 심판)"
if ($env:MODELGRADE_SKIP_CODEX) { Write-Host "  건너뜀(MODELGRADE_SKIP_CODEX)" }
elseif (Get-Command codex -ErrorAction SilentlyContinue) { Write-Host "  있음" }
elseif (Get-Command npm -ErrorAction SilentlyContinue) { npm install -g @openai/codex; Write-Host "  설치함 — 'codex login' 으로 ChatGPT 계정에 로그인하세요" }
else { Write-Host "  Node.js(https://nodejs.org) 를 설치한 뒤: npm install -g @openai/codex → codex login" }

Write-Host ""
& .\.venv\Scripts\python.exe -X utf8 tools\doctor.py
Write-Host ""
Write-Host "실행:  .\modelgrade doctor   ·   .\modelgrade login   ·   .\modelgrade start"
