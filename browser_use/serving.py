"""Run an MCP server over stdio until its client leaves or a signal stops it, then close its browsers, bridge and Tor;
and the front both servers answer a client with while the rest of browser-use loads (`Front`). Light on purpose: it is
imported before the rest of browser-use is loaded."""

import asyncio
import importlib
import logging
import os
import sys
import time
from contextvars import ContextVar
from types import ModuleType
from typing import TYPE_CHECKING, Any

import mcp.types as types
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.subscriptions import ToolsListChanged
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

if TYPE_CHECKING:
	from browser_use.net import NetworkRouter

logger = logging.getLogger('browser_use.mcp.server')

# When the client's call reached the front (monotonic): the server's deadline counts from there, so a call that first
# waited for the server to load still answers inside the client's 60 s
CALL_STARTED: ContextVar[float | None] = ContextVar('front_call_started', default=None)
# Of a call's budget, what is kept for the call itself once the server has loaded
LOAD_RESERVE_S = 15.0


class Front:
	"""What a client first meets of an MCP server: the handshake and the tool list, answered at once from light modules,
	while the server proper (browser-use with its eyes, agents and LLM clients, ~2,100 modules) is imported in a worker
	thread. A tool call waits for it. After a machine boots every one of those files is read off a cold disk: importing
	them all before answering took Retinat's handshake to 88-93 s, against a client's 120 s to connect, and kept
	browser-use from connecting at all within Claude Code's 30 s.

	A subclass names the server, lists the tools it always serves, and makes the server proper from its module."""

	name = ''  # as the client knows the server
	label = ''  # as messages name it
	module = ''  # the server proper's module, imported in the background

	def __init__(self, network: 'NetworkRouter | None' = None) -> None:
		from browser_use.eyes.views import CALL_BUDGET_S
		from browser_use.utils import get_browser_use_version

		self._network = network
		self.call_budget_s = float(os.environ.get('BROWSER_USE_MCP_CALL_BUDGET_S') or CALL_BUDGET_S)
		# A client that reconnects to a restarted server may keep the tool list of its first connection (found: Claude Code
		# did, so a tool added since could not be called). Each server process tells its client once that the list
		# changed: on 2026-07-28 connections on the stream the client opens with subscriptions/listen, on older ones
		# with the first tool call.
		self._bus = InMemorySubscriptionBus()
		self.server = Server(self.name, version=get_browser_use_version(), on_subscriptions_listen=self._listen)
		self._listen_handler = ListenHandler(self._bus)
		self.inner: Any = None
		self._loading: asyncio.Task[Any] | None = None
		self._loading_since = 0.0
		self._told_tools = False  # whether this process has told its client to list the tools again
		self._notice: asyncio.Task[None] | None = None
		self._setup_handlers()

	def tool_list(self) -> list[types.Tool]:
		"""The tools served before the server proper has loaded: all it serves whatever page it is on."""
		raise NotImplementedError

	async def make_server(self, module: ModuleType) -> Any:
		"""The server proper, from its module once imported, ready to take calls."""
		raise NotImplementedError

	async def _listen(self, context: Any, params: Any) -> Any:
		"""Serve a subscriptions/listen stream; one that asks for tool changes is told the tool list changed. The handler
		subscribes the stream before it first waits, so a notice published from a task started here reaches it."""
		notifications = getattr(params, 'notifications', None)
		if getattr(notifications, 'tools_list_changed', False):
			self._told_tools = True
			self._notice = asyncio.create_task(self._bus.publish(ToolsListChanged()))
		return await self._listen_handler(context, params)

	def start_loading(self) -> None:
		# A load that failed (the bridge's port still held by the process before, say) is tried again by the next call
		if self._loading is not None and self._loading.done() and self.inner is None:
			self._loading = None
		if self._loading is None:
			self._loading_since = time.monotonic()
			self._loading = asyncio.create_task(self._load())

	async def _load(self) -> Any:
		# Imported in a worker thread, so the handshake is answered meanwhile on this one
		module = await asyncio.to_thread(importlib.import_module, self.module)
		self.inner = await self.make_server(module)
		return self.inner

	def _setup_handlers(self) -> None:
		async def list_tools(context: Any, params: types.PaginatedRequestParams) -> types.ListToolsResult:
			if self.inner is not None:  # its own list, with whatever the page in front of it adds
				return await self.inner.server.get_request_handler('tools/list').handler(context, params)
			return types.ListToolsResult(tools=self.tool_list())

		async def call_tool(context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
			started = time.monotonic()
			if not self._told_tools and context.protocol_version not in MODERN_PROTOCOL_VERSIONS:
				self._told_tools = True
				try:
					await context.session.send_tool_list_changed()
				except Exception as e:  # never at the cost of the call
					print(
						f'{self.label}: could not tell the client to list the tools again: {type(e).__name__}: {e}',
						file=sys.stderr,
					)
			self.start_loading()
			assert self._loading is not None
			try:
				inner = await asyncio.wait_for(asyncio.shield(self._loading), max(0.1, self.call_budget_s - LOAD_RESERVE_S))
			except TimeoutError:
				text = (
					f'{self.label} is still loading ({time.monotonic() - self._loading_since:.0f}s so far: after the machine '
					'boots its files are read off a cold disk). Nothing was done; call again.'
				)
				return types.CallToolResult(content=[types.TextContent(type='text', text=f'Error: {text}')], is_error=True)
			except Exception as e:
				text = f'{self.label} failed to load: {type(e).__name__}: {e}. Nothing was done; call again to load it again.'
				return types.CallToolResult(content=[types.TextContent(type='text', text=f'Error: {text}')], is_error=True)
			token = CALL_STARTED.set(started)
			try:
				return await inner.server.get_request_handler('tools/call').handler(context, params)
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
				options = self.server.create_initialization_options(NotificationOptions(tools_changed=True))
				await self.server.run(read_stream, write_stream, options)
			except BrokenPipeError:
				pass  # the client left while we wrote: closing follows

	# What `_close_everything` closes, from the server proper once it has loaded
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


async def serve_until_stopped(server: Any) -> None:
	"""Run an MCP server over stdio until the client leaves or a signal (SIGTERM, SIGHUP, SIGINT) stops it, then close
	its browsers, bridge and Tor. Without this a stopped server left its Chromium running with no one to close it.

	On a signal the process exits from here once that is done: the stdio transport reads stdin in a worker thread that
	no cancellation reaches while the client keeps the pipe open, so the server would otherwise close its browser and
	then hang for good (both found hands-on)."""
	import signal

	loop = asyncio.get_running_loop()
	stop = asyncio.Event()
	for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
		try:
			loop.add_signal_handler(sig, stop.set)
		except (NotImplementedError, RuntimeError, ValueError):
			pass  # Windows, or not the main thread: the client closing stdin still ends run()
	running = asyncio.create_task(server.run())
	stopping = asyncio.create_task(stop.wait())
	try:
		await asyncio.wait({running, stopping}, return_when=asyncio.FIRST_COMPLETED)
	finally:
		stopping.cancel()
		await _close_everything(server)
	if stop.is_set():
		_log_stopped_by_signal()
		os._exit(0)
	if not running.cancelled():
		running.result()


async def _close_everything(server: Any) -> None:
	"""Each part gets its own bounded try, so one that hangs or fails does not keep the rest running."""
	closers: list[tuple[str, Any]] = [('sessions', server._close_all_sessions)]
	session = getattr(server, 'browser_session', None)
	if session is not None:
		closers.append(('browser', session.kill))
	for name in ('bridge', 'network'):
		part = getattr(server, name, None)
		close = getattr(part, 'stop', None) or getattr(part, 'close', None)
		if close is not None:
			closers.append((name, close))
	for name, close in closers:
		try:
			await asyncio.wait_for(close(), timeout=15)
		except Exception as e:
			logger.debug(f'closing the {name} on exit: {type(e).__name__}: {e}')


def _log_stopped_by_signal() -> None:
	logger.info('Stopped by a signal; browsers, bridge and Tor closed.')
	for stream in (sys.stdout, sys.stderr):
		try:
			stream.flush()
		except Exception:
			pass
