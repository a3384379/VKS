$ErrorActionPreference = 'Stop'
$VksRepo = Split-Path -Parent $PSScriptRoot
$VksPython = Join-Path $VksRepo '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $VksPython -PathType Leaf)) {
    Write-Error 'Run uv sync --frozen first to create the VKS Python 3.12 environment.'
    exit 1
}
& $VksPython (Join-Path $PSScriptRoot 'dev_studio.py') @args
exit $LASTEXITCODE
