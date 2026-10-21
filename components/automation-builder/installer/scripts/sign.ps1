<#
.SYNOPSIS
  Authenticode-sign a file (Windows runner / developer machine). Linux CI uses osslsigncode instead.

.DESCRIPTION
  Certificate source (exactly one):
    -Thumbprint    a code-signing certificate already in Cert:\CurrentUser\My (or LocalMachine\My) - internal PKI
    -PfxFile       a .pfx file (CI file variable) + -PfxPasswordEnv NAME of the env var holding its password
  The password is read from the environment, never from argv, and never printed.
  -TimestampUrl adds an RFC3161/Authenticode timestamp (strongly recommended: signatures outlive the cert).
  -WhatIf validates inputs only.
  Exits non-zero unless the file ends up with a signature (Status Valid, or UnknownError for an untrusted private
  root in dev) - chain trust is verified by the consumer, we verify that a signature block exists.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$File,
    [string]$Thumbprint,
    [string]$PfxFile,
    [string]$PfxPasswordEnv,
    [string]$TimestampUrl,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $File)) { throw "file to sign not found: $File" }
if ([bool]$Thumbprint -eq [bool]$PfxFile) { throw "specify exactly one of -Thumbprint or -PfxFile" }

if ($Thumbprint) {
    $tp = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
        Where-Object { $_.Thumbprint -eq $tp } | Select-Object -First 1
    if (-not $cert) { throw "certificate with thumbprint $tp not found in CurrentUser\My / LocalMachine\My" }
} else {
    if (-not (Test-Path -LiteralPath $PfxFile)) { throw "pfx file not found" }
    $pw = $null
    if ($PfxPasswordEnv) { $pw = [Environment]::GetEnvironmentVariable($PfxPasswordEnv) }
    $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 ((Resolve-Path -LiteralPath $PfxFile).Path), $pw, $flags
}
if (-not $cert.HasPrivateKey) { throw "certificate has no private key" }
$eku = $cert.Extensions | Where-Object { $_ -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension] }
if ($eku -and -not ($eku.EnhancedKeyUsages | Where-Object { $_.Value -eq '1.3.6.1.5.5.7.3.3' })) {
    throw "certificate lacks the Code Signing EKU"
}

if ($WhatIf) { Write-Output "ok: would sign $File with $($cert.Subject)"; exit 0 }

$args = @{ FilePath = $File; Certificate = $cert; HashAlgorithm = 'SHA256' }
if ($TimestampUrl) { $args['TimestampServer'] = $TimestampUrl }
$sig = Set-AuthenticodeSignature @args
if (-not $sig.SignerCertificate) { throw "signing failed: $($sig.StatusMessage)" }
Write-Output ("signed {0} by {1} (status {2})" -f $File, $sig.SignerCertificate.Subject, $sig.Status)
exit 0
