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
    Directory to look for pre-downloaded torch wheels. Default: .pip-cache\wheels
    The wheel must keep its upstream filename, e.g.
        torch-2.6.0+cu124-cp312-cp312-win_amd64.whl
        torchaudio-2.6.0+cu124-cp312-cp312-win_amd64.whl
    pip validates wheel filenames (PEP 427), so a renamed file such as
    "torch-cu124.whl" is rejected with "Invalid wheel filename".

.PARAMETER TorchWheelPath
    Explicit path to the torch wheel, bypassing the directory search.

.PARAMETER TorchAudioWheelPath
    Explicit path to the torchaudio wheel, bypassing the directory search.

.PARAMETER SkipModels
    Skip pre-fetching the Silero VAD + SenseVoice models (~1 GB).

.PARAMETER SkipVerify
    Skip the runtime self-check at the end.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup-conda.ps1

.EXAMPLE
    # Use a wheel you downloaded elsewhere, e.g. straight into Downloads
    powershell -ExecutionPolicy Bypass -File .\setup-conda.ps1 `
        -TorchWheelPath "$env:USERPROFILE\Downloads\torch-2.6.0+cu124-cp312-cp312-win_amd64.whl"

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
    [string]$TorchWheelPath = "",
    [string]$TorchAudioWheelPath = "",
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

# Find a pre-downloaded wheel by its upstream name, e.g.
#   torch-2.6.0+cu124-cp312-cp312-win_amd64.whl
# The full PEP 427 name matters: pip parses it, and rejects anything it cannot
# split into name-version-python-abi-platform ("Invalid wheel filename").
function Find-Wheel($dir, $prefix, $cuda, $explicit) {
    if (-not [string]::IsNullOrWhiteSpace($explicit)) {
        if (-not (Test-Path -LiteralPath $explicit)) { throw "wheel not found: $explicit" }
        return (Resolve-Path -LiteralPath $explicit).Path
    }
    $pattern = Join-Path $dir "$prefix-*-$cuda-*.whl"
    $hit = Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { return $hit.FullName }

    # A renamed wheel is a common trap -- report it specifically instead of
    # silently falling through to the slow network download.
    $shortName = Join-Path $dir "$prefix-$cuda.whl"
    if (Test-Path -LiteralPath $shortName) {
        throw ("found '$shortName', but pip rejects renamed wheels. Rename it back " +
               "to its upstream name (e.g. $prefix-2.6.0+$cuda-cp312-cp312-win_amd64.whl).")
    }
    return ""
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

$torchWheel = Find-Wheel $TorchWheelDir "torch" $Cuda $TorchWheelPath
$audioWheel = Find-Wheel $TorchWheelDir "torchaudio" $Cuda $TorchAudioWheelPath

if ($torchWheel -and $audioWheel) {
    Write-Host "  installing from local wheels:"
    Write-Host "    $torchWheel" -ForegroundColor DarkGray
    Write-Host "    $audioWheel" -ForegroundColor DarkGray
    # --no-deps: the CUDA wheels only need pure-python deps already covered by
    # requirements.txt, and letting pip resolve them would try the slow index.
    & $Py -m pip install --no-deps $torchWheel $audioWheel
    if ($LASTEXITCODE -ne 0) { throw "local torch wheel install failed" }
}
elseif ($Cuda -eq "cpu") {
    Write-Host "  no local wheel; installing CPU build from $Index"
    & $Py -m pip install torch torchaudio -i $Index
    if ($LASTEXITCODE -ne 0) { throw "torch install failed" }
}
else {
    Write-Host "  no local wheel found in $TorchWheelDir" -ForegroundColor Yellow
    Write-Host "  falling back to download.pytorch.org -- this is the slow one." -ForegroundColor Yellow
    Write-Host "  Recommended: download the wheel yourself first (resumable), then re-run" -ForegroundColor Yellow
    Write-Host "  this script with -TorchWheelPath. Expected sha256 for cu124/cp312:" -ForegroundColor Yellow
    Write-Host "    3313061c1fec4c7310cf47944e84513dcd27b6173b72a349bb7ca68d0ee6e9c0" -ForegroundColor DarkGray
    Write-Host "  Verify a download with:  Get-FileHash <wheel> -Algorithm SHA256" -ForegroundColor DarkGray
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
    & $Py (Join-Path $Root "tools\prefetch_models.py")
    if ($LASTEXITCODE -ne 0) { Write-Warning "model pre-fetch reported a problem; the app will retry on first launch" }
}

# ── 5. self-check ───────────────────────────────────────────────────────────
Write-Step "5/5" "self-check"
if ($SkipVerify) {
    Write-Host "  skipped (-SkipVerify)"
} else {
    & $Py (Join-Path $Root "tools\verify_imports.py")
    & $Py (Join-Path $Root "tools\verify_runtime.py")
    & $Py (Join-Path $Root "tools\verify_ui.py")
    & $Py -m pytest (Join-Path $Root "tests") -q
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Run the app with:"
Write-Host "  conda run -n $EnvName python main.py" -ForegroundColor DarkGray
