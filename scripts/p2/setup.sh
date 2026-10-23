#!/usr/bin/env bash
# Retinat bridge setup for macOS and Linux. Installs uv if missing, puts this repo in ~/retinat (or updates it),
# installs its packages, then writes the Chrome extension and waits for you to load it; a self-test says PASS or FAIL.
#   bash setup.sh            (extra arguments go to `python -m browser_use.bridge setup`, e.g. --port 9334)
set -euo pipefail

REPO_URL=https://github.com/MrNagabhushana-0-dev/browser-use-
BRANCH=claude/laughing-hawking-2cj3a6
DIR="$HOME/retinat"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nSETUP FAILED: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is missing. macOS: run 'xcode-select --install'. Linux: install git with your package manager."

if ! command -v uv >/dev/null 2>&1; then
	say "Installing uv (Astral's official installer)"
	command -v curl >/dev/null 2>&1 || die "curl is missing; install it and run this again"
	curl -LsSf https://astral.sh/uv/install.sh | sh || die "the uv installer failed"
	export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" # where the installer puts uv; new terminals get it on PATH
	command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH; open a new terminal and run this again"
fi

if [ -d "$DIR/.git" ]; then
	say "Updating $DIR"
	git -C "$DIR" pull --ff-only || die "git pull failed in $DIR (local changes? run 'git -C $DIR status')"
elif [ -e "$DIR" ]; then
	die "$DIR exists but is not a git checkout; move it away and run this again"
else
	say "Cloning into $DIR"
	git clone --branch "$BRANCH" "$REPO_URL" "$DIR" || die "git clone failed"
fi

cd "$DIR"
# The eyes extra (numpy, opencv, faster-whisper, ...) is what lets Retinat watch and hear: without it its MCP server
# does not start (browser_use/eyes imports numpy).
say "Installing packages (the first time takes a few minutes)"
uv sync || die "uv sync failed"
# The eyes extra is what lets Retinat see and hear. Its onnxruntime has wheels only for Apple Silicon on macOS 14+;
# elsewhere it cannot install, and Retinat then cannot start. The bridge itself still works, so say so and go on.
if ! uv sync --extra eyes; then
	say "WARNING: the eyes extra did not install on this machine (it needs Apple Silicon on macOS 14 or later, or Linux/Windows x86_64)."
	say "The bridge self-test below still runs. Retinat's watch tools will not start here until this is fixed."
fi

say "Writing the extension and waiting for Chrome"
exec uv run --no-sync python -m browser_use.bridge setup "$@"
