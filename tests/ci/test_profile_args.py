import sys

import pytest

from browser_use.browser import BrowserSession
from browser_use.browser.profile import CHROME_DEFAULT_ARGS, BrowserProfile


def test_get_args_keeps_default_order_when_ignoring_default_args(tmp_path):
	profile = BrowserProfile(
		user_data_dir=tmp_path,
		ignore_default_args=['--disable-popup-blocking', '--no-default-browser-check'],
		enable_default_extensions=False,
	)

	args = profile.get_args()

	expected_defaults = [
		arg for arg in CHROME_DEFAULT_ARGS if arg not in {'--disable-popup-blocking', '--no-default-browser-check'}
	]
	actual_defaults = [arg for arg in args if arg in CHROME_DEFAULT_ARGS]

	assert actual_defaults == expected_defaults
	assert '--disable-popup-blocking' not in args
	assert '--no-default-browser-check' not in args


def test_get_args_keeps_default_order_with_default_ignored_args(tmp_path):
	profile = BrowserProfile(user_data_dir=tmp_path, enable_default_extensions=False)

	args = profile.get_args()

	ignored_default_args = profile.ignore_default_args if isinstance(profile.ignore_default_args, list) else []
	expected_defaults = [arg for arg in CHROME_DEFAULT_ARGS if arg not in ignored_default_args]
	actual_defaults = [arg for arg in args if arg in CHROME_DEFAULT_ARGS]

	assert actual_defaults == expected_defaults


@pytest.mark.skipif(sys.platform != 'linux', reason='only Linux runs without a display server')
async def test_a_headful_request_with_no_display_falls_back_to_headless_and_launches(tmp_path, monkeypatch):
	# Containers and CI have no X or Wayland server: a headful Chrome dies before CDP is up, with an error
	# that never says why. The MCP servers default to headful, so this is the path they take there.
	monkeypatch.delenv('DISPLAY', raising=False)
	monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
	profile = BrowserProfile(headless=False, user_data_dir=tmp_path, enable_default_extensions=False)
	profile.detect_display_configuration()
	assert profile.headless is True
	assert '--headless=new' in profile.get_args()

	session = BrowserSession(
		browser_profile=BrowserProfile(headless=False, user_data_dir=tmp_path / 'p', enable_default_extensions=False)
	)
	try:
		await session.start()
		assert session.browser_profile.headless is True
	finally:
		await session.kill()
