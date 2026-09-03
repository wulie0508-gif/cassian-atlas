param(
    [Parameter(Mandatory = $true)][string]$InputDir,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [string]$Language = 'zh-Hans-CN',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null

function Await-Operation {
    param([Parameter(Mandatory = $true)]$Operation, [Parameter(Mandatory = $true)][Type]$ResultType)
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
        Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
    [Windows.Globalization.Language]::new($Language)
)
if ($null -eq $engine) { throw "Windows OCR engine is unavailable for $Language" }

$inputRoot = (Resolve-Path -LiteralPath $InputDir).Path
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$outputRoot = (Resolve-Path -LiteralPath $OutputDir).Path
$images = @(Get-ChildItem -LiteralPath $inputRoot -File |
    Where-Object { $_.Extension.ToLowerInvariant() -in @('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff') } |
    Sort-Object Name)

$completed = 0
$failed = 0
$started = Get-Date
foreach ($image in $images) {
    $jsonPath = Join-Path $outputRoot ($image.BaseName + '.json')
    if ((-not $Force) -and (Test-Path -LiteralPath $jsonPath)) { $completed++; continue }
    $stream = $null
    $bitmap = $null
    try {
        $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($image.FullName)) ([Windows.Storage.StorageFile])
        $stream = Await-Operation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lineRows = foreach ($line in $result.Lines) {
            $wordRows = foreach ($word in $line.Words) {
                $rect = $word.BoundingRect
                [ordered]@{ text=$word.Text; x=[math]::Round($rect.X,2); y=[math]::Round($rect.Y,2); width=[math]::Round($rect.Width,2); height=[math]::Round($rect.Height,2) }
            }
            [ordered]@{ text=(($line.Words | ForEach-Object { $_.Text }) -join ' '); words=$wordRows }
        }
        $payload = [ordered]@{
            source_image=$image.FullName; image_width=$bitmap.PixelWidth; image_height=$bitmap.PixelHeight
            angle=$result.TextAngle; text=$result.Text; lines=$lineRows
            ocr_engine="Windows.Media.Ocr/$Language"; generated_at=(Get-Date).ToString('s')
        }
        [System.IO.File]::WriteAllText(
            $jsonPath,
            ($payload | ConvertTo-Json -Depth 8),
            [System.Text.UTF8Encoding]::new($false)
        )
        $completed++
    }
    catch {
        $failed++
        [System.IO.File]::WriteAllText(
            (Join-Path $outputRoot ($image.BaseName + '.error.txt')),
            $_.Exception.ToString(),
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
    if (($completed + $failed) % 25 -eq 0) {
        Write-Output ("progress completed={0} failed={1} total={2}" -f $completed, $failed, $images.Count)
    }
}

[ordered]@{
    input_dir=$inputRoot; output_dir=$outputRoot; total=$images.Count
    completed=$completed; failed=$failed
    elapsed_seconds=[math]::Round(((Get-Date) - $started).TotalSeconds, 1)
} | ConvertTo-Json
