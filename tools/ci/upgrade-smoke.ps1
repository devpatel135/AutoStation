<#
 .SYNOPSIS
  Upgrade smoke test: install the PREVIOUS released installer, serve the NEW registry output locally, run the scheduled path and prove
  that the machine upgrades itself (and that a broken wrapper-core cannot take it down). Blocks the release (release.yml, job windows).

 .DESCRIPTION
  1. downloads the previous installer (the stable asset of the rolling 'installers-latest' release) and checks its published sha256;
     no previous installer yet (first release ever) = notice and exit 0, unless -RequirePrevious
  2. installs it silently, per user, exactly as a user would (tools\ci\smoke-installer.ps1 -Phase install)
  3. signs a COPY of the new registry output with a throwaway Ed25519 key, serves it on 127.0.0.1 and points the machine at it through
     AUTOMAGIX_REGISTRY_URL / AUTOMAGIX_RING=dev (a new version is in ring dev only) / AUTOMAGIX_REGISTRY_PUBKEYS: the signature path
     is exercised, the production key is never used, nothing leaves the runner
  4. runs the scheduled launcher (bin\run-<automation>.cmd daily): self-heal downloads the new automation / framework and STAGES the new
     wrapper-core; then the launcher's own apply-pending swaps it in and a second run proves the new runtime works end to end
  5. asserts versions (every package equals the ring target of the new manifest), the wrapper-core selftest and the run logs
  6. rollback: stages a deliberately broken wrapper-core.pending, runs bin\apply-pending.cmd and asserts that it is rejected
     (wrapper-core.bad, state\rejected-wrapper-core.txt) while the previous copy keeps working
  7. always: stops the local server and uninstalls

  The decisions live in tools\ci\upgrade_check.py (unit-tested); this script only drives Windows.
  Every failure prints one ::error line and exits 1; success exits 0.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')][string]$Automation,
    [Parameter(Mandatory = $true)][string]$RegistryOut,
    [string]$PreviousInstaller = '',
    [string]$Repo = $env:GITHUB_REPOSITORY,
    [string]$ReleaseTag = 'installers-latest',
    [int]$Port = 18099,
    [int]$TimeoutS = 300,
    [switch]$RequirePrevious
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $here)
$smoke = Join-Path $here 'smoke-installer.ps1'
$check = Join-Path $here 'upgrade_check.py'
$homeDir = Join-Path $env:LOCALAPPDATA 'AUTOMAGIX'
$tmpRoot = $(if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() })
$work = Join-Path $tmpRoot 'upgrade-smoke'
$script:server = $null
$script:installed = $false

function Fail([string]$msg) {
    $m = ($msg -replace '[\r\n]+', ' | ')
    Write-Host "::error title=upgrade smoke failed::$m"
    if (Test-Path $homeDir) {
        Get-ChildItem $homeDir -Depth 1 -ErrorAction SilentlyContinue | Select-Object -First 40 -ExpandProperty FullName | Out-Host
        Get-Content (Join-Path $homeDir 'logs\wrapper.log') -ErrorAction SilentlyContinue | Select-Object -Last 40 | Out-Host
    }
    Cleanup
    exit 1
}

function Cleanup {
    if ($script:server) { Stop-Process -Id $script:server.Id -Force -ErrorAction SilentlyContinue; $script:server = $null }
    if ($script:installed) {
        try { & $smoke -Phase uninstall -Automation $Automation | Out-Host } catch { Write-Host "::warning::uninstall during cleanup failed: $($_.Exception.Message)" }
        $script:installed = $false
    }
}

function Py([string[]]$ArgList) {
    $out = & python @ArgList
    if ($LASTEXITCODE -ne 0) { Fail "python $($ArgList -join ' ') exited with $LASTEXITCODE" }
    return $out
}

