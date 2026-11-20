param([switch]$Quiet)

# 启停共用同一个 Windows 互斥和 WSL 所有权检查。
& (Join-Path $PSScriptRoot 'start.ps1') -StopService -Quiet:$Quiet
exit $LASTEXITCODE
