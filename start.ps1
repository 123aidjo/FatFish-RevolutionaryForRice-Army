param([switch]$NoBrowser, [switch]$Quiet, [switch]$StopService)

$ErrorActionPreference = 'Stop'
$python = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'FishbotV2Public\venv\Scripts\python.exe'
$helper = Join-Path $PSScriptRoot 'launcher_windows.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $helper -PathType Leaf)) {
    $message = 'V2 的 Windows 虚拟环境或启动文件缺失。请按项目 README 重建 V2 专用 Windows 环境。'
    $logDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'FishbotV2Public\launcher'
    try {
        [void][IO.Directory]::CreateDirectory($logDirectory)
        Add-Content -LiteralPath (Join-Path $logDirectory 'launcher.log') -Encoding UTF8 -Value (
            '[{0:yyyy-MM-dd HH:mm:ss}] {1}' -f (Get-Date), $message
        )
    } catch { Write-Warning '无法写入 V2 启动日志，请检查目录权限。' }
    [Console]::Error.WriteLine($message)
    if (-not $Quiet) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show($message, '大肥鱼 V2')
    }
    exit 1
}

# Python 启动器使用 Unicode 错误提示，并继承桌面启动器的退出码。
$mode = if ($StopService) { 'stop' } else { 'start' }
$options = @()
if ($NoBrowser) { $options += '--no-browser' }
if ($Quiet) { $options += '--quiet' }
& $python -X utf8 $helper $mode @options
exit $LASTEXITCODE
