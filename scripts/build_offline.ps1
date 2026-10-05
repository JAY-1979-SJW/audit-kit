<#
.SYNOPSIS
  인터넷이 안 되는 PC용 오프라인 설치 묶음(dist\wheelhouse) 생성.

.DESCRIPTION
  audit-kit + 모든 도구(ruff, mypy, import-linter, vulture, radon, bandit, pytest, pytest-cov)의 wheel을 모은다.
  주의: ruff·mypy 등은 OS/파이썬 버전별 바이너리 wheel이다.
        대상 PC와 같은 OS·같은 파이썬 버전(예: Windows x64 / Python 3.11)으로 실행해야 한다.

.EXAMPLE
  .\scripts\build_offline.ps1
  .\scripts\build_offline.ps1 -Python C:\Python312\python.exe
#>
param(
    [string]$Python = "python",
    [switch]$WithGraph
)

# 네이티브 명령(pip)의 stderr 알림을 오류로 취급하지 않도록 종료코드로 판단
$ErrorActionPreference = "Continue"
$Kit = Split-Path -Parent $PSScriptRoot
$wh = Join-Path $Kit "dist\wheelhouse"
New-Item -ItemType Directory -Force $wh | Out-Null

$ver = & $Python -c "import sys, platform; print(f'Python {sys.version.split()[0]} / {platform.system()} {platform.machine()}')"
Write-Host "빌드 환경: $ver  → 대상 PC도 같아야 함"

$target = $Kit
if ($WithGraph) { $target = "$Kit[graph]" }
& $Python -m pip wheel $target -w $wh
if ($LASTEXITCODE -ne 0) { throw "wheel 생성 실패" }

"$ver" | Out-File -Encoding utf8 (Join-Path $wh "BUILT_WITH.txt")
Write-Host ""
Write-Host "완료: $wh" -ForegroundColor Green
Write-Host "audit-kit 폴더 전체(dist 포함)를 대상 PC로 복사한 뒤:"
Write-Host "  .\scripts\install.ps1 -Project <프로젝트경로> -Offline"
