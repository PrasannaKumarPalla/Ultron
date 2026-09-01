param([switch]$NoOpen)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Ultron is not installed. Follow the development setup in README.md first."
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw "Ollama is required. Install Ollama and try again."
}
if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath (Get-Command ollama).Source -ArgumentList "serve" -WindowStyle Hidden
}

Set-Location $root
if (-not $NoOpen) {
    Start-Process "http://127.0.0.1:8766/"
}
& $python -m uvicorn ultron.api:app --host 127.0.0.1 --port 8766
