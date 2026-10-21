<#
.SYNOPSIS
  Kick off the first run of a freshly installed automation without blocking (or flashing a console in) the installer.

.DESCRIPTION
  Starts `python -m wrapper_core run <automation>` as a hidden, detached process with AUTOMAGIX_HOME set.
  Task Scheduler takes over from the next scheduled time; this is only the "confirm it works" run from the
  assessment's install flow (step 5). Exit code is 0 if the process was started.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$AutomagixHome,
    [Parameter(Mandatory = $true)][string]$Automation
)
$ErrorActionPreference = 'Stop'
$Python = $Python.Trim('"')
if (-not (Test-Path -LiteralPath $Python)) {
    # system-Python installs pass a bare name such as python.exe
    $cmd = Get-Command $Python -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { throw "python not found: $Python" }
    $Python = $cmd.Source
}
if ($Automation -notmatch '^[A-Za-z0-9._-]+$') { throw "invalid automation name" }

$env:AUTOMAGIX_HOME = $AutomagixHome
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Python
$psi.Arguments = "-m wrapper_core run $Automation"
$psi.WorkingDirectory = $AutomagixHome
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$p = [System.Diagnostics.Process]::Start($psi)
Write-Output "first run started (pid $($p.Id))"
exit 0
