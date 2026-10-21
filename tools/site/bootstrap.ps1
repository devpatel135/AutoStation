<#
.SYNOPSIS
  Download, VERIFY and install an AUTOSTATION automation for the current user (no administrator rights).

.DESCRIPTION
  Download this script, read it, then run it. It never executes downloaded text as code.

  What it does
    1. reads the install catalog published on the AutoStation site (install/catalog.json)
    2. downloads the installer for the automation (a GitHub Release asset)
    3. computes the installer's SHA-256 and compares it with the hash published on the site (an independent origin) and,
       when reachable, with the .sha256 file next to the release asset. Any mismatch deletes the file and stops.
    4. shows the Authenticode signature status (unsigned dev builds are allowed but called out)
    5. runs the per-user installer silently: /S /RING=<ring>   (use -NoRun to stop after verification)

.EXAMPLE
  .\bootstrap.ps1 -Automation automation-hello-world -Ring pilot

.EXAMPLE
  .\bootstrap.ps1 -Automation automation-hello-world -InstallerPath .\automation-hello-world-setup.exe -NoRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Automation,
    [ValidateSet('dev', 'pilot', 'broad')][string]$Ring = 'broad',
    [string]$CatalogUrl = '@@PAGES_URL@@install/catalog.json',
    [string]$InstallerPath,
    [switch]$NoRun,
    [switch]$AllowUnverified
)
$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 can inherit a PowerShell 7 PSModulePath (CI runners, pwsh-launched shells) and then fail to load the
# built-in security modules. Pin the built-in module path there; PowerShell 7 itself is left alone.
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    $env:PSModulePath = (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules') + ';' + (Join-Path $env:windir 'System32\WindowsPowerShell\v1.0\Modules')
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

function Get-Text([string]$Location) {
    if (Test-Path -LiteralPath $Location) { return [IO.File]::ReadAllText((Resolve-Path -LiteralPath $Location).Path) }
    return (Invoke-WebRequest -UseBasicParsing -Uri $Location -TimeoutSec 60).Content
}

function Get-Sha256Text([string]$Text) {
    # accepts "<hash>" or "<hash>  <file name>" (sha256sum / Get-FileHash style)
    $m = [regex]::Match($Text, '(?i)\b[0-9a-f]{64}\b')
    if ($m.Success) { return $m.Value.ToLowerInvariant() }
    return $null
}

try {
    Write-Host "Reading the install catalog: $CatalogUrl"
    $catalog = (Get-Text $CatalogUrl) | ConvertFrom-Json
    $entry = $catalog.automations.$Automation
    if (-not $entry) {
        $names = ($catalog.automations.PSObject.Properties.Name) -join ', '
        throw "automation '$Automation' is not in the catalog. Available: $names"
    }
    if ($entry.status -eq 'paused') {
        Write-Warning "$Automation is currently PAUSED by the platform operators ($($entry.paused_reason)). It will install but will not run until it is resumed."
    }
    $version = $entry.rings.$Ring
    if (-not $version) { throw "$Automation is not released to the '$Ring' ring yet." }
    Write-Host "$Automation : ring '$Ring' runs version $version"

    $expected = $null
    if ($entry.installer -and $entry.installer.sha256) { $expected = ([string]$entry.installer.sha256).ToLowerInvariant() }

    if ($InstallerPath) {
        $file = (Resolve-Path -LiteralPath $InstallerPath).Path
        $downloaded = $false
    } else {
        $url = [string]$entry.installer.url
        if (-not $url.StartsWith('https://')) { throw "refusing a non-HTTPS installer URL: $url" }
        $file = Join-Path $env:TEMP ("autostation-{0}-{1}.exe" -f $Automation, [guid]::NewGuid().ToString('N').Substring(0, 8))
        Write-Host "Downloading $url"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $file -TimeoutSec 600
        $downloaded = $true

        # second, independent source: the .sha256 asset published next to the installer
        $releaseSha = $null
        try { $releaseSha = Get-Sha256Text (Get-Text ($url + '.sha256')) } catch { Write-Verbose "no .sha256 asset reachable: $($_.Exception.Message)" }
        if ($releaseSha -and $expected -and $releaseSha -ne $expected) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
            throw "the hash published on the site ($expected) and the release asset hash ($releaseSha) DISAGREE. Not installing."
        }
        if ($releaseSha -and -not $expected) { $expected = $releaseSha; Write-Warning 'only the release-asset hash is available (the site did not publish one).' }
    }

    $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "SHA-256 of the installer: $actual"
    if ($expected) {
        if ($actual -ne $expected) {
            if ($downloaded) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
            throw "HASH MISMATCH. Expected $expected but the file is $actual. Not installing."
        }
        Write-Host 'Hash verified against the published value.' -ForegroundColor Green
    } elseif ($AllowUnverified) {
        Write-Warning 'No published hash is available; continuing because -AllowUnverified was given.'
    } else {
        if ($downloaded) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
        throw 'No published SHA-256 is available for this installer, so it cannot be verified. Ask the platform team, or re-run with -AllowUnverified if you accept that risk.'
    }

    try {
        $sig = Get-AuthenticodeSignature -LiteralPath $file
        if ($sig.Status -eq 'Valid') { Write-Host "Signature: valid ($($sig.SignerCertificate.Subject))" -ForegroundColor Green }
        else { Write-Warning "Signature status: $($sig.Status). Unsigned dev builds show 'Unknown publisher'; production releases should be signed." }
    } catch {
        Write-Warning "Authenticode check is not available on this platform: $($_.Exception.Message)"
    }

    if ($NoRun) {
        Write-Host "Verified only (-NoRun). Installer: $file"
        exit 0
    }
    Write-Host "Installing for the current user (no administrator rights): $Automation, ring $Ring"
    $p = Start-Process -FilePath $file -ArgumentList @('/S', "/RING=$Ring") -Wait -PassThru
    if ($downloaded) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
    if ($p.ExitCode -ne 0) { throw "the installer exited with code $($p.ExitCode)" }
    Write-Host "Installed. First run starts automatically; status: %LOCALAPPDATA%\AUTOMAGIX\state\installed.json" -ForegroundColor Green
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
