# ============================================================================
#  GitHub Trending Daily — interactive installer (Windows)
# ============================================================================
#  What this does:
#    1. Copies the project to %LOCALAPPDATA%\GitHubTrending\
#    2. Writes .env (asks you for Baidu + agent API keys)
#    3. Generates user_profile.md from the .example template
#    4. Registers 5 Windows scheduled tasks (21:00 main + 08/12/16/18 watchdogs)
#    5. Creates desktop shortcut "Today's GitHub Trending"
#    6. Optionally: run a smoke test fetch right now
#
#  Run:  powershell -ExecutionPolicy Bypass -File installer\install.ps1
#
#  Re-run safely: it will overwrite .env / tasks / shortcut with current values.
# ============================================================================

[CmdletBinding()]
param(
    [switch]$SkipFetch,    # skip the smoke test at the end
    [switch]$Unattended    # don't prompt (use defaults / .env.example values)
)

$ErrorActionPreference = 'Stop'

# --- 0. Sanity checks -------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  GitHub Trending Daily — installer" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
    Write-Host "[!] Python not found in PATH. Install Python 3.9+ from https://python.org" -ForegroundColor Red
    Write-Host "    During install, tick 'Add Python to PATH'." -ForegroundColor Red
    exit 1
}
$pyVer = & python --version 2>&1
Write-Host "[OK] Found $pyVer" -ForegroundColor Green

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Write-Host "[OK] Source: $repoRoot" -ForegroundColor Green

# --- 1. Pick install location ----------------------------------------------
$defaultDir = Join-Path $env:LOCALAPPDATA "GitHubTrending"
if (-not $Unattended) {
    $ans = Read-Host "Install to [$defaultDir] (Enter to accept, or type a path)"
    if ($ans) { $defaultDir = $ans }
}
$installDir = $defaultDir
Write-Host ""
Write-Host "[*] Installing to: $installDir" -ForegroundColor Yellow

# Copy tree (skip the things we never want in the install dir)
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$exclude = @(
    '__pycache__', '.git', 'node_modules', '_node_old',
    'log', 'cache', 'data', 'pages', 'index.html',
    '*.log', 'screenshot-*.png', '.env'
)
robocopy $repoRoot $installDir /MIR /XD __pycache__ log cache data pages /XF *.log screenshot-*.png .env | Out-Null
Write-Host "[OK] Files copied." -ForegroundColor Green

# --- 2. Write .env ----------------------------------------------------------
$envPath = Join-Path $installDir ".env"
$envExample = Join-Path $repoRoot ".env.example"

if (-not $Unattended) {
    Write-Host ""
    Write-Host "--- Translation (Baidu Fanyi) ---" -ForegroundColor Cyan
    Write-Host "Apply at https://api.fanyi.baidu.com/ (free tier is enough)" -ForegroundColor Gray
    $baiduAppid = Read-Host "  Baidu APPID"
    $baiduKey   = Read-Host "  Baidu Translate Key"

    Write-Host ""
    Write-Host "--- Agent API (any OpenAI-compatible) ---" -ForegroundColor Cyan
    Write-Host "Default: minimax (https://api.minimaxi.com/)" -ForegroundColor Gray
    Write-Host "Other options: deepseek / moonshot / zhipu / qwen / openai / ollama / openrouter" -ForegroundColor Gray
    $provider = Read-Host "  Provider [minimax]"
    if (-not $provider) { $provider = "minimax" }
    $apiBase  = Read-Host "  API Base URL [https://api.minimaxi.com/v1]"
    if (-not $apiBase) {
        $preset = @{ minimax="https://api.minimaxi.com/v1"; deepseek="https://api.deepseek.com/v1"; moonshot="https://api.moonshot.cn/v1"; zhipu="https://open.bigmodel.cn/api/paas/v4"; qwen="https://dashscope.aliyuncs.com/compatible-mode/v1"; openai="https://api.openai.com/v1" }
        if ($preset.ContainsKey($provider)) { $apiBase = $preset[$provider] } else { $apiBase = "https://api.minimaxi.com/v1" }
    }
    $model = Read-Host "  Model name [MiniMax-M3]"
    if (-not $model) { $model = "MiniMax-M3" }
    $apiKey = Read-Host "  API Key"

    # Build .env from template
    $envContent = Get-Content $envExample -Raw
    $envContent = $envContent -replace 'BAIDU_APPID=.*', "BAIDU_APPID=$baiduAppid"
    $envContent = $envContent -replace 'BAIDU_TRANSLATE_KEY=.*', "BAIDU_TRANSLATE_KEY=$baiduKey"
    $envContent = $envContent -replace 'AGENT_PROVIDER=.*', "AGENT_PROVIDER=$provider"
    $envContent = $envContent -replace 'AGENT_API_BASE=.*', "AGENT_API_BASE=$apiBase"
    $envContent = $envContent -replace 'AGENT_MODEL=.*', "AGENT_MODEL=$model"
    $envContent = $envContent -replace 'AGENT_API_KEY=.*', "AGENT_API_KEY=$apiKey"
    $envContent = $envContent -replace 'minimax_API_KEY=.*', "minimax_API_KEY=$apiKey"
    Set-Content -Path $envPath -Value $envContent -Encoding UTF8 -NoNewline
    Write-Host "[OK] .env written." -ForegroundColor Green
} else {
    if (Test-Path $envPath) {
        Write-Host "[OK] .env already exists, keeping current values." -ForegroundColor Green
    } else {
        Copy-Item $envExample $envPath
        Write-Host "[OK] .env created from template (fill in your keys before first run)." -ForegroundColor Yellow
    }
}

