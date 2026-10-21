<#
.SYNOPSIS
  Restrict a file or directory to the current user only (no inherited ACEs, no other principals).

.DESCRIPTION
  Per-user install => no admin needed. Removes inheritance, then grants the current user Full control.
  Directories get (OI)(CI) so children inherit the owner-only rule. Exits non-zero if the result cannot be verified.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Path
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 can inherit a PowerShell 7 PSModulePath (CI runners, pwsh-launched shells) and then fail to
# load Microsoft.PowerShell.Security (Get-Acl / Set-AuthenticodeSignature). Pin the built-in module path.
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    $env:PSModulePath = (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules') + ';' + (Join-Path $env:windir 'System32\WindowsPowerShell\v1.0\Modules')
}


if (-not (Test-Path -LiteralPath $Path)) { throw "path not found: $Path" }
$item = Get-Item -LiteralPath $Path -Force
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$grant = if ($item.PSIsContainer) { '{0}:(OI)(CI)F' -f $me.Name } else { '{0}:F' -f $me.Name }

& icacls.exe $item.FullName /inheritance:r /grant:r $grant | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed ($LASTEXITCODE) for $Path" }

# Some hosts (hardened CI images, EDR-managed VDI) put explicit SYSTEM / Administrators / OWNER RIGHTS entries on new files.
# "Owner-only" means exactly that: strip every other explicit principal (admins can still take ownership if they must).
$mine = $me.User.Value
foreach ($ace in @((Get-Acl -LiteralPath $item.FullName).Access)) {
    $sid = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($sid -ne $mine) {
        & icacls.exe $item.FullName /remove:g ("*" + $sid) | Out-Null
    }
}

# verify: every ACE is explicit (not inherited) and belongs to the current user
$acl = Get-Acl -LiteralPath $item.FullName
$others = @($acl.Access | Where-Object {
        $_.IsInherited -or
        ($_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -ne $me.User.Value)
    })
if ($others.Count -gt 0) {
    $detail = ($others | ForEach-Object { "{0} inherited={1} rights={2}" -f $_.IdentityReference, $_.IsInherited, $_.FileSystemRights }) -join "; "
    throw "ACL for $Path still has other principals/inherited entries: $detail (me=$($me.Name))"
}
Write-Output "restricted -> $Path"
exit 0
