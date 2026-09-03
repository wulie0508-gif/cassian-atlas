param(
    [string]$OutputDirectory = "",
    [string]$Version = ""
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $head = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $head) {
        throw 'Unable to resolve the release commit.'
    }
    $dirty = git status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to inspect the Git worktree.'
    }
    if ($dirty) {
        throw 'Release bundles must be built from a committed, clean worktree.'
    }

    $projectVersion = (python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $projectVersion) {
        throw 'Unable to read the package version.'
    }
    $runtimeVersion = (python -c "import sys; sys.path.insert(0, 'src'); import english_tracker; print(english_tracker.__version__)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $runtimeVersion) {
        throw 'Unable to read the runtime version.'
    }
    if ($runtimeVersion -ne $projectVersion) {
        throw "Runtime version $runtimeVersion does not match package version $projectVersion."
    }
    if (-not $Version) {
        $Version = $projectVersion
    }
    if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]*$') {
        throw 'Version may contain only letters, numbers, dots, underscores, and hyphens.'
    }
    if ($Version -ne $projectVersion) {
        throw "Requested version $Version does not match package version $projectVersion."
    }

    if (-not $OutputDirectory) {
        $OutputDirectory = Join-Path $repoRoot 'dist'
    }
    New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
    $resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path

    python -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed.' }
    python -B scripts/release_privacy_audit.py
    if ($LASTEXITCODE -ne 0) { throw 'Current-tree privacy audit failed.' }
    python -B scripts/release_privacy_audit.py --history
    if ($LASTEXITCODE -ne 0) { throw 'Git-history privacy audit failed.' }
    git diff --check
    if ($LASTEXITCODE -ne 0) { throw 'Git whitespace check failed.' }

    $headAfterChecks = (git rev-parse HEAD).Trim()
    $dirtyAfterChecks = git status --porcelain --untracked-files=all
    if ($headAfterChecks -ne $head -or $dirtyAfterChecks) {
        throw 'The release commit or worktree changed while checks were running.'
    }

    $archiveName = "cassian-atlas-codex-app-v$Version.zip"
    $archivePath = Join-Path $resolvedOutput $archiveName
    if (Test-Path -LiteralPath $archivePath) {
        throw "Refusing to overwrite an existing bundle: $archivePath"
    }
    git archive --format=zip --prefix="cassian-atlas-$Version/" --output=$archivePath $head
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed.' }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $prefix = "cassian-atlas-$Version/"
        $entryNames = @($archive.Entries | ForEach-Object { $_.FullName })
        $requiredEntries = @(
            "${prefix}AGENTS.md",
            "${prefix}docs/CODEX_APP.md",
            "${prefix}skills/route-learning-task/SKILL.md",
            "${prefix}src/english_tracker/migrations/014_verified_selection_and_explanations.sql",
            "${prefix}src/english_tracker/web/index.html",
            "${prefix}site/index.html",
            "${prefix}tests/test_codex_first_e2e.py"
        )
        foreach ($required in $requiredEntries) {
            if ($entryNames -notcontains $required) {
                throw "Release bundle is missing required entry: $required"
            }
        }
        $skillManifests = @($entryNames | Where-Object { $_ -match '^cassian-atlas-[^/]+/skills/[^/]+/SKILL\.md$' })
        if ($skillManifests.Count -ne 12) {
            throw "Expected 12 bundled skill manifests, found $($skillManifests.Count)."
        }
    }
    finally {
        $archive.Dispose()
    }

    $digest = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = "$archivePath.sha256"
    "$digest  $archiveName" | Set-Content -LiteralPath $checksumPath -Encoding ascii -NoNewline

    [pscustomobject]@{
        status = 'built'
        version = $Version
        commit = $head
        archive = $archivePath
        sha256 = $digest
        checksum = $checksumPath
        contents = @('Codex project policy', 'specialist skills', 'Cassian Atlas CLI and API', 'migrations', 'Teacher Console', 'synthetic public site', 'documentation and tests')
    } | ConvertTo-Json -Depth 3
}
finally {
    Pop-Location
}
