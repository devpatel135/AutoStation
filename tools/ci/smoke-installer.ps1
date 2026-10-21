<#
 .SYNOPSIS
  Smoke-test a built AutoStation installer on a Windows machine, exactly as a user would use it.

 .DESCRIPTION
  -Phase install   silent per-user install (/S /NORUN), checks the bundled runtime, config and scheduled task
  -Phase run       starts the automation's local demo portal, runs the installed automation once through wrapper_core
                   (transport forced to a local folder: a smoke run never reaches the live logs) and checks the result
  -Phase uninstall runs the uninstaller and checks that the task and payload are gone

  Used by canary.yml. (installer-dev.yml carries its own copy of the same checks inline; keep them in step.)
  Every failure prints one ::error line and exits 1; success exits 0 (an explicit exit also stops a native command's exit
  code from leaking out of the step).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('install', 'run', 'uninstall')][string]$Phase,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')][string]$Automation,
    [string]$Installer = '',
    [int]$TimeoutS = 300
)
$ErrorActionPreference = 'Stop'
$homeDir = Join-Path $env:LOCALAPPDATA 'AUTOMAGIX'

function Fail([string]$msg) {
    $m = ($msg -replace '[\r\n]+', ' | ')
    Write-Host "::error title=smoke $Phase failed::$m"
    if (Test-Path $homeDir) {
        Get-ChildItem $homeDir -Recurse -Depth 2 -ErrorAction SilentlyContinue | Select-Object -First 60 -ExpandProperty FullName | Out-Host
        Get-Content (Join-Path $homeDir 'logs\wrapper.log') -ErrorAction SilentlyContinue | Select-Object -Last 40 | Out-Host
    }
    exit 1
}

try {
    switch ($Phase) {
        'install' {
            if (-not $Installer -or -not (Test-Path -LiteralPath $Installer)) { Fail "installer not found: $Installer" }
            # /NORUN: the smoke run below must not push a CI run anywhere
            $p = Start-Process -FilePath $Installer -ArgumentList '/S', '/NORUN' -PassThru
            if (-not $p.WaitForExit($TimeoutS * 1000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Fail "installer did not finish in ${TimeoutS}s" }
            if ($p.ExitCode -ne 0) { Fail "installer exit code $($p.ExitCode)" }
            foreach ($f in @('python\python.exe', 'wrapper-core\wrapper_core\__init__.py', 'execution-framework\node\node.exe',
                             'execution-framework\node_modules\playwright-core\package.json', 'execution-framework\VERSION',
                             "automations\$Automation\automation.yaml", 'state\installed.json', 'config\platform.json')) {
                if (-not (Test-Path (Join-Path $homeDir $f))) { Fail "missing after install: $f" }
            }
            Get-Content (Join-Path $homeDir 'config\platform.json') | Out-Host
            schtasks /Query /TN "AUTOMAGIX\$Automation-daily" | Out-Host
            if ($LASTEXITCODE -ne 0) { Fail 'scheduled task was not registered' }
            Write-Host 'install OK'
        }
        'run' {
            $payload = Join-Path $homeDir "automations\$Automation"
            $node = Join-Path $homeDir 'execution-framework\node\node.exe'
            $server = Join-Path $payload 'demo\server.js'
            # a canary that cannot exercise the automation must be RED, never a silent green
            if (-not (Test-Path $server)) { Fail "no local demo portal (demo\server.js) in the installed $Automation - the run phase cannot prove anything" }
            $srv = Start-Process -FilePath $node -ArgumentList 'demo\server.js' -WorkingDirectory $payload -PassThru -WindowStyle Hidden
            try {
                $up = $false
                foreach ($i in 1..30) {
                    try { Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8099/portal.html' -TimeoutSec 2 | Out-Null; $up = $true; break } catch { Start-Sleep -Seconds 1 }
                }
                if (-not $up) { Fail 'demo portal did not start' }
                $logs = Join-Path $homeDir 'smoke-logs'
                $env:AUTOMAGIX_HOME = $homeDir
                $env:PYTHONPATH = Join-Path $homeDir 'wrapper-core'
                $env:AUTOMAGIX_LOG_TRANSPORT = 'dir'; $env:AUTOMAGIX_LOG_REMOTE = $logs
                $env:AUTOMAGIX_NO_TOAST = '1'
                $env:AUTOMAGIX_NO_REMEDIATION = '1'      # the smoke must report the real result, not a remediated retry
                $env:AUTOMATION_PARAM_PORTAL_URL = 'http://127.0.0.1:8099/portal.html'
                $env:AUTOMATION_PARAM_PICKER_HOST = 'localhost:8099'
                $env:AUTOMATION_PARAM_ALLOWED_HOSTS = '127.0.0.1:8099,localhost:8099'
                $env:AMX_ALLOW_TEST_HOSTS = '1'          # local demo only
                $env:AUTOMATION_PARAM_HEADLESS = 'true'
                $env:AUTOMATION_PARAM_MODE = 'playwright'   # the runner may have DevTools policies of its own: the demo must use the real browser path
                $env:AUTOMATION_PARAM_TIMEOUT_S = '45'
                & (Join-Path $homeDir 'python\python.exe') -m wrapper_core run $Automation --slot daily
                $code = $LASTEXITCODE
            } finally {
                Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
            }
            $run = Get-ChildItem -Recurse -Path $logs -Filter *.json -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match 'run[\\/]' } | Select-Object -First 1
            if (-not $run) { Fail "no run log was written (exit code $code)" }
            $rec = Get-Content $run.FullName -Raw | ConvertFrom-Json
            Write-Host "run: status=$($rec.status) synopsis=$($rec.synopsis)"
            if ($rec.status -ne 'success') { Fail "run status was $($rec.status): $($rec.error | ConvertTo-Json -Compress)" }
            if ($rec.result.state -ne 'account_selected') { Fail "expected the account tile to be clicked, got state '$($rec.result.state)'" }
            Write-Host 'run OK: the account tile was clicked and the run stopped'
        }
        'uninstall' {
            $un = Get-ChildItem $homeDir -Filter 'uninstall-*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $un) { Fail 'uninstaller not found' }
            $p = Start-Process -FilePath $un.FullName -ArgumentList '/S' -PassThru
            if (-not $p.WaitForExit($TimeoutS * 1000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Fail "uninstaller did not finish in ${TimeoutS}s" }
            if ($p.ExitCode -ne 0) { Fail "uninstaller exit code $($p.ExitCode)" }
            Start-Sleep -Seconds 3
            schtasks /Query /TN "AUTOMAGIX\$Automation-daily" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { Fail 'scheduled task still registered after uninstall' }
            if (Test-Path (Join-Path $homeDir "automations\$Automation")) { Fail 'payload still present after uninstall' }
            Write-Host 'uninstall OK'
        }
    }
} catch {
    Fail $_.Exception.Message
}
exit 0
