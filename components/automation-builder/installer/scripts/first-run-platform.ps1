<#
.SYNOPSIS
  Platform first-run: register the AUTOSTATION heartbeat task and startup maintenance.

.DESCRIPTION
  Called once at the end of the platform installer. Registers:
    - A Task Scheduler heartbeat (\AUTOMAGIX\heartbeat) that runs every 4 hours and
      calls `python -m wrapper_core maintain` to drive self-heal for all installed automations.
    - A Startup folder .lnk that launches wrapper_core.tray on logon.
  Both are registered for the current user only (no admin). Safe to re-run: existing
  registrations are replaced, not duplicated.
  Exits 0 on success, 1 on failure; prints a single line of JSON:
    {"ok":bool,"heartbeat":bool,"tray":bool,"detail":str}
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$AutomagixHome
)
$ErrorActionPreference = 'Stop'
$results = [ordered]@{ ok = $false; heartbeat = $false; tray = $false; detail = '' }

function Report([string]$detail) {
    $results['ok']     = $results['heartbeat'] -and $results['tray']
    $results['detail'] = $detail
    Write-Output (ConvertTo-Json -Compress $results)
    exit ([int](-not $results['ok']))
}

# -- Task Scheduler heartbeat (every 4 hours) ----------------------------------------
try {
    $action  = New-ScheduledTaskAction -Execute $Python -Argument "-m wrapper_core maintain" `
                   -WorkingDirectory $AutomagixHome
    $trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Hours 4) -Once `
                   -At (Get-Date).Date
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
                    -StartWhenAvailable -RunOnlyIfNetworkAvailable:$false
    Register-ScheduledTask -TaskPath '\AUTOMAGIX\' -TaskName 'heartbeat' `
        -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited `
        -Force | Out-Null
    $results['heartbeat'] = $true
} catch {
    Report "heartbeat registration failed: $($_.Exception.Message)"
}

# -- Startup folder shortcut (tray app) -----------------------------------------------
try {
    $startupDir = [System.Environment]::GetFolderPath('Startup')
    $lnkPath    = Join-Path $startupDir 'AUTOSTATION Tray.lnk'
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($lnkPath)
    $lnk.TargetPath       = $Python
    $lnk.Arguments        = '-m wrapper_core tray'
    $lnk.WorkingDirectory = $AutomagixHome
    $lnk.Description      = 'AUTOSTATION platform tray'
    $lnk.Save()
    $results['tray'] = $true
} catch {
    Report "startup shortcut creation failed: $($_.Exception.Message)"
}

# -- Launch tray now (detached) --------------------------------------------------------
try {
    Start-Process -FilePath $Python -ArgumentList '-m wrapper_core tray' `
        -WorkingDirectory $AutomagixHome -WindowStyle Hidden
} catch { }   # advisory: tray not launching is not a fatal install error

Report 'platform first-run complete'