try {
    if (-not (Test-Path (Join-Path $RegistryOut 'manifest.yaml'))) { Fail "no manifest.yaml in $RegistryOut" }
    New-Item -ItemType Directory -Force $work | Out-Null

    # 1. the previous installer ---------------------------------------------------------------------------------------------------
    if (-not $PreviousInstaller) {
        $PreviousInstaller = Join-Path $work "$Automation-previous-setup.exe"
        $sha = Join-Path $work "$Automation-previous-setup.exe.sha256"
        $got = $true
        try {
            gh release download $ReleaseTag --repo $Repo --pattern "$Automation-setup.exe" --output $PreviousInstaller --clobber
            if ($LASTEXITCODE -ne 0) { $got = $false }
            if ($got) { gh release download $ReleaseTag --repo $Repo --pattern "$Automation-setup.exe.sha256" --output $sha --clobber; if ($LASTEXITCODE -ne 0) { $got = $false } }
        } catch { $got = $false }
        if (-not $got) {
            if ($RequirePrevious) { Fail "no previous installer could be downloaded from release $ReleaseTag" }
            Write-Host "::notice title=upgrade smoke skipped::no previous installer exists yet (first release): nothing to upgrade from"
            exit 0
        }
        $want = ((Get-Content $sha -Raw).Trim() -split '\s+')[0].ToLower()
        $have = (Get-FileHash -Algorithm SHA256 -LiteralPath $PreviousInstaller).Hash.ToLower()
        if ($want -ne $have) { Fail "the previous installer does not match its published sha256 ($have vs $want)" }
    }
    if (-not (Test-Path -LiteralPath $PreviousInstaller)) { Fail "previous installer not found: $PreviousInstaller" }

    # 2. install it ---------------------------------------------------------------------------------------------------------------
    & $smoke -Phase install -Automation $Automation -Installer $PreviousInstaller -TimeoutS $TimeoutS
    if ($LASTEXITCODE -ne 0) { Fail 'the previous installer did not install' }
    $script:installed = $true
    $beforeFile = Join-Path $work 'before.json'
    (Py @($check, 'versions', '--home', $homeDir, '--automation', $Automation)) | Set-Content -LiteralPath $beforeFile -Encoding utf8
    Write-Host "before the upgrade: $(Get-Content $beforeFile -Raw)"

    # 3. the new registry, signed with a throwaway key, served locally ---------------------------------------------------------------
    $reg = Join-Path $work 'registry'
    if (Test-Path $reg) { Remove-Item -Recurse -Force $reg }
    Copy-Item -Recurse -LiteralPath $RegistryOut -Destination $reg
    $seedFile = Join-Path $work 'throwaway.seed'
    $keyOut = (Py @((Join-Path $repoRoot 'tools\sign_registry.py'), '--gen-key', $seedFile)) -join "`n"
    if ($keyOut -notmatch 'public key: ([0-9a-f]{64})') { Fail 'could not generate the throwaway signing key' }
    $pub = $Matches[1]
    $env:REGISTRY_SIGNING_SEED = (Get-Content $seedFile -Raw).Trim()
    Py @((Join-Path $repoRoot 'tools\sign_registry.py'), '--registry-dir', $reg) | Out-Host
    Remove-Item Env:\REGISTRY_SIGNING_SEED
    Remove-Item -Force $seedFile
    if (-not (Test-Path (Join-Path $reg 'manifest.yaml.sig'))) { Fail 'the throwaway signature was not written' }

    $expectedFile = Join-Path $work 'expected.json'
    (Py @($check, 'expected', '--manifest', (Join-Path $reg 'manifest.yaml'), '--automation', $Automation, '--ring', 'dev')) | Set-Content -LiteralPath $expectedFile -Encoding utf8
    Write-Host "the new registry says: $(Get-Content $expectedFile -Raw)"

    $script:server = Start-Process -FilePath python -ArgumentList '-m', 'http.server', "$Port", '--bind', '127.0.0.1', '--directory', $reg -PassThru -WindowStyle Hidden
    $up = $false
    foreach ($i in 1..30) {
        try { Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/manifest.yaml" -TimeoutSec 2 | Out-Null; $up = $true; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $up) { Fail 'the local registry server did not start' }

    $env:AUTOMAGIX_HOME = $homeDir
    $env:AUTOMAGIX_REGISTRY_URL = "http://127.0.0.1:$Port/"
    $env:AUTOMAGIX_RING = 'dev'
    $env:AUTOMAGIX_REGISTRY_PUBKEYS = $pub
    $env:AUTOMAGIX_LOG_TRANSPORT = 'dir'
    $env:AUTOMAGIX_LOG_REMOTE = Join-Path $homeDir 'smoke-logs'
    $env:AUTOMAGIX_NO_TOAST = '1'
    $env:AUTOMAGIX_NO_REMEDIATION = '1'

    # 4. the scheduled path, as Task Scheduler runs it. The automation itself needs its local demo portal (as in smoke-installer.ps1 -Phase run);
    #    the run is allowed to fail here: what this step proves is that self-heal installed the new versions BEFORE the run.
    $payload = Join-Path $homeDir "automations\$Automation"
    $node = Join-Path $homeDir 'execution-framework\node\node.exe'
    $launcher = Join-Path $homeDir "bin\run-$Automation.cmd"
    if (-not (Test-Path $launcher)) { Fail "the scheduled launcher is missing: $launcher" }
    $env:AUTOMATION_PARAM_PORTAL_URL = 'http://127.0.0.1:8099/portal.html'
    $env:AUTOMATION_PARAM_PICKER_HOST = 'localhost:8099'
    $env:AUTOMATION_PARAM_ALLOWED_HOSTS = '127.0.0.1:8099,localhost:8099'
    $env:AUTOMATION_PARAM_ACCOUNT = 'demo.user@example.test'
    $env:AMX_ALLOW_TEST_HOSTS = '1'
    $env:AUTOMATION_PARAM_HEADLESS = 'true'
    $env:AUTOMATION_PARAM_MODE = 'playwright'
    $env:AUTOMATION_PARAM_TIMEOUT_S = '45'
    $demo = $null
    if (Test-Path (Join-Path $payload 'demo\server.js')) {
        $demo = Start-Process -FilePath $node -ArgumentList 'demo\server.js' -WorkingDirectory $payload -PassThru -WindowStyle Hidden
        Start-Sleep -Seconds 3
    }
    try {
        $p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$launcher`" daily" -PassThru -WindowStyle Hidden
        if (-not $p.WaitForExit($TimeoutS * 1000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Fail "the scheduled run did not finish in ${TimeoutS}s" }
        Write-Host "scheduled run #1 exit code: $($p.ExitCode)"
    } finally {
        if ($demo) { Stop-Process -Id $demo.Id -Force -ErrorAction SilentlyContinue }
    }

    # the launcher of the NEXT scheduled run swaps in the staged wrapper-core before Python starts
    $apply = Join-Path $homeDir 'bin\apply-pending.cmd'
    if (-not (Test-Path $apply)) { Fail "bin\apply-pending.cmd is missing after the upgrade" }
    cmd.exe /c "`"$apply`""
    if (Test-Path (Join-Path $homeDir 'wrapper-core.pending')) { Fail 'wrapper-core.pending is still staged after bin\apply-pending.cmd' }

    # 5. the second scheduled run uses the NEW runtime end to end (real run against the local demo portal, must succeed)
    & $smoke -Phase run -Automation $Automation
    if ($LASTEXITCODE -ne 0) { Fail 'the run after the upgrade failed' }
    Py @($check, 'verify', '--home', $homeDir, '--automation', $Automation, '--expected', $expectedFile) | Out-Host
    $env:PYTHONPATH = Join-Path $homeDir 'wrapper-core'
    & (Join-Path $homeDir 'python\python.exe') -m wrapper_core selftest
    if ($LASTEXITCODE -ne 0) { Fail 'wrapper_core selftest fails after the upgrade' }
    $runs = @(Get-ChildItem -Recurse -Path $env:AUTOMAGIX_LOG_REMOTE -Filter *.json -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match 'run[\\/]' })
    if ($runs.Count -lt 1) { Fail 'no run log was written by the upgraded machine' }
    Write-Host "upgrade OK ($($runs.Count) run log(s))"

    # 6. rollback: a broken wrapper-core must be rejected and the working copy must stay
    $versionBefore = (Get-Content (Join-Path $homeDir 'wrapper-core\VERSION') -Raw).Trim()
    (Py @($check, 'versions', '--home', $homeDir, '--automation', $Automation)) | Set-Content -LiteralPath $beforeFile -Encoding utf8
    Py @($check, 'break', '--home', $homeDir) | Out-Host
    cmd.exe /c "`"$apply`""
    Py @($check, 'rollback', '--home', $homeDir, '--before', $beforeFile) | Out-Host
    & (Join-Path $homeDir 'python\python.exe') -m wrapper_core selftest
    if ($LASTEXITCODE -ne 0) { Fail "the old wrapper-core $versionBefore does not work after the rollback" }
    Write-Host "rollback OK: wrapper-core $versionBefore still runs"
} catch {
    Fail $_.Exception.Message
}
Cleanup
exit 0
