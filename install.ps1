# 설치 — Windows (PowerShell 5.1 · 7 모두)
#   한 줄 설치(아무 폴더에서):  irm https://raw.githubusercontent.com/yunjae305/ModelGrade/main/install.ps1 | iex
#   받아 둔 폴더에서:           powershell -ExecutionPolicy Bypass -File .\install.ps1
# 하는 일: (한 줄 설치면 ~\ModelGrade 로 받음) → 파이썬 · Chrome · Node.js 확인(없으면 winget 으로 설치) → .venv · 패키지 → .env
#          → Codex CLI · Claude CLI(npm) → 'modelgrade' 명령을 PATH 에 → 처음 설정(modelgrade setup: 키 · 로그인 · 점검)
# 환경 변수(선택): MODELGRADE_HOME 받을 폴더 · MODELGRADE_SKIP_CODEX · MODELGRADE_SKIP_CLAUDE 설치 건너뜀 · MODELGRADE_NONINTERACTIVE 묻지 않음
$ErrorActionPreference = "Continue"   # 네이티브 명령의 오류 글만으로 멈추지 않게(PS 5.1) — 단계마다 종료 코드로 판단
$Repo = "https://github.com/yunjae305/ModelGrade"

function Has($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") }
function Winget-Install($id, $what) {
  if (-not (Has winget)) { Write-Host "  $what 이(가) 없고 winget 도 없습니다 — $what 을(를) 설치한 뒤 다시 실행하세요"; return $false }
  Write-Host "  $what 설치 (winget install $id) …"
  winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
  Refresh-Path
  return $true
}

# 0) 한 줄 설치 — 저장소 밖에서 불렸으면 받아서 그 폴더의 install.ps1 을 새 창 없이 실행 (irm | iex 에서 exit 로 창이 닫히지 않게)
$here = $PSScriptRoot
if (-not $here -or -not (Test-Path (Join-Path $here "tools\modelgrade.py"))) {
  $dest = if ($env:MODELGRADE_HOME) { $env:MODELGRADE_HOME } else { Join-Path $HOME "ModelGrade" }
  Write-Host "ModelGrade 를 $dest 에 받습니다"
  if (Test-Path (Join-Path $dest "tools\modelgrade.py")) {
    if ((Test-Path (Join-Path $dest ".git")) -and (Has git)) { git -C $dest pull --ff-only | Out-Host }
  } else {
    if (-not (Has git)) { Winget-Install "Git.Git" "Git" | Out-Null }
    if (Has git) { git clone --depth 1 "$Repo.git" $dest | Out-Host }
    else {
      $zip = Join-Path $env:TEMP "ModelGrade-main.zip"
      Invoke-WebRequest "$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing -ErrorAction Stop
      Expand-Archive $zip -DestinationPath $env:TEMP -Force -ErrorAction Stop
      Move-Item (Join-Path $env:TEMP "ModelGrade-main") $dest -ErrorAction Stop
    }
  }
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "install.ps1")
  return
}
Set-Location $here
$quiet = [bool]($env:MODELGRADE_NONINTERACTIVE -or $env:CI)

Write-Host "1/6 파이썬 3.10 이상"
function Find-Python {
  foreach ($c in @(@("py", "-3"), @("python"))) {
    if (Has $c[0]) {
      & $c[0] @($c | Select-Object -Skip 1) -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
      if ($LASTEXITCODE -eq 0) { return , $c }
    }
  }
  return $null
}
$py = Find-Python
if (-not $py) { if (Winget-Install "Python.Python.3.12" "파이썬 3.12") { $py = Find-Python } }
if (-not $py) { Write-Host "  파이썬 3.10 이상이 필요합니다 — https://www.python.org/downloads/ (설치 화면의 'Add python.exe to PATH' 체크) 뒤 다시 실행"; exit 1 }
$pyArgs = @($py | Select-Object -Skip 1)

Write-Host "2/6 가상환경(.venv)과 패키지"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py[0] @pyArgs -m venv .venv; if ($LASTEXITCODE -ne 0) { Write-Host "  가상환경을 못 만들었습니다"; exit 1 } }
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Host "  pip 업그레이드 실패 — 계속 진행" }
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "  패키지 설치 실패 — 위 오류를 확인하세요"; exit 1 }

Write-Host "3/6 .env (키 · 로그인 정보 — 값은 마지막 처음 설정에서 묻습니다)"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "  .env 를 만들었습니다" } else { Write-Host "  .env 있음(그대로 둠)" }

Write-Host "4/6 Google Chrome (답변 수집)"
& .\.venv\Scripts\python.exe -X utf8 -c "from tools.chrome_cdp import chrome_path; chrome_path()" 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host "  있음" } else { Winget-Install "Google.Chrome" "Google Chrome" | Out-Null }

Write-Host "5/6 Node.js · Codex CLI(2차 심판) · Claude CLI(기준 다듬기 루프, 선택)"
if (-not (Has npm) -and -not ($env:MODELGRADE_SKIP_CODEX -and $env:MODELGRADE_SKIP_CLAUDE)) { Winget-Install "OpenJS.NodeJS.LTS" "Node.js" | Out-Null }
foreach ($cli in @(@("codex", "@openai/codex", $env:MODELGRADE_SKIP_CODEX), @("claude", "@anthropic-ai/claude-code", $env:MODELGRADE_SKIP_CLAUDE))) {
  if ($cli[2]) { Write-Host "  $($cli[0]): 건너뜀"; continue }
  if (Has $cli[0]) { Write-Host "  $($cli[0]): 있음"; continue }
  if (-not (Has npm)) { Write-Host "  $($cli[0]): Node.js 가 없어 못 설치 — Node.js 설치 뒤: npm install -g $($cli[1])"; continue }
  npm install -g $cli[1] | Out-Host
  Refresh-Path
  if ($LASTEXITCODE -ne 0) { Write-Host "  $($cli[0]) 설치 실패 — 관리자 PowerShell 에서: npm install -g $($cli[1])" } else { Write-Host "  $($cli[0]): 설치함" }
}

Write-Host "6/6 'modelgrade' 명령 (어느 폴더에서나)"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($userPath -split ";") -contains $here)) {
  [Environment]::SetEnvironmentVariable("Path", (($userPath.TrimEnd(";") + ";" + $here).TrimStart(";")), "User")
  Write-Host "  사용자 PATH 에 $here 를 넣었습니다 — 새 터미널부터 'modelgrade' 로 실행"
} else { Write-Host "  이미 PATH 에 있음" }
$env:Path = "$env:Path;$here"

Write-Host ""
if ($quiet) {
  & .\.venv\Scripts\python.exe -X utf8 tools\doctor.py
  Write-Host ""
  Write-Host "다음: 새 터미널에서  modelgrade setup  (키 · 로그인) → modelgrade start"
} else {
  Write-Host "처음 설정 — 키 · CLI 로그인 · 웹 로그인 · 점검 (나중에 다시: modelgrade setup)"
  & .\modelgrade.cmd setup
  Write-Host ""
  Write-Host "다음: 새 터미널에서  modelgrade start"
}
