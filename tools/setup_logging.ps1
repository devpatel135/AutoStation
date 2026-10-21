<#
.SYNOPSIS
  One-time setup for LIVE logging: a private logs repo + the LOGGING_TOKEN secret that turns on "connected" installers and
  the live dashboards. Safe to re-run.

.DESCRIPTION
  What it does (with the GitHub CLI `gh`, signed in as a repo admin):
    1. creates the private repo <Owner>/AutoStation-logs (with a README so it has a default branch)
    2. adds a ruleset that makes its default branch read-only for everyone (the token can only write the `ingest` branch)
    3. prompts for a fine-grained token (you create it in the browser - GitHub offers no API for that) and stores it as the
       repository secret LOGGING_TOKEN on <Owner>/<Repo>
    4. dispatches the installers + site workflows so the connected installer and live dashboards are built

  Without `gh` it prints the same steps as a checklist.

.EXAMPLE
  .\tools\setup_logging.ps1 -Owner devpatel135 -Repo AutoStation
#>
[CmdletBinding()]
param(
    [string]$Owner = 'devpatel135',
    [string]$Repo = 'AutoStation',
    [string]$LogsRepo = 'AutoStation-logs'
)
$ErrorActionPreference = 'Stop'

$tokenUrl = 'https://github.com/settings/personal-access-tokens/new'
function Show-Manual {
    Write-Host @"

Manual steps (about 3 minutes):
 1. Create a PRIVATE repo named '$LogsRepo' under '$Owner' (tick 'Add a README file').
    https://github.com/new?name=$LogsRepo&visibility=private
 2. Create a fine-grained personal access token:  $tokenUrl
      - Repository access: Only select repositories -> $Owner/$LogsRepo
      - Permissions: Repository -> Contents: Read and write   (Metadata: Read-only is added automatically)
      - Expiration: pick a date and put the renewal in your calendar
 3. Add it to the code repo as a secret named LOGGING_TOKEN:
    https://github.com/$Owner/$Repo/settings/secrets/actions/new
 4. (Recommended) In $Owner/$LogsRepo -> Settings -> Rules -> Rulesets -> New branch ruleset: target the default branch and
    enable 'Restrict updates' + 'Restrict deletions' + 'Block force pushes'. The token then can only ever add logs on the `ingest` branch.
 5. Run the 'installers' and 'site' workflows (Actions tab -> Run workflow), or push any commit.
"@
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) { Write-Warning 'The GitHub CLI (gh) is not installed, so here is the manual checklist.'; Show-Manual; exit 0 }
& gh auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning 'gh is not signed in (run: gh auth login). Falling back to the manual checklist.'; Show-Manual; exit 0 }

# 1. logs repo
& gh repo view "$Owner/$LogsRepo" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating private repo $Owner/$LogsRepo ..."
    & gh repo create "$Owner/$LogsRepo" --private --add-readme --description 'AutoStation run logs (written by installed automations; read by the site workflow)'
    if ($LASTEXITCODE -ne 0) { throw 'could not create the logs repo' }
} else { Write-Host "$Owner/$LogsRepo already exists." }

# 2. ruleset: default branch read-only
$ruleset = @'
{
  "name": "default-branch-read-only",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [ { "type": "deletion" }, { "type": "non_fast_forward" }, { "type": "update" } ]
}
'@
try {
    $ruleset | & gh api "repos/$Owner/$LogsRepo/rulesets" --method POST --input - | Out-Null
    Write-Host 'ruleset added: the default branch of the logs repo is read-only.'
} catch { Write-Warning "could not add the ruleset (maybe it exists, or your plan does not allow it): $($_.Exception.Message)" }

# 3. token -> secret
Write-Host "`nCreate the token now:  $tokenUrl"
Write-Host "  Only select repositories -> $Owner/$LogsRepo ; Repository permissions -> Contents: Read and write"
Start-Process $tokenUrl | Out-Null
$secure = Read-Host 'Paste the token (input is hidden)' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $plain) { throw 'no token entered' }
$plain | & gh secret set LOGGING_TOKEN --repo "$Owner/$Repo"
if ($LASTEXITCODE -ne 0) { throw 'could not store the secret' }
$plain = $null
Write-Host 'LOGGING_TOKEN stored.'

# 4. rebuild
foreach ($wf in 'installer-dev.yml', 'site.yml') {
    try { & gh workflow run $wf --repo "$Owner/$Repo" | Out-Null; Write-Host "dispatched $wf" } catch { Write-Warning "could not dispatch $wf" }
}
Write-Host "`nDone. In a few minutes the install page serves a CONNECTED installer; after it runs once the dashboards go live."
