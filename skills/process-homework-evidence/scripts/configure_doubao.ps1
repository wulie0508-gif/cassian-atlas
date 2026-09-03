[CmdletBinding()]
param(
    [string]$Model,
    [string]$BaseUrl = "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    [string]$ConfigPath,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$userProfilePath = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path (Join-Path $userProfilePath ".opentutor") "doubao.env"
}
$resolvedConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$configDirectory = Split-Path -Parent $resolvedConfigPath

function Get-ConfigMap {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $pair = $trimmed.Split("=", 2)
        if ($pair.Count -eq 2) { $values[$pair[0].Trim()] = $pair[1].Trim() }
    }
    return $values
}

if ($Status) {
    $existing = Get-ConfigMap -Path $resolvedConfigPath
    [pscustomobject]@{
        ConfigPath = $resolvedConfigPath
        Exists = Test-Path -LiteralPath $resolvedConfigPath
        ApiKeyConfigured = -not [string]::IsNullOrWhiteSpace($existing["OPEN_TUTOR_DOUBAO_API_KEY"])
        BaseUrl = $existing["OPEN_TUTOR_DOUBAO_BASE_URL"]
        Model = $existing["OPEN_TUTOR_DOUBAO_MODEL"]
    } | Format-List
    exit 0
}

$uri = $null
if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne "https" -or $uri.UserInfo) {
    throw "BaseUrl must be an HTTPS URL without embedded credentials."
}

if ([string]::IsNullOrWhiteSpace($Model)) {
    $Model = Read-Host "请输入火山方舟的模型或推理接入点 ID"
}
if ([string]::IsNullOrWhiteSpace($Model) -or $Model.Contains("`n") -or $Model.Contains("`r")) {
    throw "Model/endpoint ID is required and must be one line."
}

$secureKey = Read-Host "请输入豆包/火山方舟 API Key（输入不会显示）" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey.Contains("`n") -or $apiKey.Contains("`r")) {
        throw "API Key is required and must be one line."
    }

    New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
    $lines = @(
        "OPEN_TUTOR_DOUBAO_API_KEY=$apiKey"
        "OPEN_TUTOR_DOUBAO_BASE_URL=$BaseUrl"
        "OPEN_TUTOR_DOUBAO_MODEL=$Model"
        "OPEN_TUTOR_DOUBAO_TIMEOUT_SECONDS=30"
        "OPEN_TUTOR_DOUBAO_MAX_RETRIES=2"
        "OPEN_TUTOR_DOUBAO_RETRY_BASE_SECONDS=0.5"
    )
    [IO.File]::WriteAllLines($resolvedConfigPath, $lines, [Text.UTF8Encoding]::new($false))

    $fileSecurity = [Security.AccessControl.FileSecurity]::new()
    $fileSecurity.SetAccessRuleProtection($true, $false)
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $fileSecurity.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($currentSid, "FullControl", $allow))
    $fileSecurity.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($systemSid, "FullControl", $allow))
    Set-Acl -LiteralPath $resolvedConfigPath -AclObject $fileSecurity
}
finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    $apiKey = $null
    $secureKey = $null
}

Write-Host "豆包配置已保存到私密目录。"
Write-Host "Config: $resolvedConfigPath"
Write-Host "Model: $Model"
Write-Host "API Key: configured (hidden)"
