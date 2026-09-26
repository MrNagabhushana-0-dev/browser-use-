"""Regression test: network-based download detection must cover every tab, not just the first.

``DownloadsWatchdog.attach_to_target`` is invoked once per tab (on every ``TabCreatedEvent``,
see ``browser_use/browser/session.py``). The CDP-level ``Browser.downloadWillBegin`` /
``Browser.downloadProgress`` listeners really are browser-wide and only need registering
once, but ``_setup_network_monitoring(target_id)`` is per-*target*: it enables the
``Network`` domain for that target's CDP session and adds ``target_id`` to
``_network_monitored_targets`` so the global ``Network.responseReceived`` callback knows
to inspect that tab's responses for PDFs / ``Content-Disposition: attachment`` payloads.

Before the fix, ``attach_to_target`` returned early as soon as
``self._download_cdp_session_setup`` was already ``True`` (i.e. on every tab after the
first), which skipped the trailing ``await self._setup_network_monitoring(target_id)``
call entirely for that tab. Any tab opened after the first therefore never had its
target added to ``_network_monitored_targets``, so PDFs/attachments served on that tab
without a ``.pdf`` URL were silently never auto-downloaded, contradicting
``_setup_network_monitoring``'s own docstring ("catches ALL download variants") and the
module's stated purpose (PDF auto-download detection).
"""

import asyncio

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession

PDF_BYTES = b'%PDF-1.4\n%fake-pdf-body-for-tests\n%%EOF'


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
	loop = asyncio.get_event_loop()
	deadline = loop.time() + timeout
	while loop.time() < deadline:
		if predicate():
			return True
		await asyncio.sleep(interval)
	return predicate()


@pytest.fixture
def pdf_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/first-tab').respond_with_data('<html><body>first tab</body></html>', content_type='text/html')
	# Deliberately no ".pdf" anywhere in the path/URL so the URL-pattern based
	# `_check_url_for_pdf` detector can't catch it - only network-based
	# Content-Type sniffing (the thing this test protects) can.
	server.expect_request('/report-attachment').respond_with_data(PDF_BYTES, content_type='application/pdf')
	yield server
	server.stop()


async def test_second_tab_gets_network_download_monitoring(pdf_server: HTTPServer, tmp_path):
	"""A tab opened after the first must still be able to auto-download a PDF."""
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			downloads_path=tmp_path,
			auto_download_pdfs=True,
		)
	)
	await session.start()
	try:
		watchdog = session._downloads_watchdog
		assert watchdog is not None

		# First tab: navigate the initial tab so its target is definitely attached.
		await session.navigate_to(pdf_server.url_for('/first-tab'))
		first_target_id = session.agent_focus_target_id
		assert first_target_id is not None

		assert await _wait_until(lambda: first_target_id in watchdog._network_monitored_targets), (
			'First tab should have network download monitoring enabled'
		)

		# Second tab: opened after the browser-level CDP download listener is already set up.
		await session.navigate_to(pdf_server.url_for('/report-attachment'), new_tab=True)
		second_target_id = session.agent_focus_target_id
		assert second_target_id is not None
		assert second_target_id != first_target_id

		# Root cause assertion: the second target must be registered for network
		# monitoring too, exactly like the first one was.
		assert await _wait_until(lambda: second_target_id in watchdog._network_monitored_targets), (
			'Second (and every subsequent) tab must also get network-based download '
			'monitoring; attach_to_target must not return before calling '
			'_setup_network_monitoring for tabs opened after the first'
		)

		# Behavioral assertion: the PDF served on the second tab (no ".pdf" in the URL,
		# so only network Content-Type sniffing can catch it) actually gets downloaded.
		def _pdf_downloaded() -> bool:
			return any(f.suffix != '' for f in tmp_path.iterdir() if f.is_file())

		assert await _wait_until(_pdf_downloaded, timeout=10.0), (
			f'Expected the PDF served on the second tab to be auto-downloaded to {tmp_path}, found: {list(tmp_path.iterdir())}'
		)
	finally:
		await session.kill()
