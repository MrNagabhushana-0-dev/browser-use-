"""Minimal real stdio MCP server used by tests/ci/test_mcp_client_reconnect.py.

Not a mock: this is a genuine MCP server process talking real stdio JSON-RPC,
exercised via a real subprocess launch through MCPClient.connect().
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer('echo-server')


@mcp.tool()
def echo(text: str) -> str:
	"""Echo the given text back."""
	return f'echo: {text}'


if __name__ == '__main__':
	mcp.run(transport='stdio')
