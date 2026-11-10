param(
    [string]$Python = 'python',
    [string]$IndexUrl = '',
    [switch]$SkipInstall,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'quickstart.py'))
if ($IndexUrl) { $arguments += @('--index-url', $IndexUrl) }
if ($SkipInstall) { $arguments += '--skip-install' }
if ($SkipBuild) { $arguments += '--skip-build' }
& $Python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
