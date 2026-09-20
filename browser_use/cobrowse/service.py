"""Share one browser between a person and the agent.

The problem this solves is authentication. Google, Instagram and most of the interesting
web will not let a fresh automated profile in: no session, a datacenter IP, and a login
flow that escalates to SMS or a device prompt the moment it sees one. Scripting around
that is a losing game and usually a terms-of-service violation besides.

The way out is to not automate the login at all. A person opens a browser, signs in as
themselves, and hands the same live browser to the agent — same profile, same cookies,
same IP, same tab. The agent then works through the UI exactly as the person would, and
the site sees one continuous session because that is what it is.

Mechanically this is just CDP: Chrome launched with a remote debugging port and a
persistent profile directory, and a BrowserSession attached to it rather than spawning
its own. The care is all in not disturbing what the person left behind — no new profile,
no blank tab stealing focus, no closing their windows on exit.
"""

import asyncio
import logging
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

# How long to wait for Chrome to write its DevToolsActivePort file.
LAUNCH_TIMEOUT_S = 30.0

# Targets that are never "the tab the person is looking at".
_IGNORED_URL_PREFIXES = ('devtools://', 'chrome-extension://', 'chrome://', 'about:blank')


def _free_port() -> int:
	with socket.socket() as s:
		s.bind(('127.0.0.1', 0))
		return int(s.getsockname()[1])


def find_chrome() -> str:
	"""Locate a Chrome/Chromium binary, reusing browser-use's own search."""
	from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog

	path = LocalBrowserWatchdog._find_installed_browser_path()
	if path:
		return path
	for name in ('google-chrome', 'chromium', 'chromium-browser', 'chrome'):
		if found := shutil.which(name):
			return found
	raise RuntimeError('No Chrome/Chromium binary found. Pass executable_path explicitly.')


@dataclass
class HumanBrowser:
	"""A browser a person is driving, which the agent can attach to."""

	cdp_url: str
	port: int
	user_data_dir: Path
	process: asyncio.subprocess.Process | None = field(default=None, repr=False)

	async def close(self) -> None:
		"""Shut the browser down cleanly. Only call this for a browser you launched.

		The CDP `Browser.close` matters and a signal is not a substitute for it: Chrome
		batches cookie writes and only commits them to the profile on its normal shutdown
		path. Killed with SIGTERM it leaves a `Cookies-journal` behind and the session the
		person signed into is gone on the next launch — while localStorage, which flushes
		eagerly, survives and makes the loss look like a site bug rather than a lost cookie.
		"""
		if not self.process or self.process.returncode is not None:
			return

		try:
			await asyncio.wait_for(self._request_clean_shutdown(), timeout=10.0)
			await asyncio.wait_for(self.process.wait(), timeout=10.0)
			return
		except Exception as e:
			logger.debug(f'Clean shutdown failed, falling back to a signal: {type(e).__name__}: {e}')

		self.process.terminate()
		try:
			await asyncio.wait_for(self.process.wait(), timeout=10.0)
		except Exception:
			self.process.kill()

	async def _request_clean_shutdown(self) -> None:
		"""Ask Chrome to close itself the way clicking the X would."""
		from cdp_use import CDPClient

		client = CDPClient(self.cdp_url)
		await client.start()
		try:
			await client.send.Browser.close()
		finally:
			try:
				await client.stop()
			except Exception:
				pass


async def launch_for_human(
	user_data_dir: Path | str,
	port: int | None = None,
	headless: bool = False,
	executable_path: str | None = None,
	extra_args: list[str] | None = None,
) -> HumanBrowser:
	"""Start a browser for a person to use and later hand over.

	The profile directory is persistent on purpose: it is what carries the login across
	restarts, so the handover survives closing the laptop. Headful by default, because the
	whole point is that a human sits in front of it.
	"""
	user_data_dir = Path(user_data_dir).expanduser()
	user_data_dir.mkdir(parents=True, exist_ok=True)
	port = port or _free_port()

	args = [
		executable_path or find_chrome(),
		f'--remote-debugging-port={port}',
		f'--user-data-dir={user_data_dir}',
		# Bind to loopback only. A debugging port is unauthenticated: anything that can
		# reach it can read every cookie in the profile.
		'--remote-debugging-address=127.0.0.1',
		'--no-first-run',
		'--no-default-browser-check',
	]
	if headless:
		args.append('--headless=new')

	# Chrome refuses to start its sandbox as root, which is the normal case inside a
	# container. Matches what BrowserProfile already does for the same reason.
	from browser_use.config import CONFIG

	if CONFIG.IN_DOCKER:
		args.extend(['--no-sandbox', '--disable-dev-shm-usage'])

	args.extend(extra_args or [])

	logger.info(f'🧑‍💻 Launching a browser for you to sign in with, profile at {user_data_dir}')
	process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

	cdp_url = await _wait_for_cdp(port, process)
	return HumanBrowser(cdp_url=cdp_url, port=port, user_data_dir=user_data_dir, process=process)


