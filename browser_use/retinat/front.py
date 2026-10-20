"""What a client first meets of Retinat: the MCP handshake and the tool list, answered at once from light modules,
while the server proper (browser-use with its eyes, agents and LLM clients, ~2,200 modules) is imported in a worker
thread. A tool call waits for it. After a machine boots every one of those files is read off a cold disk: importing
them all before answering took the handshake to 88-93 s, against a client's 120 s to connect."""

import argparse
import asyncio
import importlib
import os
import sys
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import mcp.types as types
from mcp.server import Server

from browser_use.eyes.views import CALL_BUDGET_S
from browser_use.net import NetworkMode, NetworkRouter
from browser_use.net.tools import network_tools
from browser_use.retinat.tools import _tools
from browser_use.serving import serve_until_stopped

if TYPE_CHECKING:
	from browser_use.retinat.server import RetinatServer

# When the client's call reached the front (monotonic): the server's deadline counts from there, so a call that first
# waited for the server to load still answers inside the client's 60 s
CALL_STARTED: ContextVar[float | None] = ContextVar('retinat_call_started', default=None)
# Of a call's budget, what is kept for the call itself once the server has loaded
LOAD_RESERVE_S = 15.0


def tool_list() -> list[types.Tool]:
	"""What Retinat serves, as `RetinatServer` lists it."""
	return [*_tools(), *network_tools('retinat')]


class RetinatFront:
	"""The MCP server a client talks to. It answers the handshake and the tool list itself, and hands each tool call to
	the `RetinatServer` it loads in the background."""

	def __init__(self, cdp_url: str | None = None, network: NetworkRouter | None = None, bridge_port: int | None = None) -> None:
		from browser_use.utils import get_browser_use_version

		self.cdp_url = cdp_url
		self._network = network
		self.bridge_port = bridge_port
		self.call_budget_s = float(os.environ.get('BROWSER_USE_MCP_CALL_BUDGET_S') or CALL_BUDGET_S)
		self.server = Server('retinat', version=get_browser_use_version())
		self.inner: 'RetinatServer | None' = None
		self._loading: asyncio.Task['RetinatServer'] | None = None
		self._loading_since = 0.0
		self._setup_handlers()

	def start_loading(self) -> None:
		if self._loading is None:
			self._loading_since = time.monotonic()
			self._loading = asyncio.create_task(self._load())

	async def _load(self) -> 'RetinatServer':
		# Imported in a worker thread, so the handshake is answered meanwhile on this one
		module = await asyncio.to_thread(importlib.import_module, 'browser_use.retinat.server')
		bridge = None
		if self.bridge_port is not None:
			from browser_use.bridge import EXTENSION_DIR, BridgeRelay

			bridge = await BridgeRelay(port=self.bridge_port).start()
			print(
				f'Retinat bridge on {bridge.cdp_url}; load the extension from {EXTENSION_DIR} and share a tab.', file=sys.stderr
			)
		inner = module.RetinatServer(cdp_url=self.cdp_url, network=self._network, bridge=bridge)
		await inner._start_cleanup_task()
		self.inner = inner
		return inner

	def _setup_handlers(self) -> None:
		async def list_tools(_context: Any, _params: types.PaginatedRequestParams) -> types.ListToolsResult:
			return types.ListToolsResult(tools=tool_list())

		async def call_tool(context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
			started = time.monotonic()
			self.start_loading()
			assert self._loading is not None
			try:
				inner = await asyncio.wait_for(asyncio.shield(self._loading), max(0.1, self.call_budget_s - LOAD_RESERVE_S))
			except TimeoutError:
				text = (
					f'Retinat is still loading ({time.monotonic() - self._loading_since:.0f}s so far: after the machine '
					'boots its files are read off a cold disk). Nothing was done; call again.'
				)
				return types.CallToolResult(content=[types.TextContent(type='text', text=f'Error: {text}')], is_error=True)
			token = CALL_STARTED.set(started)
			try:
				return await inner.server.get_request_handler('tools/call').handler(context, params)  # type: ignore[union-attr]
			finally:
				CALL_STARTED.reset(token)

		async def empty_resources(_context: Any, _params: types.PaginatedRequestParams) -> types.ListResourcesResult:
			return types.ListResourcesResult(resources=[])

		async def empty_prompts(_context: Any, _params: types.PaginatedRequestParams) -> types.ListPromptsResult:
			return types.ListPromptsResult(prompts=[])

		self.server.add_request_handler('tools/list', types.PaginatedRequestParams, list_tools)
		self.server.add_request_handler('tools/call', types.CallToolRequestParams, call_tool)
		self.server.add_request_handler('resources/list', types.PaginatedRequestParams, empty_resources)
		self.server.add_request_handler('prompts/list', types.PaginatedRequestParams, empty_prompts)

	async def run(self) -> None:
		"""Serve over stdio until the client leaves; the server proper loads meanwhile."""
		from mcp.server.stdio import stdio_server

		if sys.stdin is None:
			raise RuntimeError('MCP stdio transport requires stdin, but this process was launched without one.')
		self.start_loading()
		async with stdio_server() as (read_stream, write_stream):
			try:
				await self.server.run(read_stream, write_stream, self.server.create_initialization_options())
			except BrokenPipeError:
				pass  # the client left while we wrote: closing follows

	# What `serving._close_everything` closes, from the server proper once it has loaded
	async def _close_all_sessions(self) -> str:
		if self.inner is None:
			return 'No server loaded'
		return await self.inner._close_all_sessions()

	@property
	def browser_session(self) -> Any:
		return self.inner.browser_session if self.inner is not None else None

	@property
	def bridge(self) -> Any:
		return self.inner.bridge if self.inner is not None else None

	@property
	def network(self) -> Any:
		return self.inner.network if self.inner is not None else self._network


async def main(cdp_url: str | None = None, network: NetworkRouter | None = None, bridge_port: int | None = None) -> None:
	await serve_until_stopped(RetinatFront(cdp_url=cdp_url, network=network, bridge_port=bridge_port))


def cli() -> None:
	parser = argparse.ArgumentParser(
		prog='retinat', description='Retinat MCP server: continuous image/video perception for LLMs (stdio).'
	)
	parser.add_argument(
		'--cdp-url', default=os.environ.get('RETINAT_CDP_URL'), help='attach to a Chrome you started with --remote-debugging-port'
	)
	parser.add_argument(
		'--network',
		choices=[m.value for m in NetworkMode],
		default=None,
		help='route: off (direct), auto (default: Tor only after a network/geo block), always (Tor); env BROWSER_USE_NETWORK',
	)
	parser.add_argument(
		'--exit-country', default=None, help='two-letter Tor exit country, e.g. de (env BROWSER_USE_EXIT_COUNTRY)'
	)
	parser.add_argument(
		'--bridge',
		nargs='?',
		type=int,
		const=9333,
		default=int(os.environ['RETINAT_BRIDGE']) if os.environ.get('RETINAT_BRIDGE') else None,
		metavar='PORT',
		help="use the person's own browser through the Retinat bridge extension (relay port, default 9333; env RETINAT_BRIDGE)",
	)
	args = parser.parse_args()
	network = None
	if args.network or args.exit_country:
		base = NetworkRouter.from_env(default=NetworkMode.AUTO)
		network = NetworkRouter(args.network or base.mode, args.exit_country or base.exit_country)
	asyncio.run(main(args.cdp_url, network, args.bridge))
