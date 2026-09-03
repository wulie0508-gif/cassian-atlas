param(
    [string]$TargetRoot = ""
)

$ErrorActionPreference = 'Stop'
$sourceRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'skills'
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "Skill source directory not found: $sourceRoot"
}

if (-not $TargetRoot) {
    $codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $TargetRoot = Join-Path $codexRoot 'skills'
}

New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
$resolvedTarget = (Resolve-Path -LiteralPath $TargetRoot).Path
$backupRoot = Join-Path $resolvedTarget '.cassian-atlas-backups'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$installed = @()

foreach ($source in Get-ChildItem -LiteralPath $sourceRoot -Directory | Sort-Object Name) {
    $skillManifest = Join-Path $source.FullName 'SKILL.md'
    if (-not (Test-Path -LiteralPath $skillManifest -PathType Leaf)) {
        throw "Skill is missing SKILL.md: $($source.FullName)"
    }
    $destination = Join-Path $resolvedTarget $source.Name
    $destinationParent = [IO.Path]::GetFullPath((Split-Path -Parent $destination))
    if ($destinationParent -ne [IO.Path]::GetFullPath($resolvedTarget)) {
        throw "Refusing to install outside the selected skills directory: $destination"
    }
    if (Test-Path -LiteralPath $destination) {
        New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
        $backup = Join-Path $backupRoot "$($source.Name)-$stamp"
        Move-Item -LiteralPath $destination -Destination $backup
    }
    Copy-Item -LiteralPath $source.FullName -Destination $destination -Recurse
    $installed += $source.Name
}

[pscustomobject]@{
    status = 'installed'
    target = $resolvedTarget
    skills = $installed
    preferred_cli = 'cassian'
    cli_aliases = @('cassian-atlas', 'opentutor', 'open-tutor-ledger', 'english-tracker', 'python -m english_tracker')
    backup_root = if (Test-Path -LiteralPath $backupRoot) { $backupRoot } else { $null }
} | ConvertTo-Json -Depth 4
