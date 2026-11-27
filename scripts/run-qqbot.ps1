$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = 'D:\CWFLY\CWFLY-WEB\backend-py\.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw not found: $pythonw" }

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$pidFile = Join-Path $logDir 'qqbot.pid'

if (Test-Path $pidFile) {
    $oldPid = 0
    [void][int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$oldPid)
    if ($oldPid -gt 0 -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) { return }
}

$existing = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*qq_official_bot.py*' }
if ($existing) {
    $existing | Select-Object -First 1 -ExpandProperty ProcessId | Set-Content -Path $pidFile -Encoding utf8
    return
}

$proc = Start-Process -FilePath $pythonw `
    -ArgumentList @('-u', 'py\qq_official_bot.py') `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'qqbot.out.log') `
    -RedirectStandardError (Join-Path $logDir 'qqbot.err.log') `
    -PassThru
Set-Content -Path $pidFile -Value $proc.Id -Encoding utf8
