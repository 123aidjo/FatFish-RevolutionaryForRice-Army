$ErrorActionPreference = 'Stop'
$icon = Join-Path $PSScriptRoot 'fish.ico'
$starter = Join-Path $PSScriptRoot 'start.ps1'
$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
$target = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
foreach ($file in @($icon, $starter, $target)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "缺少必要文件：$file" }
}
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'Windows 返回的桌面目录不可用，请检查桌面重定向。'
}
$link = Join-Path $desktop '大肥鱼 V2 开源版.lnk'
$arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -WindowStyle Hidden -File "' + $starter + '"'
$shell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $link) {
    $old = $shell.CreateShortcut($link)
    if ($old.TargetPath -ne $target -or $old.Arguments -notlike ('*"' + $starter + '"*')) {
        throw "同名快捷方式不属于本项目，已保留：$link"
    }
}
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = '启动大肥鱼 V2 并打开本机控制面板'
$shortcut.WindowStyle = 7
$shortcut.Save()

$check = $shell.CreateShortcut($link)
if ($check.TargetPath -ne $target -or $check.Arguments -ne $arguments -or
    $check.WorkingDirectory -ne $PSScriptRoot -or $check.IconLocation -ne "$icon,0") {
    throw '快捷方式保存后未能正确解析，请检查桌面权限。'
}
Write-Output $link
