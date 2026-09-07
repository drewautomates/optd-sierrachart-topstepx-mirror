<#
.SYNOPSIS
    Copy Manual_Mirror.cpp and tsx_manual_emit.h into a Sierra Chart ACS_Source folder.

.DESCRIPTION
    There is deliberately NO default target. Sierra Chart installs somewhere
    different on every machine, so the target must come from -Target or the
    SC_ACS_SOURCE environment variable. The script never guesses a path.

    ACS_Source sits directly inside your Sierra Chart installation folder,
    next to Data\. It is the folder Analysis > Build Custom Studies DLL
    compiles from. After deploying, build inside Sierra Chart.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File sierra\deploy.ps1 -Target "<SierraChart>\ACS_Source"
#>
[CmdletBinding()]
param(
    [string]$Target,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$SourceDir = $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($Target)) { $Target = $env:SC_ACS_SOURCE }
if ([string]::IsNullOrWhiteSpace($Target)) {
    Write-Host "ERROR: no target ACS_Source folder specified." -ForegroundColor Red
    Write-Host '  Pass -Target "<SierraChart>\ACS_Source" or set $env:SC_ACS_SOURCE.'
    exit 1
}
if (-not (Test-Path -LiteralPath $Target)) {
    Write-Host "ERROR: target ACS_Source not found: $Target" -ForegroundColor Red
    exit 1
}

$files = @('Manual_Mirror.cpp', 'tsx_manual_emit.h')
foreach ($name in $files) {
    $src = Join-Path $SourceDir $name
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host "ERROR: missing $src" -ForegroundColor Red
        exit 1
    }
    if ($WhatIf) {
        Write-Host "  WOULD COPY  $name -> $Target"
    } else {
        Copy-Item -LiteralPath $src -Destination (Join-Path $Target $name) -Force
        Write-Host "  deployed    $name -> $Target"
    }
}

if (-not $WhatIf) {
    Write-Host ""
    Write-Host "Next: Sierra Chart -> Analysis > Build Custom Studies DLL > Build > Manual_Mirror"
    Write-Host "      Then on ONE chart -> Analysis > Studies > Add Custom Study -> TopstepX Manual Mirror."
}