# --- 3. Generate user_profile.md from .example ------------------------------
$profilePath = Join-Path $installDir "user_profile.md"
if (-not (Test-Path $profilePath)) {
    $profileTemplate = Join-Path $repoRoot "user_profile.md.example"
    if (Test-Path $profileTemplate) {
        Copy-Item $profileTemplate $profilePath
        Write-Host "[OK] user_profile.md created (edit it later to personalize)." -ForegroundColor Green
    }
}

# --- 4. Install Python deps --------------------------------------------------
Write-Host ""
Write-Host "[*] Installing Python dependencies..." -ForegroundColor Yellow
& python -m pip install -q -r (Join-Path $installDir "requirements.txt")
Write-Host "[OK] Dependencies installed." -ForegroundColor Green

# --- 5. Register scheduled tasks --------------------------------------------
Write-Host ""
Write-Host "[*] Registering scheduled tasks..." -ForegroundColor Yellow

$taskScript = @"
@echo off
setlocal
set GITHUB_TRENDING_INSECURE=1
set PYTHONIOENCODING=utf-8
cd /d "$installDir"
python run.py %*
"@
$runBat = Join-Path $installDir "run.bat"
Set-Content -Path $runBat -Value $taskScript -Encoding ASCII

$checkBat = Join-Path $installDir "check.bat"
$checkScript = @"
@echo off
setlocal
set GITHUB_TRENDING_INSECURE=1
set PYTHONIOENCODING=utf-8
cd /d "$installDir"
python run.py --check
"@
Set-Content -Path $checkBat -Value $checkScript -Encoding ASCII

$tasks = @(
    @{ Name = "GitHubTrending-Main";  Time = "21:00"; Script = $runBat   }
    @{ Name = "GitHubTrending-Check-08"; Time = "08:00"; Script = $checkBat }
    @{ Name = "GitHubTrending-Check-12"; Time = "12:00"; Script = $checkBat }
    @{ Name = "GitHubTrending-Check-16"; Time = "16:00"; Script = $checkBat }
    @{ Name = "GitHubTrending-Check-18"; Time = "18:00"; Script = $checkBat }
)

foreach ($t in $tasks) {
    $exists = schtasks /query /tn $t.Name 2>$null
    if ($exists) { schtasks /delete /tn $t.Name /f | Out-Null }

    schtasks /create /tn $t.Name `
        /tr "`"$t.Script`"" `
        /sc daily /st $t.Time `
        /ru SYSTEM /rl HIGHEST /f | Out-Null

    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] $($t.Name) at $($t.Time)" -ForegroundColor Green
    } else {
        Write-Host "  [!] Failed: $($t.Name)" -ForegroundColor Red
    }
}

# --- 6. Desktop shortcut ----------------------------------------------------
$todayHtml = Join-Path $installDir ("pages\{0:yyyy}\{0:MM}\{0:yyyy-MM-dd}.html" -f (Get-Date))
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "今日 GitHub 热榜.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($shortcutPath)
$sc.TargetPath = "cmd.exe"
$sc.Arguments = "/c start """" `"$todayHtml`""
$sc.WorkingDirectory = $installDir
$sc.IconLocation = "shell32.dll,13"
$sc.Description = "Open today's GitHub trending digest"
$sc.Save()
Write-Host "[OK] Desktop shortcut created." -ForegroundColor Green

# --- 7. Smoke test ----------------------------------------------------------
Write-Host ""
if (-not $SkipFetch -and -not $Unattended) {
    $ans = Read-Host "Run a smoke test fetch now? [Y/n]"
    if ($ans -eq '' -or $ans -match '^[Yy]') {
        Write-Host ""
        Write-Host "[*] Running smoke test (this may take ~30 seconds)..." -ForegroundColor Yellow
        & $runBat
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[OK] Smoke test passed." -ForegroundColor Green
        } else {
            Write-Host "[!] Smoke test failed — check log\daily.log" -ForegroundColor Yellow
        }
    }
}

# --- Done -------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Install complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Installed to: $installDir"
Write-Host "Daily report runs at 21:00 (watchdogs at 08/12/16/18)."
Write-Host "Edit settings anytime: open today's HTML -> top-right gear icon."
Write-Host ""
Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File $installDir\installer\uninstall.ps1"
Write-Host ""