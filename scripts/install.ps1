<#
.SYNOPSIS
  audit-kit 를 대상 프로젝트의 가상환경에 설치하고 init 까지 실행한다.

.EXAMPLE
  # 온라인 (PyPI에서 도구 다운로드)
  .\scripts\install.ps1 -Project D:\work\MyProject

  # 오프라인 (build_offline.ps1 로 만든 dist\wheelhouse 사용)
  .\scripts\install.ps1 -Project D:\work\MyProject -Offline

  # 가상환경 경로를 직접 지정
  .\scripts\install.ps1 -Project D:\work\MyProject -Python D:\work\MyProject\env\Scripts\python.exe
#>
param(
    [Parameter(Mandatory = $true)][string]$Project,
    [string]$Python = "",
    [string]$Packages = "",
    [switch]$Offline,
    [switch]$NoInit
)

# 네이티브 명령(pip)의 stderr 알림을 오류로 취급하지 않도록 종료코드로 판단
$ErrorActionPreference = "Continue"
$Kit = Split-Path -Parent $PSScriptRoot
$Project = (Resolve-Path $Project).Path

if (-not $Python) {
    foreach ($cand in @(".venv\Scripts\python.exe", "venv\Scripts\python.exe", "env\Scripts\python.exe")) {
        $p = Join-Path $Project $cand
        if (Test-Path $p) { $Python = $p; break }
    }
}
if (-not $Python) {
    Write-Host "프로젝트 가상환경을 찾지 못했습니다 (.venv / venv / env)." -ForegroundColor Yellow
    Write-Host "mypy·pytest가 프로젝트 의존성을 찾으려면 프로젝트 venv 안에 설치해야 합니다."
    Write-Host "먼저 만드세요:  python -m venv `"$Project\.venv`"  후  pip install -r requirements.txt"
    Write-Host "또는 -Python 으로 인터프리터 경로를 지정하세요."
    exit 1
}
Write-Host "python : $Python"
Write-Host "kit    : $Kit"

if ($Offline) {
    $wh = Join-Path $Kit "dist\wheelhouse"
    if (-not (Test-Path $wh)) { throw "오프라인 묶음이 없습니다: $wh  (먼저 scripts\build_offline.ps1 실행)" }
    & $Python -m pip install --no-index --find-links $wh audit-kit
} else {
    & $Python -m pip install $Kit
}
if ($LASTEXITCODE -ne 0) { throw "설치 실패" }

if (-not $NoInit) {
    $initArgs = @("-m", "audit_kit", "init", "--path", $Project)
    if ($Packages) { $initArgs += @("--packages", $Packages) }
    & $Python @initArgs
    Write-Host ""
    & $Python -m audit_kit doctor --path $Project
}
