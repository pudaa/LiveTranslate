<#
.SYNOPSIS
    Create or refresh the LiveTranslate conda environment (Windows).

.DESCRIPTION
    Why this exists instead of install.ps1:

      * install.ps1 creates a project-local .venv and installs Python via winget.
        This script keeps the project on an isolated conda env instead, so the
        global / base interpreter is never touched.

      * PyPI's Windows torch wheel is CPU-only (verified: ~106 MB). The CUDA
        build only exists on download.pytorch.org (~2.4 GB), which is slow from
        China (0.1-1 MB/s). So this script prefers wheels you already downloaded
        into .pip-cache\wheels\ and only falls back to the network. Use
        -TorchWheelDir to point somewhere else.

      * Everything except torch comes from a domestic mirror (~17 MB/s measured
        against PyPI's few hundred KB/s).

.PARAMETER EnvName
    conda environment name. Default: livetranslate

.PARAMETER Cuda
    PyTorch CUDA build used when no local wheel is found.
    cu124 (default) / cu126 / cu128 / cpu

.PARAMETER Index
    pip index for the non-torch requirements. Defaults to the Tsinghua mirror.

.PARAMETER TorchWheelDir
    Directory holding pre-downloaded torch-<Cuda>.whl and torchaudio-<Cuda>.whl.
    Default: .pip-cache\wheels

.PARAMETER SkipModels
    Skip pre-fetching the Silero VAD + SenseVoice models (~1 GB).

.PARAMETER SkipVerify
    Skip the runtime self-check at the end.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup-conda.ps1

.EXAMPLE
    # CPU-only env, e.g. to run the test suite on a machine without a GPU
    powershell -ExecutionPolicy Bypass -File .\setup-conda.ps1 -Cuda cpu
#>
param(
    [string]$EnvName = "livetranslate",
    [ValidateSet("cu124", "cu126", "cu128", "cpu")]
    [string]$Cuda = "cu124",
    [string]$Index = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$TorchWheelDir = "",
    [switch]$SkipModels,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Root

if ([string]::IsNullOrWhiteSpace($TorchWheelDir)) {
    $TorchWheelDir = Join-Path $Root ".pip-cache\wheels"
}

function Write-Step($n, $text) {
    Write-Host ""
    Write-Host "=== $n $text ===" -ForegroundColor Cyan
}

function Get-EnvPython($name) {
    $code = "import sys; print(sys.executable)"
    $out = & conda run -n $name python -c $code 2>$null
    if (-not $out) { throw "could not resolve the python for conda env '$name'" }
    return ($out | Select-Object -Last 1).Trim()
}

# ── 1. conda env ────────────────────────────────────────────────────────────
Write-Step "1/5" "conda environment '$EnvName'"

$envList = & conda env list
$exists = $false
foreach ($line in $envList) {
    if ($line -match "^\s*$([regex]::Escape($EnvName))\s") { $exists = $true; break }
}
if ($exists) {
    Write-Host "  already exists, reusing"
} else {
    Write-Host "  creating (python 3.12)..."
    & conda create -n $EnvName python=3.12 -y
    if ($LASTEXITCODE -ne 0) { throw "conda create failed" }
}

$Py = Get-EnvPython $EnvName
Write-Host "  python: $Py"

# The env may have been created by this script's first run; make sure pip is fresh.
& $Py -m pip install --upgrade pip --quiet

# ── 2. PyTorch ──────────────────────────────────────────────────────────────
Write-Step "2/5" "PyTorch ($Cuda)"

$torchWheel = Join-Path $TorchWheelDir "torch-$Cuda.whl"
$audioWheel = Join-Path $TorchWheelDir "torchaudio-$Cuda.whl"

if ((Test-Path $torchWheel) -and (Test-Path $audioWheel)) {
    Write-Host "  installing from local wheels in $TorchWheelDir"
    & $Py -m pip install $torchWheel $audioWheel
    if ($LASTEXITCODE -ne 0) { throw "local torch wheel install failed" }
}
elseif ($Cuda -eq "cpu") {
    Write-Host "  no local wheel; installing CPU build from $Index"
    & $Py -m pip install torch torchaudio -i $Index
    if ($LASTEXITCODE -ne 0) { throw "torch install failed" }
}
else {
    Write-Host "  no local wheel at $torchWheel"
    Write-Host "  falling back to download.pytorch.org -- this is the slow one." -ForegroundColor Yellow
    Write-Host "  To avoid it, download the wheels yourself (resumable):" -ForegroundColor Yellow
    Write-Host "    curl.exe -L -C - --retry 30 -o `"$torchWheel`" ``" -ForegroundColor DarkGray
    Write-Host "      `"https://download.pytorch.org/whl/$Cuda/torch-2.6.0%2B$Cuda-cp312-cp312-win_amd64.whl`"" -ForegroundColor DarkGray
    & $Py -m pip install torch torchaudio --index-url "https://download.pytorch.org/whl/$Cuda"
    if ($LASTEXITCODE -ne 0) { throw "torch install failed" }
}

# ── 3. remaining requirements ───────────────────────────────────────────────
Write-Step "3/5" "requirements.txt (via $Index)"
& $Py -m pip install -r requirements.txt -i $Index
if ($LASTEXITCODE -ne 0) { throw "requirements install failed" }

# ── 4. models ───────────────────────────────────────────────────────────────
Write-Step "4/5" "models"
if ($SkipModels) {
    Write-Host "  skipped (-SkipModels)"
} else {
    & $Py (Join-Path $Root "doc\migrations\prefetch_models.py")
    if ($LASTEXITCODE -ne 0) { Write-Warning "model pre-fetch reported a problem; the app will retry on first launch" }
}

# ── 5. self-check ───────────────────────────────────────────────────────────
Write-Step "5/5" "self-check"
if ($SkipVerify) {
    Write-Host "  skipped (-SkipVerify)"
} else {
    & $Py (Join-Path $Root "doc\migrations\verify_runtime.py")
    & $Py (Join-Path $Root "doc\migrations\verify_ui.py")
    & $Py -m pytest (Join-Path $Root "tests") -q
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Run the app with:"
Write-Host "  conda run -n $EnvName python main.py" -ForegroundColor DarkGray
