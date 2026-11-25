param([switch]$NoBrowser, [switch]$Quiet, [switch]$StopService)

$ErrorActionPreference = 'Stop'
if ($env:FISHBOT_USE_WSL -ne '1') {
    $nativePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $nativePython -PathType Leaf)) {
        Write-Error 'Please double-click setup.bat first to prepare the project environment.'
        exit 1
    }
    if ($StopService) {
        Write-Output 'Use Stop Service in the Fatfish desktop window. Legacy WSL mode requires FISHBOT_USE_WSL=1.'
        exit 0
    }
    & $nativePython -c 'import tkinter, fastapi, uvicorn, httpx, PIL, tomli_w, websockets'
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'Project dependencies are incomplete. Run setup.bat and retry.'
        exit 1
    }
    & $nativePython -X utf8 (Join-Path $PSScriptRoot 'desktop.py')
    exit $LASTEXITCODE
}
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
