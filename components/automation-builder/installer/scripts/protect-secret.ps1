<#
.SYNOPSIS
  DPAPI (CurrentUser) protect / unprotect a file. Byte-compatible with wrapper_core.secrets.

.DESCRIPTION
  Uses [System.Security.Cryptography.ProtectedData] (the assessment's "Protect-Data" is not a built-in cmdlet)
  with the fixed entropy bytes 'AUTOMAGIX-v1', exactly like wrapper_core.secrets.dpapi_protect.

  Protect (default): reads -InFile, writes the encrypted blob to -OutFile, restricts the blob's ACL to the current
  user and, with -DeleteInput, overwrites the plaintext with zeros and deletes it.
  Unprotect: -Unprotect reads an encrypted -InFile and writes the plaintext to -OutFile (used by tests / support
  only; production code decrypts in memory inside wrapper_core).

  The secret value is never written to the console or to any log.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InFile,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [switch]$DeleteInput,
    [switch]$Unprotect
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

$entropy = [System.Text.Encoding]::UTF8.GetBytes('AUTOMAGIX-v1')
$scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser

function Remove-Securely([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        $len = (Get-Item -LiteralPath $Path).Length
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write)
        try {
            $zeros = New-Object byte[] ([int][Math]::Max($len, 1))
            $fs.Write($zeros, 0, [int]$len)
            $fs.Flush($true)
        } finally { $fs.Dispose() }
    } catch { }   # best effort; deletion below is what matters
    Remove-Item -LiteralPath $Path -Force
}

if (-not (Test-Path -LiteralPath $InFile)) { throw "input file not found: $InFile" }
$data = [System.IO.File]::ReadAllBytes($InFile)
if ($data.Length -eq 0) { throw "input file is empty: $InFile" }

$outDir = Split-Path -Parent $OutFile
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

if ($Unprotect) {
    $plain = [System.Security.Cryptography.ProtectedData]::Unprotect($data, $entropy, $scope)
    [System.IO.File]::WriteAllBytes($OutFile, $plain)
    Write-Output "unprotected -> $OutFile"
} else {
    $blob = [System.Security.Cryptography.ProtectedData]::Protect($data, $entropy, $scope)
    [System.IO.File]::WriteAllBytes($OutFile, $blob)
    & (Join-Path $PSScriptRoot 'restrict-acl.ps1') -Path $OutFile
    Write-Output "protected -> $OutFile"
}
[Array]::Clear($data, 0, $data.Length)

if ($DeleteInput) { Remove-Securely $InFile }
exit 0
