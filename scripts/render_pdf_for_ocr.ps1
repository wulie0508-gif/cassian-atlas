param(
    [Parameter(Mandatory = $true)][string]$InputPdf,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [string]$PdfToPpm,
    [int]$Dpi = 150,
    [int]$JpegQuality = 82
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $InputPdf).Path
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$target = (Resolve-Path -LiteralPath $OutputDir).Path

if (-not $PdfToPpm) {
    $command = Get-Command pdftoppm -ErrorAction SilentlyContinue
    if ($command) { $PdfToPpm = $command.Source }
}
if (-not $PdfToPpm) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin\pdftoppm.exe'
    if (Test-Path -LiteralPath $bundled) { $PdfToPpm = $bundled }
}
if (-not $PdfToPpm -or -not (Test-Path -LiteralPath $PdfToPpm)) {
    throw 'pdftoppm was not found. Install Poppler or pass -PdfToPpm.'
}

$prefix = Join-Path $target 'page'
& $PdfToPpm -jpeg -jpegopt "quality=$JpegQuality" -r $Dpi $source $prefix
if ($LASTEXITCODE -ne 0) { throw "pdftoppm failed with exit code $LASTEXITCODE" }

[ordered]@{
    input_pdf=$source; output_dir=$target; dpi=$Dpi
    pages=@(Get-ChildItem -LiteralPath $target -File -Filter 'page-*.jpg').Count
} | ConvertTo-Json
