"""Run an MCP server over stdio until its client leaves or a signal stops it, then close its browsers, bridge and Tor.
Light on purpose: Retinat's front (`retinat/front.py`) serves with it before the rest of browser-use is loaded."""

import asyncio
import logging
import os
import sys
from typing import Any

logger = logging.getLogger('browser_use.mcp.server')


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
