#!/usr/bin/env bash
# Start one of this checkout's MCP servers over stdio, as .mcp.json does.
# Usage:
#   $ ./bin/mcp-serve.sh browser_use.retinat [args...]
#
# With the project installed, uv's sync check is skipped (--no-sync): after a machine boots it read every package's
# metadata off a cold disk, ~54 s of a client's 120 s to connect. A virtualenv the project is not installed in (a fresh
# container) is installed first instead; --no-sync there ran the server in an empty virtualenv, where it failed to import.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$SCRIPT_DIR/.." || exit 1

for installed in .venv/lib/python3*/site-packages/browser_use-*.dist-info; do
	if [ -e "$installed" ]; then
		exec uv run --no-sync python -m "$@"
	fi
done
exec uv run python -m "$@"
