<#
.SYNOPSIS
  Check that the AUTOSTATION platform is installed and meets the minimum version.

.DESCRIPTION
  Reads $InstallDir\platform-version.txt and compares it against MinVersion using
  System.Version. Exits 0 (ok) or 1 (absent/stale). Output is one JSON line:
    {"ok":bool,"installed":str|null,"required":str,"detail":str}
  No secrets are read here; this is safe to run before any secrets are staged.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$MinVersion
)
$ErrorActionPreference = 'Stop'

function Out([bool]$ok, [string]$installed, [string]$detail) {
    Write-Output (ConvertTo-Json -Compress ([ordered]@{
        ok        = $ok
        installed = $installed
        required  = $MinVersion
        detail    = $detail
    }))
    exit ([int](-not $ok))
}

$vfile = Join-Path $InstallDir 'platform-version.txt'
if (-not (Test-Path -LiteralPath $vfile)) {
    Out $false $null "platform-version.txt not found at $InstallDir — run autostation-platform-setup.exe first"
}

$raw = (Get-Content -LiteralPath $vfile -Raw).Trim()
try {
    $installed = [System.Version]::Parse($raw)
    $required  = [System.Version]::Parse($MinVersion)
} catch {
    Out $false $raw "could not parse version from platform-version.txt: $($_.Exception.Message)"
}

if ($installed -ge $required) {
    Out $true $raw "platform $raw satisfies requirement >= $MinVersion"
} else {
    Out $false $raw "platform $raw is older than required >= $MinVersion — re-run autostation-platform-setup.exe"
}
