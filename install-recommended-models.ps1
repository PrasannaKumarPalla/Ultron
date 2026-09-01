$ErrorActionPreference = "Stop"
$ultronRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logPath = Join-Path $ultronRoot "data\model-install.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null

$models = @("qwen3.6:27b", "qwen3-coder:30b")
foreach ($model in $models) {
    "[$(Get-Date -Format o)] Installing $model" | Add-Content -LiteralPath $logPath
    & ollama pull $model 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) { throw "Failed to install $model" }
}
"[$(Get-Date -Format o)] Recommended models installed" | Add-Content -LiteralPath $logPath
