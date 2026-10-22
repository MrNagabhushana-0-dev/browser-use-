"""Entry point for running MCP server as a module.

Usage:
    python -m browser_use.mcp
"""

from browser_use.mcp.front import cli

if __name__ == '__main__':
	cli()
