"""What a client first meets of `python -m browser_use.mcp`: the MCP handshake and the tool list, answered at once from
light modules, while the server proper is imported in a worker thread (see `browser_use.serving.Front`)."""

import asyncio
import os
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import mcp.types as types

from browser_use.mcp.tools import browser_tools
from browser_use.serving import Front, serve_until_stopped

if TYPE_CHECKING:
	from browser_use.mcp.server import BrowserUseServer


class BrowserUseFront(Front):
	"""The MCP server a client talks to. It answers the handshake and the tool list itself, and hands each tool call to
	the `BrowserUseServer` it loads in the background."""

	name = 'browser-use'
	label = 'browser-use'
	module = 'browser_use.mcp.server'
	inner: 'BrowserUseServer | None'

	def __init__(self, session_timeout_minutes: int = 10) -> None:
		self.session_timeout_minutes = session_timeout_minutes
		super().__init__()

	def tool_list(self) -> list[types.Tool]:
		return browser_tools()

	async def make_server(self, module: ModuleType) -> 'BrowserUseServer':
		inner = module.BrowserUseServer(session_timeout_minutes=self.session_timeout_minutes)
		if os.environ.get('BROWSER_USE_BRIDGE'):
			from browser_use.bridge import EXTENSION_DIR, BridgeRelay

			inner.bridge = await BridgeRelay(port=int(os.environ['BROWSER_USE_BRIDGE'])).start()
			inner.cdp_url = inner.bridge.cdp_url
			print(f'Bridge on {inner.cdp_url}; load the extension from {EXTENSION_DIR} and share a tab.', file=sys.stderr)
		inner._capture_started()
		await inner._start_cleanup_task()
		return inner


async def main(session_timeout_minutes: int = 10) -> None:
	front = BrowserUseFront(session_timeout_minutes=session_timeout_minutes)
	try:
		await serve_until_stopped(front)
	finally:
		if front.inner is not None:
			front.inner._capture_stopped()


def cli() -> None:
	asyncio.run(main())
