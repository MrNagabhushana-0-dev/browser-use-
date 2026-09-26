"""CrashWatchdog health check must probe the agent's focus tab, not a stray new-tab page.

`_check_browser_health()` grabs the focus tab's CDP session, then walks every page target to
redirect leftover `chrome://new-tab-page/` tabs to about:blank. That redirect loop reuses the
same `cdp_session` variable, so the "quick ping" after the loop runs against whichever tab was
redirected last. A hung focus tab therefore looks healthy as long as one stray new-tab page is
open next to it.
"""

import asyncio
import logging
from contextlib import contextmanager

import pytest

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.browser.watchdogs.crash_watchdog import CrashWatchdog

# Chrome cannot use its sandbox as root in a container; nothing else here is CI-specific.
CONTAINER_ARGS = ['--no-sandbox', '--disable-dev-shm-usage']

# Blocks the renderer's main thread forever, which is what a hung/crashed tab looks like over CDP.
HANG_RENDERER_JS = 'setTimeout(function(){while(true){}},0)'


class _RecordingHandler(logging.Handler):
	"""Real logging handler (no mocks) so the test can read what the watchdog reported."""

	def __init__(self) -> None:
		super().__init__(level=logging.DEBUG)
		self.messages: list[str] = []

	def emit(self, record: logging.LogRecord) -> None:
		self.messages.append(record.getMessage())


@contextmanager
def _capture_browser_use_logs(handler: logging.Handler):
	"""Capture every browser_use log record.

	BrowserSession.logger is rebuilt per focus target (its name embeds the focused target id),
	so attaching to one session logger would miss anything logged after a focus switch.
	"""
	root = logging.getLogger('browser_use')
	previous_level = root.level
	root.addHandler(handler)
	root.setLevel(logging.DEBUG)
	try:
		yield
	finally:
		root.removeHandler(handler)
		root.setLevel(previous_level)


@pytest.fixture
async def browser_session():
	session = BrowserSession(
		browser_profile=BrowserProfile(headless=True, args=CONTAINER_ARGS, user_data_dir=None),
	)
	await session.start()
	yield session
	# The test deliberately wedges a renderer, so a graceful close can block: kill outright.
	await session.kill()


async def _wait_for_target_url(session: BrowserSession, url: str, timeout: float = 10.0) -> str:
	"""Wait until SessionManager knows about a page target sitting on `url`, return its id."""
	deadline = asyncio.get_event_loop().time() + timeout
	while asyncio.get_event_loop().time() < deadline:
		for target in session.session_manager.get_all_page_targets():
			if target.url == url:
				return target.target_id
		await asyncio.sleep(0.2)
	raise AssertionError(f'no page target with url {url!r} appeared within {timeout}s')


async def test_health_check_detects_hung_focus_tab_despite_background_new_tab_page(browser_session: BrowserSession):
	focus_target_id = browser_session.agent_focus_target_id
	assert focus_target_id is not None

	# A leftover new-tab page sitting in the background, exactly what the redirect loop targets.
	await browser_session.cdp_client.send.Target.createTarget(params={'url': 'chrome://new-tab-page/'})
	new_tab_target_id = await _wait_for_target_url(browser_session, 'chrome://new-tab-page/')
	assert new_tab_target_id != focus_target_id

	# Creating the tab may move focus; the agent is still working in its own tab.
	browser_session.agent_focus_target_id = focus_target_id

	# Wedge the focus tab's renderer.
	focus_session = await browser_session.get_or_create_cdp_session(target_id=focus_target_id, focus=True)
	await focus_session.cdp_client.send.Runtime.evaluate(
		params={'expression': HANG_RENDERER_JS}, session_id=focus_session.session_id
	)
	await asyncio.sleep(0.5)

	# Sanity check: the focus tab really is unresponsive now.
	with pytest.raises(asyncio.TimeoutError):
		await asyncio.wait_for(
			focus_session.cdp_client.send.Runtime.evaluate(params={'expression': '1+1'}, session_id=focus_session.session_id),
			timeout=3.0,
		)

	handler = _RecordingHandler()
	with _capture_browser_use_logs(handler):
		watchdog = CrashWatchdog(event_bus=browser_session.event_bus, browser_session=browser_session)
		await watchdog._check_browser_health()

	logged = '\n'.join(handler.messages)

	# The health check must notice that the focus tab is wedged.
	assert 'Crashed/unresponsive session detected' in logged, (
		f'hung focus tab {focus_target_id} was reported healthy; log was:\n{logged}'
	)
	assert 'Browser health check passed' not in logged

	# And it must not quietly relocate the agent onto the stray new-tab page.
	assert browser_session.agent_focus_target_id == focus_target_id, (
		f'health check moved agent focus from {focus_target_id} to {browser_session.agent_focus_target_id}'
	)


async def test_health_check_detects_hung_focus_tab_without_background_new_tab_page(browser_session: BrowserSession):
	"""Control: with no stray new-tab page around, the same hung tab is detected fine."""
	focus_target_id = browser_session.agent_focus_target_id
	assert focus_target_id is not None

	focus_session = await browser_session.get_or_create_cdp_session(target_id=focus_target_id, focus=True)
	await focus_session.cdp_client.send.Runtime.evaluate(
		params={'expression': HANG_RENDERER_JS}, session_id=focus_session.session_id
	)
	await asyncio.sleep(0.5)

	handler = _RecordingHandler()
	with _capture_browser_use_logs(handler):
		watchdog = CrashWatchdog(event_bus=browser_session.event_bus, browser_session=browser_session)
		await watchdog._check_browser_health()

	logged = '\n'.join(handler.messages)
	assert 'Crashed/unresponsive session detected' in logged, f'log was:\n{logged}'
