"""The headless UA must describe the host it actually runs on.

A UA string claiming `X11; Linux x86_64` while navigator.platform and
navigator.userAgentData report macOS or Windows is a free automation signal, so
`_ua_os_token()` maps the host OS onto the token real Chrome would send there.
"""

import platform

import pytest

from browser_use.browser.profile import BrowserProfile, _ua_os_token


def test_ua_os_token_maps_darwin_to_mac():
	assert _ua_os_token('Darwin') == 'Macintosh; Intel Mac OS X 10_15_7'


def test_ua_os_token_maps_windows():
	assert _ua_os_token('Windows') == 'Windows NT 10.0; Win64; x64'


@pytest.mark.parametrize('system', ['Linux', 'FreeBSD', ''])
def test_ua_os_token_falls_back_to_linux(system: str):
	assert _ua_os_token(system) == 'X11; Linux x86_64'


def test_headful_user_agent_uses_host_os_token(tmp_path):
	profile = BrowserProfile(user_data_dir=tmp_path, headless=True, enable_default_extensions=False)

	user_agent = profile._headful_user_agent()

	assert f'({_ua_os_token(platform.system())})' in user_agent
	assert 'HeadlessChrome' not in user_agent


@pytest.mark.skipif(platform.system() != 'Linux', reason='asserts the Linux host UA token')
def test_get_args_emits_linux_user_agent_on_linux(tmp_path):
	profile = BrowserProfile(user_data_dir=tmp_path, headless=True, enable_default_extensions=False)

	ua_args = [arg for arg in profile.get_args() if arg.startswith('--user-agent=')]

	assert len(ua_args) == 1, ua_args
	assert 'X11; Linux x86_64' in ua_args[0]
	assert 'HeadlessChrome' not in ua_args[0]
