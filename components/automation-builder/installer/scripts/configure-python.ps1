<#
.SYNOPSIS
  Make an embeddable CPython find wrapper-core.

.DESCRIPTION
  Embeddable Python ignores PYTHONPATH whenever a pythonXY._pth file exists, so wrapper-core (and the payloads it
  spawns, which inherit the same interpreter) would not be importable. This rewrites the ._pth file to:
      pythonXY.zip, ., ..\wrapper-core, import site
  (paths are relative to the ._pth file; ..\wrapper-core resolves to <AUTOMAGIX>\wrapper-core).
  No-op with exit 0 when the interpreter has no ._pth (a normal system Python).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonDir,
    [string]$WrapperRelPath = '..\wrapper-core'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PythonDir)) { throw "python dir not found: $PythonDir" }

$pth = @(Get-ChildItem -LiteralPath $PythonDir -Filter 'python*._pth' -File)
if ($pth.Count -eq 0) { Write-Output "no ._pth file: nothing to do"; exit 0 }
foreach ($f in $pth) {
    $stem = $f.Name -replace '\._pth$', ''
    $lines = @("$stem.zip", '.', $WrapperRelPath, '', 'import site')
    [System.IO.File]::WriteAllText($f.FullName, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "configured $($f.Name)"
}
exit 0
