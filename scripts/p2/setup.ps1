# Retinat bridge setup for Windows (PowerShell). Installs uv if missing, puts this repo in ~\retinat (or updates it),
# installs its packages, then writes the Chrome extension and waits for you to load it; a self-test says PASS or FAIL.
#   powershell -ExecutionPolicy Bypass -File setup.ps1     (extra arguments go to the setup command, e.g. --port 9334)
$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/MrNagabhushana-0-dev/browser-use-'
$Branch = 'claude/laughing-hawking-2cj3a6'
$Dir = Join-Path $HOME 'retinat'

function Say([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Die([string]$Message) { Write-Host "`nSETUP FAILED: $Message" -ForegroundColor Red; exit 1 }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
	Die 'git is missing. Install it (winget install --id Git.Git -e), open a new PowerShell and run this again.'
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
	Say "Installing uv (Astral's official installer)"
	powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
	if ($LASTEXITCODE -ne 0) { Die 'the uv installer failed' }
	$env:Path = "$HOME\.local\bin;$HOME\.cargo\bin;$env:Path"  # where the installer puts uv; new windows get it on PATH
	if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv was installed but is not on PATH; open a new PowerShell and run this again' }
}

if (Test-Path (Join-Path $Dir '.git')) {
	Say "Updating $Dir"
	git -C $Dir pull --ff-only
	if ($LASTEXITCODE -ne 0) { Die "git pull failed in $Dir (local changes? run: git -C $Dir status)" }
} elseif (Test-Path $Dir) {
	Die "$Dir exists but is not a git checkout; move it away and run this again"
} else {
	Say "Cloning into $Dir"
	git clone --branch $Branch $RepoUrl $Dir
	if ($LASTEXITCODE -ne 0) { Die 'git clone failed' }
}

Set-Location $Dir
# The eyes extra (numpy, opencv, faster-whisper, ...) is what lets Retinat watch and hear: without it its MCP server
# does not start (browser_use/eyes imports numpy).
Say 'Installing packages (uv sync --extra eyes; the first time takes a few minutes)'
uv sync --extra eyes
if ($LASTEXITCODE -ne 0) { Die 'uv sync failed' }

Say 'Writing the extension and waiting for Chrome'
uv run --no-sync python -m browser_use.bridge setup @args
exit $LASTEXITCODE
