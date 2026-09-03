param(
  [string]$Output = (Join-Path $PSScriptRoot '..\docs\assets\cassian-atlas.ico')
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CassianAtlasNativeIcon {
  [DllImport("user32.dll", CharSet = CharSet.Auto)]
  public static extern bool DestroyIcon(IntPtr handle);
}
"@

$resolvedOutput = [System.IO.Path]::GetFullPath($Output)
$bitmap = New-Object System.Drawing.Bitmap 64, 64
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::FromArgb(18, 58, 47))

$cream = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(244, 239, 224)), 4
$cream.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
$mint = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(91, 196, 165))

$left = @(
  (New-Object System.Drawing.Point 13, 18),
  (New-Object System.Drawing.Point 27, 21),
  (New-Object System.Drawing.Point 31, 26),
  (New-Object System.Drawing.Point 31, 49),
  (New-Object System.Drawing.Point 27, 45),
  (New-Object System.Drawing.Point 13, 42),
  (New-Object System.Drawing.Point 13, 18)
)
$right = @(
  (New-Object System.Drawing.Point 51, 18),
  (New-Object System.Drawing.Point 37, 21),
  (New-Object System.Drawing.Point 33, 26),
  (New-Object System.Drawing.Point 33, 49),
  (New-Object System.Drawing.Point 37, 45),
  (New-Object System.Drawing.Point 51, 42),
  (New-Object System.Drawing.Point 51, 18)
)
$graphics.DrawLines($cream, $left)
$graphics.DrawLines($cream, $right)
$graphics.FillEllipse($mint, 20, 27, 6, 6)
$graphics.FillEllipse($mint, 38, 27, 6, 6)
$graphics.FillEllipse($mint, 29, 39, 6, 6)

$handle = $bitmap.GetHicon()
$icon = [System.Drawing.Icon]::FromHandle($handle)
$stream = [System.IO.File]::Open($resolvedOutput, [System.IO.FileMode]::Create)
try {
  $icon.Save($stream)
} finally {
  $stream.Dispose()
  $icon.Dispose()
  [CassianAtlasNativeIcon]::DestroyIcon($handle) | Out-Null
  $cream.Dispose()
  $mint.Dispose()
  $graphics.Dispose()
  $bitmap.Dispose()
}

Write-Output $resolvedOutput
