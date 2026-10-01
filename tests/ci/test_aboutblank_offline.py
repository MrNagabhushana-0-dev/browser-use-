"""The about:blank loading screen must not reach the internet.

It used to load its logo from https://cf.browser-use.com/logo.svg, so every browser start told a
third party when and from where the library ran. The logo is now inline.
"""

import asyncio

from browser_use.browser import BrowserProfile, BrowserSession


async def test_the_loading_screen_draws_its_logo_without_fetching_anything():
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	await session.start()
	try:
		watchdog = session._aboutblank_watchdog
		assert watchdog is not None
		target_id = session.agent_focus_target_id
		assert target_id is not None
		cdp = await session.get_or_create_cdp_session(target_id, focus=False)
		await cdp.cdp_client.send.Page.navigate(params={'url': 'about:blank'}, session_id=cdp.session_id)
		await asyncio.sleep(0.3)
		await watchdog._show_dvd_screensaver_loading_animation_cdp(target_id, 'test')

		async def page(expression: str):
			r = await cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
			)
			return (r.get('result') or {}).get('value')

		for _ in range(20):
			if await page("!!document.querySelector('#pretty-loading-animation img')"):
				break
			await asyncio.sleep(0.1)
		src = await page("document.querySelector('#pretty-loading-animation img').src")
		assert isinstance(src, str) and src.startswith('data:image/svg+xml'), src
		fetched = await page("performance.getEntriesByType('resource').map(e => e.name)")
		assert not [u for u in fetched or [] if u.startswith(('http:', 'https:'))], fetched
	finally:
		await session.kill()
