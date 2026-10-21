<#
.SYNOPSIS
  Export the user's client certificate (with private key) from the CurrentUser store to a temporary PFX with a random passphrase,
  for fw.http.client({ pfxPath, passphrase }) headless cert-auth. PowerShell 5.1, no admin, no extra modules beyond PKI (built in).

.DESCRIPTION
  The private key must be marked exportable by the issuing PKI profile. Output is ONE JSON line on stdout:
    {"pfxPath":"...","passphrase":"...","thumbprint":"...","subject":"...","notAfter":"..."}
  The PFX is created with an owner-only ACL. Delete it when done:   export-cert.ps1 -Cleanup <pfxPath>
  Nothing is written to the certificate store and nothing leaves the machine.

.EXAMPLE
  $c = powershell -NoProfile -File export-cert.ps1 -SubjectLike '*CN=svc-automation*' | ConvertFrom-Json
#>
[CmdletBinding()]
param(
  [string]$Thumbprint,
  [string]$SubjectLike,
  [string]$OutDir = $env:TEMP,
  [string]$Store = 'Cert:\CurrentUser\My',
  [string]$Cleanup
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 can inherit a PowerShell 7 PSModulePath (CI runners, pwsh-launched shells) and then fail to
# load Microsoft.PowerShell.Security (Get-Acl / Set-AuthenticodeSignature). Pin the built-in module path.
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    $env:PSModulePath = (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules') + ';' + (Join-Path $env:windir 'System32\WindowsPowerShell\v1.0\Modules')
}


if ($Cleanup) {
  if (Test-Path -LiteralPath $Cleanup) {
    # overwrite then delete: limits casual recovery of the key material
    $len = (Get-Item -LiteralPath $Cleanup).Length
    [System.IO.File]::WriteAllBytes($Cleanup, (New-Object byte[] $len))
    Remove-Item -LiteralPath $Cleanup -Force
  }
  Write-Output '{"cleaned":true}'
  exit 0
}

if (-not $Thumbprint -and -not $SubjectLike) { throw 'Specify -Thumbprint or -SubjectLike (e.g. "*CN=svc-automation*").' }

$now = Get-Date
$candidates = @(Get-ChildItem -Path $Store | Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt $now -and $_.NotBefore -lt $now })
if ($Thumbprint) {
  $want = ($Thumbprint -replace '\s', '').ToUpperInvariant()
  $candidates = @($candidates | Where-Object { $_.Thumbprint -eq $want })
}
if ($SubjectLike) {
  $candidates = @($candidates | Where-Object { $_.Subject -like $SubjectLike })
}
if ($candidates.Count -eq 0) { throw "No valid certificate with a private key matched in $Store." }
$cert = $candidates | Sort-Object NotAfter -Descending | Select-Object -First 1

# 32 random bytes -> base64 passphrase
$rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$rng.Dispose()
$passphrase = [Convert]::ToBase64String($bytes)
$secure = ConvertTo-SecureString -String $passphrase -AsPlainText -Force

if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$file = Join-Path $OutDir ('amx-' + [Guid]::NewGuid().ToString('N') + '.pfx')

try {
  Export-PfxCertificate -Cert $cert -FilePath $file -Password $secure | Out-Null
} catch {
  throw "Export failed (is the private key marked exportable?): $($_.Exception.Message)"
}

# owner-only ACL
$acl = Get-Acl -LiteralPath $file
$acl.SetAccessRuleProtection($true, $false)
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $file -AclObject $acl

$out = [ordered]@{
  pfxPath    = $file
  passphrase = $passphrase
  thumbprint = $cert.Thumbprint
  subject    = $cert.Subject
  notAfter   = $cert.NotAfter.ToString('o')
}
Write-Output ($out | ConvertTo-Json -Compress)
