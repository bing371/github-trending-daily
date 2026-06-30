# ============================================================================
#  GitHub Trending Daily — uninstaller (Windows)
# ============================================================================
#  Removes scheduled tasks, desktop shortcut, and (optionally) the
#  install directory. Run with -KeepData to preserve your data/pages/log.
# ============================================================================

[CmdletBinding()]
param(
    [switch]$KeepData    # don't delete the install folder (just remove tasks + shortcut)
)

$installDir = Join-Path $env:LOCALAPPDATA "GitHubTrending"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  GitHub Trending Daily — uninstaller" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Remove scheduled tasks
$tasks = @(
    "GitHubTrending-Main",
    "GitHubTrending-Check-08",
    "GitHubTrending-Check-12",
    "GitHubTrending-Check-16",
    "GitHubTrending-Check-18"
)
foreach ($t in $tasks) {
    $exists = schtasks /query /tn $t 2>$null
    if ($exists) {
        schtasks /delete /tn $t /f | Out-Null
        Write-Host "  [OK] Removed task: $t" -ForegroundColor Green
    }
}

# 2. Remove desktop shortcut
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcut = Join-Path $desktop "今日 GitHub 热榜.lnk"
if (Test-Path $shortcut) {
    Remove-Item $shortcut -Force
    Write-Host "  [OK] Removed desktop shortcut" -ForegroundColor Green
}

# 3. Remove install dir
if (-not $KeepData -and (Test-Path $installDir)) {
    Remove-Item $installDir -Recurse -Force
    Write-Host "  [OK] Removed install dir: $installDir" -ForegroundColor Green
} elseif (Test-Path $installDir) {
    Write-Host "  [..] Kept install dir: $installDir  (use -KeepData $false to remove)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. No traces left behind." -ForegroundColor Green
Write-Host ""