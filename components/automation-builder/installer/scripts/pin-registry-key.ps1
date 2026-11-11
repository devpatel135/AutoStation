<#
.SYNOPSIS
  Pin the operator's registry signing public key into platform.json on a new or existing install.

.DESCRIPTION
  Calls `python -m wrapper_core trust --pin <key>` which fetches the live registry manifest, verifies it
  against the supplied Ed25519 public key, and writes the key to platform.json only if verification
  succeeds. After pinning, the machine refuses any unsigned or mis-signed manifest — a compromised
  registry host cannot push arbitrary payloads.

  Exit 0: key pinned and verified.
  Exit 1: key absent, malformed, or did not verify against the live registry (nothing written).
  Exit 2: --AllowUnsigned used; key pinning skipped (development / pilot use only).

  This script must be called after configure-python.ps1 (so wrapper_core is importable) and after
  platform.json exists with a registry_url set by the main installer.

.PARAMETER Python
  Path to the Python executable (or bare name). Passed by the NSIS installer from the configure-python step.

.PARAMETER AutomagixHome
  AUTOMAGIX_HOME directory. platform.json must already exist here with registry_url set.

.PARAMETER PubKey
  The operator's Ed25519 public key as a 64-hex string. Required unless -AllowUnsigned is given.

.PARAMETER AllowUnsigned
  Skip key pinning. Manifests will be accepted unsigned. USE ONLY FOR DEVELOPMENT / PILOT ENVIRONMENTS.
  The NSIS installer should expose this as an advanced / expert-mode option, off by default.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$AutomagixHome,
    [string]$PubKey,
    [switch]$AllowUnsigned
)
$ErrorActionPreference = 'Stop'

if ($AllowUnsigned) {
    Write-Warning "pin-registry-key: --AllowUnsigned set. Manifests will be accepted UNSIGNED. Do not use in production."
    exit 2
}

if ([string]::IsNullOrWhiteSpace($PubKey)) {
    Write-Error "pin-registry-key: -PubKey is required. Provide the operator Ed25519 public key (64 hex characters) published in your operator setup guide."
    exit 1
}

# Validate hex format before invoking Python (fail fast with a clear message)
if ($PubKey -notmatch '^[0-9a-fA-F]{64}$') {
    Write-Error "pin-registry-key: -PubKey must be exactly 64 hexadecimal characters (got: '$($PubKey.Substring(0,[Math]::Min(16,$PubKey.Length)))...')"
    exit 1
}

$Python = $Python.Trim('"')
if (-not (Test-Path -LiteralPath $Python -ErrorAction SilentlyContinue)) {
    $cmd = Get-Command $Python -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { Write-Error "pin-registry-key: python not found: $Python"; exit 1 }
    $Python = $cmd.Source
}

$env:AUTOMAGIX_HOME = $AutomagixHome

Write-Output "pin-registry-key: fetching live registry manifest and verifying key $($PubKey.Substring(0,8))... against it"

# wrapper_core trust --pin verifies the key against the live registry before writing platform.json.
# It exits 0 on success, 1 on any failure (bad key, manifest unreachable, signature mismatch).
& $Python -m wrapper_core trust --pin $PubKey
if ($LASTEXITCODE -ne 0) {
    Write-Error "pin-registry-key: trust --pin failed (exit $LASTEXITCODE). The key was NOT stored. Check that the registry is reachable and the key is correct."
    exit 1
}

Write-Output "pin-registry-key: registry key pinned. This machine will now refuse unsigned or mis-signed manifests."
exit 0
