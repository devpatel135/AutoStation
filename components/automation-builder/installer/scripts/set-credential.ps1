<#
.SYNOPSIS
  Store a secret as a Windows Credential Manager generic credential (CredWriteW) without ever echoing it.

.DESCRIPTION
  Secret sources (first match wins): -SecretFile (wiped + deleted afterwards), -FromStdin, env var AMX_SECRET
  (cleared afterwards). The blob is the secret's UTF-16LE bytes, exactly what wrapper_core.secrets.credman_read
  expects (CredReadW + utf-16-le decode). Persist = LOCAL_MACHINE (survives reboots, stays on this machine).

  -DryRun (or env AUTOMAGIX_CRED_DRYRUN=1) validates inputs and exits 0 without touching Credential Manager;
  the test-suite uses this so it never writes real credentials.
  -Delete removes the credential instead.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [string]$UserName = 'automagix',
    [string]$SecretFile,
    [switch]$FromStdin,
    [switch]$Delete,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

if ($Target -notmatch '^[A-Za-z0-9_./-]{3,128}$') { throw "invalid credential target name" }
$dry = $DryRun -or ($env:AUTOMAGIX_CRED_DRYRUN -eq '1')

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AmxCred {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL {
        public UInt32 Flags; public UInt32 Type; public string TargetName; public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public UInt32 CredentialBlobSize; public IntPtr CredentialBlob; public UInt32 Persist;
        public UInt32 AttributeCount; public IntPtr Attributes; public string TargetAlias; public string UserName;
    }
    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredWrite(ref CREDENTIAL cred, UInt32 flags);
    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredDelete(string target, UInt32 type, UInt32 flags);
}
'@

if ($Delete) {
    if ($dry) { Write-Output "dry-run: would delete $Target"; exit 0 }
    $ok = [AmxCred]::CredDelete($Target, 1, 0)
    Write-Output ($(if ($ok) { "deleted $Target" } else { "not found: $Target" }))
    exit 0
}

$secret = $null
if ($SecretFile) {
    if (-not (Test-Path -LiteralPath $SecretFile)) { throw "secret file not found" }
    $secret = ([System.IO.File]::ReadAllText($SecretFile)).Trim()
    # wipe the plaintext file regardless of dry-run: the caller handed it to us
    try {
        $len = (Get-Item -LiteralPath $SecretFile).Length
        [System.IO.File]::WriteAllBytes($SecretFile, (New-Object byte[] ([int]$len)))
    } catch { }
    Remove-Item -LiteralPath $SecretFile -Force -ErrorAction SilentlyContinue
} elseif ($FromStdin) {
    $secret = ([Console]::In.ReadToEnd()).Trim()
} elseif ($env:AMX_SECRET) {
    $secret = $env:AMX_SECRET.Trim()
    $env:AMX_SECRET = $null
}
if ([string]::IsNullOrEmpty($secret)) { throw "no secret supplied (use -SecretFile, -FromStdin or AMX_SECRET)" }
if ($secret.Length -gt 2500) { throw "secret too long for a credential blob" }

if ($dry) { Write-Output "dry-run: would store $($secret.Length) chars as $Target"; exit 0 }

$bytes = [System.Text.Encoding]::Unicode.GetBytes($secret)   # UTF-16LE, no terminator
$ptr = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
try {
    [System.Runtime.InteropServices.Marshal]::Copy($bytes, 0, $ptr, $bytes.Length)
    $cred = New-Object AmxCred+CREDENTIAL
    $cred.Type = 1                    # CRED_TYPE_GENERIC
    $cred.TargetName = $Target
    $cred.UserName = $UserName
    $cred.CredentialBlobSize = [uint32]$bytes.Length
    $cred.CredentialBlob = $ptr
    $cred.Persist = 2                 # CRED_PERSIST_LOCAL_MACHINE
    if (-not [AmxCred]::CredWrite([ref]$cred, 0)) {
        throw ("CredWriteW failed (Win32 error {0})" -f [System.Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
} finally {
    [Array]::Clear($bytes, 0, $bytes.Length)
    [System.Runtime.InteropServices.Marshal]::FreeHGlobal($ptr)
}
Write-Output "stored $Target"
exit 0
