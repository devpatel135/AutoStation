<#
.SYNOPSIS
  Prerequisite checks run by the installer before anything is written.

.DESCRIPTION
  Prints ONE line of JSON: {"ok":bool,"checks":[{"name","ok","detail"}...],"git":path|null,"ssh":path|null,"python":path|null}
  and exits 0 (all required checks passed) or 1. Execution policy is irrelevant (the installer launches scripts with
  -ExecutionPolicy Bypass for this process only).

  Required: Windows 10+, writable install dir, free disk space >= MinFreeMB, git on PATH (unless -SkipGit),
  ssh.exe on PATH (unless -SkipGit; git push over SSH needs it).
  Informational: a system Python (used only if the installer was built without an embedded interpreter).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [int]$MinFreeMB = 500,
    [switch]$SkipGit
)
$ErrorActionPreference = 'Stop'
$checks = New-Object System.Collections.ArrayList
function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    [void]$checks.Add([ordered]@{ name = $Name; ok = $Ok; detail = $Detail })
}

# Windows 10+
$ver = [Environment]::OSVersion.Version
Add-Check 'windows' ($ver.Major -ge 10) ("{0}.{1}.{2}" -f $ver.Major, $ver.Minor, $ver.Build)

# install dir writable (create it if missing, then probe)
try {
    if (-not (Test-Path -LiteralPath $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null }
    $probe = Join-Path $InstallDir ('.write-test-' + [Guid]::NewGuid().ToString('N'))
    [System.IO.File]::WriteAllText($probe, 'x'); Remove-Item -LiteralPath $probe -Force
    Add-Check 'install_dir_writable' $true $InstallDir
} catch { Add-Check 'install_dir_writable' $false $_.Exception.Message }

# free space
try {
    $root = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $InstallDir).Path)
    $drive = New-Object System.IO.DriveInfo ($root)
    $freeMB = [int][Math]::Floor($drive.AvailableFreeSpace / 1MB)
    Add-Check 'disk_space' ($freeMB -ge $MinFreeMB) ("{0} MB free, need {1}" -f $freeMB, $MinFreeMB)
} catch { Add-Check 'disk_space' $false $_.Exception.Message }

function Find-Exe([string]$name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    return $null
}
$git = Find-Exe 'git.exe'
$ssh = Find-Exe 'ssh.exe'
if (-not $SkipGit) {
    Add-Check 'git' ($null -ne $git) $(if ($git) { $git } else { 'git.exe not on PATH (needed for log shipping)' })
    Add-Check 'ssh' ($null -ne $ssh) $(if ($ssh) { $ssh } else { 'ssh.exe not on PATH (Windows optional feature "OpenSSH Client" or Git for Windows)' })
}
$py = Find-Exe 'python.exe'
if ($py -and $py -like '*\WindowsApps\python.exe') { $py = $null }   # Store alias stub, not a real interpreter

$ok = -not (@($checks | Where-Object { -not $_.ok }).Count -gt 0)
$result = [ordered]@{ ok = $ok; checks = $checks; git = $git; ssh = $ssh; python = $py }
Write-Output (ConvertTo-Json -InputObject $result -Compress -Depth 5)
if ($ok) { exit 0 } else { exit 1 }
