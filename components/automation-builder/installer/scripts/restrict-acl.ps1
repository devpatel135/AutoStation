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

if (-not (Test-Path -LiteralPath $Path)) { throw "path not found: $Path" }
$item = Get-Item -LiteralPath $Path -Force
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$grant = if ($item.PSIsContainer) { '{0}:(OI)(CI)F' -f $me.Name } else { '{0}:F' -f $me.Name }

& icacls.exe $item.FullName /inheritance:r /grant:r $grant | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed ($LASTEXITCODE) for $Path" }

# verify: every ACE is explicit (not inherited) and belongs to the current user
$acl = Get-Acl -LiteralPath $item.FullName
$others = @($acl.Access | Where-Object {
        $_.IsInherited -or
        ($_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -ne $me.User.Value)
    })
if ($others.Count -gt 0) { throw "ACL for $Path still has other principals/inherited entries" }
Write-Output "restricted -> $Path"
exit 0
