<#
 Native (no DevTools protocol) fallback for the certification-site automation.

 Managed Edge installs often set the policy RemoteDebuggingAllowed=0 ("DevTools remote debugging is disallowed by the system admin"),
 which makes Playwright unable to drive Edge. This script respects that policy: it opens the page in the user's normal Edge
 (their profile, their SSO) and uses Windows UI Automation - the same accessibility API screen readers use - to find the
 Microsoft account tile for -Account and press it. It never types anything, then stops.

 Locale independence: nothing here matches window titles or button captions in a particular language. The state comes from
 (1) whether a control whose accessible name contains the account e-mail exists (the picker), and (2) the HOST in the page's address
 bar (Microsoft sign-in hosts vs the portal host), both of which are the same on every Windows display language.

 Window hygiene: it only ever touches windows it opened itself, and CLOSES them when it is done (after the click has had a few seconds to
 take effect, or when the user was already signed in). It leaves the window open only when a person has to continue there
 (state manual_step_required).

 Prints ONE json line: {"state": "...", "clicked": bool, ...} ; exit 0 unless the script itself failed.
 states: account_selected | target | account-picker (dry run) | manual_step_required | other | error
#>
param(
  [Parameter(Mandatory)][string]$Url,
  [Parameter(Mandatory)][ValidatePattern('^[^@\s]+@[^@\s]+\.[^@\s]+$')][string]$Account,
  [int]$TimeoutS = 60,
  [string]$PortalHost = '',
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
if (-not $PortalHost) { try { $PortalHost = ([uri]$Url).Host } catch { $PortalHost = '' } }
$edge = @("$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { Write-Output '{"state":"error","clicked":false,"detail":"msedge.exe not found"}'; exit 0 }

function Edge-Windows {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, 'Chrome_WidgetWin_1')
  # every top-level Chromium window; Edge's own windows are told apart by the process image, not by a translated title
  @($root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond) | Where-Object {
      try { (Get-Process -Id $_.Current.ProcessId -ErrorAction Stop).ProcessName -eq 'msedge' } catch { $false } })
}

# the host shown in the address bar of an Edge window (the omnibox is the Edit control whose Value is a URL/host; its caption is localised, its value is not)
function Window-Host($win) {
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
  foreach ($el in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
    try {
      $v = [string]$el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
      if ($v -match '^(?:https?://)?([A-Za-z0-9.-]+\.[A-Za-z]{2,}|localhost|127\.0\.0\.1)(?::\d+)?(?:[/?#]|$)') { return $Matches[1].ToLowerInvariant() }
    } catch { }
  }
  return ''
}

function Find-Tile($win, $account) {
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsInvokePatternAvailableProperty, $true)
  foreach ($el in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
    $n = $el.Current.Name
    if ($n -and $n.IndexOf($account, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $el }
  }
  # a tile is often a non-invokable container whose child text carries the e-mail: take the nearest invokable ancestor
  $txt = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $account)
  $hit = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $txt)
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  while ($hit -ne $null) {
    $p = $null
    try { $p = $hit.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern) } catch { $p = $null }
    if ($p -ne $null) { return $hit }
    try { $hit = $walker.GetParent($hit) } catch { return $null }
  }
  return $null
}

function Close-Ours($handles) {
  foreach ($w in (Edge-Windows | Where-Object { $handles -contains $_.Current.NativeWindowHandle })) {
    try { $w.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close() } catch { }
  }
}

$isMs = '(^|\.)login\.(microsoftonline|live|microsoft)\.com$'
# only ever touch the windows WE open: remember the existing ones first
$before = @(Edge-Windows | ForEach-Object { $_.Current.NativeWindowHandle })
Start-Process -FilePath $edge -ArgumentList @('--new-window', $Url) | Out-Null
$ours = New-Object System.Collections.Generic.List[int]

$deadline = (Get-Date).AddSeconds($TimeoutS)
$state = 'other'; $clicked = $false; $hostSeen = ''
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 1500
  foreach ($w in (Edge-Windows | Where-Object { $before -notcontains $_.Current.NativeWindowHandle })) {
    $h = $w.Current.NativeWindowHandle
    if (-not $ours.Contains($h)) { $ours.Add($h) }
    $tile = $null
    try { $tile = Find-Tile $w $Account } catch { $tile = $null }
    $whost = Window-Host $w
    if ($whost) { $hostSeen = $whost }
    if ($tile) {
      $state = 'account-picker'
      if (-not $DryRun) {
        try { $tile.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $clicked = $true } catch { $state = 'manual_step_required' }
      }
      break
    } elseif ($whost -and $PortalHost -and $whost -eq $PortalHost.ToLowerInvariant()) {
      $state = 'target'
    } elseif ($whost -match $isMs) {
      $state = 'signin'      # a Microsoft sign-in page that does NOT offer this account: a person has to continue
    }
  }
  if ($clicked -or $state -eq 'target' -or $state -eq 'manual_step_required' -or ($DryRun -and $state -eq 'account-picker')) { break }
}
if ($clicked) { Start-Sleep -Seconds 4 }     # let the redirect that the click starts begin before the window goes away
$outState = if ($clicked) { 'account_selected' } elseif ($state -eq 'signin') { 'manual_step_required' } else { $state }
if ($outState -ne 'manual_step_required') { Close-Ours $ours }
@{ state = $outState; clicked = $clicked; host = $hostSeen; mode = 'native-uia' } | ConvertTo-Json -Compress | Write-Output
exit 0