async def _wait_for_cdp(port: int, process: 'asyncio.subprocess.Process | None' = None) -> str:
	"""Poll the debugging endpoint until Chrome answers, then return its websocket URL."""
	deadline = asyncio.get_running_loop().time() + LAUNCH_TIMEOUT_S
	last_error: Exception | None = None
	async with httpx.AsyncClient(timeout=2.0) as client:
		while asyncio.get_running_loop().time() < deadline:
			if process is not None and process.returncode is not None:
				raise RuntimeError(f'Browser exited before its debugging port opened (code {process.returncode})')
			try:
				response = await client.get(f'http://127.0.0.1:{port}/json/version')
				if response.status_code == 200:
					return str(response.json()['webSocketDebuggerUrl'])
			except Exception as e:
				last_error = e
			await asyncio.sleep(0.25)
	raise TimeoutError(f'Browser never opened a debugging port on {port}: {last_error}')


async def cdp_url_for(port: int) -> str:
	"""The websocket URL for a browser already listening on `port`."""
	return await _wait_for_cdp(port)


async def attach(cdp_url: str, **session_kwargs) -> 'BrowserSession':
	"""Attach an agent session to a browser someone else is driving.

	Deliberately does not open a tab, create a profile, or change what is on screen. The
	session lands on whatever the person left in front of them.
	"""
	from browser_use.browser.session import BrowserSession

	session = BrowserSession(cdp_url=cdp_url, is_local=False, **session_kwargs)  # type: ignore[call-overload]
	await session.start()
	logger.info(f'🤝 Attached to the browser at {cdp_url}')
	return session


async def focus_human_tab(session: 'BrowserSession', url_contains: str | None = None) -> str | None:
	"""Point the agent at the tab the person was actually using.

	Chrome exposes no "active tab" flag over CDP, so this takes the most recently attached
	real page — blank tabs, devtools and extension pages are never it — optionally
	narrowed by a substring of the URL when several could match.
	"""
	targets = session.session_manager.get_all_page_targets()
	candidates = [
		t
		for t in targets
		if not str(getattr(t, 'url', '')).startswith(_IGNORED_URL_PREFIXES)
		and (url_contains is None or url_contains in str(getattr(t, 'url', '')))
	]
	if not candidates:
		logger.warning('🤝 No human tab to take over; the browser has only blank or internal pages')
		return None

	chosen = candidates[-1]
	await session.get_or_create_cdp_session(target_id=chosen.target_id, focus=True)
	logger.info(f'🤝 Took over the tab at {getattr(chosen, "url", "")[:80]}')
	return chosen.target_id


async def describe_session(session: 'BrowserSession') -> dict:
	"""What the agent inherited: tabs, and whether it looks signed in anywhere."""
	targets = session.session_manager.get_all_page_targets()
	cookies = []
	# Cookie names only. The values are the person's live credentials and have no
	# business being logged, returned to a model, or written to a trace.
	try:
		result = await session.cdp_client.send.Storage.getCookies()
		cookies = sorted({c['name'] for c in result.get('cookies', [])})
	except Exception as e:
		logger.debug(f'Could not read cookie names: {type(e).__name__}: {e}')
	return {
		'tabs': [{'url': str(getattr(t, 'url', ''))[:200], 'target_id': t.target_id} for t in targets],
		'cookie_names': cookies[:50],
		'cookie_count': len(cookies),
	}
