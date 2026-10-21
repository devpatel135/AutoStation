<#
.SYNOPSIS
  Write config\platform.json (SPEC section 2) with real JSON escaping.

.DESCRIPTION
  Keys read by wrapper_core.paths.platform_config: ring, registry_url, log_transport, log_remote, llm_endpoint,
  llm_model. Existing keys not managed here are preserved so a re-install never drops operator additions.
  log_transport is 'git-ssh' when -LogRemote looks like an ssh remote, otherwise 'dir'.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][ValidateSet('dev', 'pilot', 'broad')][string]$Ring,
    [Parameter(Mandatory = $true)][string]$RegistryUrl,
    [string]$LogRemote = '',
    [string]$LlmEndpoint = '',
    [string]$LlmModel = ''
)
$ErrorActionPreference = 'Stop'

$cfg = [ordered]@{}
if (Test-Path -LiteralPath $Out) {
    try {
        $old = Get-Content -LiteralPath $Out -Raw | ConvertFrom-Json
        foreach ($p in $old.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    } catch { }   # corrupt old file: start fresh
}
$cfg['ring'] = $Ring
$cfg['registry_url'] = $RegistryUrl
if ($LogRemote -eq 'local') {
    # standalone install: no logging server. Run logs land next to the install and can be collected/ingested later.
    $LogRemote = Join-Path (Split-Path -Parent (Split-Path -Parent $Out)) 'local-logs'
}
$cfg['log_remote'] = $LogRemote
$cfg['log_transport'] = $(if ($LogRemote -match '^(git@|ssh://)') { 'git-ssh' } else { 'dir' })
if ($LlmEndpoint) { $cfg['llm_endpoint'] = $LlmEndpoint }
if ($LlmModel) { $cfg['llm_model'] = $LlmModel }

$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
[System.IO.File]::WriteAllText($Out, ((ConvertTo-Json -InputObject $cfg -Depth 5) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
Write-Output "wrote $Out"
exit 0
