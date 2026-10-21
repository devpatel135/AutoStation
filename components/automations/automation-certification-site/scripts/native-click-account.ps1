<#
 Native (no DevTools protocol) fallback for the certification-site automation.

 Managed Edge installs often set the policy RemoteDebuggingAllowed=0 ("DevTools remote debugging is disallowed by the system admin"),
 which makes Playwright unable to drive Edge. This script respects that policy: it opens the page in the user's normal Edge
 (their profile, their SSO) and uses Windows UI Automation - the same accessibility API screen readers use - to find the
 Microsoft account tile for -Account and press it. It never types anything, then stops.
 Prints ONE json line: {"state": "...", "clicked": bool, "host": "..."} ; exit 0 unless the script itself failed.
#>
param(
  [Parameter(Mandatory)][string]$Url,
  [Parameter(Mandatory)][ValidatePattern('^[^@\s]+@[^@\s]+\.[^@\s]+$')][string]$Account,
  [int]$TimeoutS = 60,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$edge = @("$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { Write-Output '{"state":"error","clicked":false,"detail":"msedge.exe not found"}'; exit 0 }
function Edge-Windows {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, 'Chrome_WidgetWin_1')
  @($root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond) | Where-Object { $_.Current.Name -match 'Edge' })
}
# only ever touch the window WE open: remember the existing ones first
$before = @(Edge-Windows | ForEach-Object { $_.Current.NativeWindowHandle })
Start-Process -FilePath $edge -ArgumentList @('--new-window', $Url) | Out-Null

function Find-Tile($win, $account) {
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsInvokePatternAvailableProperty, $true)
  foreach ($el in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
    $n = $el.Current.Name
    if ($n -and $n -like "*$account*") { return $el }
  }
  # a tile is often a non-invokable container whose child text carries the e-mail: take the nearest invokable ancestor
  $txt = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $account)
  $hit = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $txt)
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  while ($hit -ne $null) {
    if ($hit.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern) -ne $null) { return $hit }
    try { $hit = $walker.GetParent($hit) } catch { return $null }
  }
  return $null
}

$deadline = (Get-Date).AddSeconds($TimeoutS)
$state = 'other'; $clicked = $false; $title = ''
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 1500
  foreach ($w in (Edge-Windows | Where-Object { $before -notcontains $_.Current.NativeWindowHandle })) {
    $title = $w.Current.Name
    if ($title -match 'Sign in to your account|Pick an account|Sign in') {
      $tile = $null
      try { $tile = Find-Tile $w $Account } catch { $tile = $null }
      if ($tile) {
        $state = 'account-picker'
        if (-not $DryRun) {
          try { $tile.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $clicked = $true } catch {
            $r = $tile.Current.BoundingRectangle; if ($r.Width -gt 0) { $null = $r }  # no invoke pattern: fall through to manual
          }
        }
        break
      } else { $state = 'signin' }
    } elseif ($title -match 'certification|Deloitte') { $state = 'target' }
  }
  if ($clicked -or $state -eq 'target' -or ($DryRun -and $state -eq 'account-picker')) { break }
}
$outState = if ($clicked) { 'account_selected' } elseif ($state -eq 'signin') { 'manual_step_required' } else { $state }
@{ state = $outState; clicked = $clicked; title = $title; mode = 'native-uia' } | ConvertTo-Json -Compress | Write-Output
exit 0
