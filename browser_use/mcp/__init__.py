"""MCP (Model Context Protocol) support for browser-use.

This module provides integration with MCP servers and clients for browser automation.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.mcp.client import MCPClient
	from browser_use.mcp.controller import MCPToolWrapper
	from browser_use.mcp.server import BrowserUseServer

__all__ = ['MCPClient', 'MCPToolWrapper', 'BrowserUseServer']


def __getattr__(name):
	"""Lazy imports: `python -m browser_use.mcp` runs this file before its front answers the client, and the client,
	the tool wrapper and the server each load most of browser-use."""
	if name == 'MCPClient':
		from browser_use.mcp.client import MCPClient

		return MCPClient
	if name == 'MCPToolWrapper':
		from browser_use.mcp.controller import MCPToolWrapper

		return MCPToolWrapper
	if name == 'BrowserUseServer':
		from browser_use.mcp.server import BrowserUseServer

		return BrowserUseServer
	raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
