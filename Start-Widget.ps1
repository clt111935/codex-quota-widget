$ErrorActionPreference = 'Stop'
$script:projectRoot = $PSScriptRoot
$script:pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $script:pythonw) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('请先安装 Python 3.11 或更新版本，并添加到 PATH。然后重新启动面板。', 'Codex 用量伴侣') | Out-Null
    exit 1
}
Start-Process -FilePath $script:pythonw.Source -ArgumentList @('"' + (Join-Path $script:projectRoot 'run.py') + '"') -WorkingDirectory $script:projectRoot -WindowStyle Hidden
