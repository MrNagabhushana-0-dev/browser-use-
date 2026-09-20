"""
Measure what browser-use can tell a model about real websites, and what it costs.

For each site this navigates, induces a typed tool surface from the page's own
accessibility layer, and compares the tokens that surface costs against the tokens the
serialized page would have cost to say the same thing.

    uv run examples/features/site_tool_surface.py
    uv run examples/features/site_tool_surface.py https://your-site.example

Behind a TLS-terminating proxy, point Chromium at the CA it presents:

    export BROWSER_USE_PROXY_CA_CERT=/path/to/proxy-ca.crt

A run over twenty public sites in September 2026 produced tools on seventeen of them at a
median of 10ms each, for 1,913 tokens of tools against 44,041 tokens of serialized page.
Numbers will drift as those sites change; the script is here so the claim can be checked
rather than taken on trust.
"""

import asyncio
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

load_dotenv()

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

SITES = [
	'https://en.wikipedia.org/wiki/Web_scraping',
	'https://news.ycombinator.com',
	'https://pypi.org',
	'https://www.python.org',
	'https://arxiv.org',
	'https://stackoverflow.com/questions',
	'https://www.gov.uk',
	'https://www.nasa.gov',
	'https://openlibrary.org',
	'https://www.gutenberg.org',
	'https://crates.io',
]


def count_tokens(text: str) -> int:
	"""Real tokens where tiktoken is installed, a reasonable stand-in where it is not."""
	try:
		import tiktoken

		return len(tiktoken.get_encoding('cl100k_base').encode(text or ''))
	except Exception:
		return len(text or '') // 4


async def main() -> None:
	sites = sys.argv[1:] or SITES
	proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')

	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=True,
			args=[f'--proxy-server={proxy}'] if proxy else [],
		)
	)
	await session.start()

	rows: list[tuple[str, int, int, int, int]] = []
	try:
		for url in sites:
			try:
				event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
				await event
				await event.event_result(raise_if_any=False, raise_if_none=False)
				await asyncio.sleep(2.0)

				started = time.perf_counter()
				page_tools = await session.get_webmcp_tools()
				elapsed_ms = round((time.perf_counter() - started) * 1000)

				state = await session.get_browser_state_summary(include_screenshot=False)
				tool_tokens = count_tokens(page_tools.prompt_description())
				dom_tokens = count_tokens(state.dom_state.llm_representation())
			except Exception as e:
				print(f'  {url}\n    skipped: {type(e).__name__}: {e}')
				continue

			rows.append((url, len(page_tools.tools), elapsed_ms, tool_tokens, dom_tokens))
			print(f'\n  {url}  ({elapsed_ms}ms)')
			for tool in page_tools.tools:
				print(f'    {tool.signature()}')
			if not page_tools.tools:
				print('    (nothing operable found)')
	finally:
		await session.kill()
		await session.event_bus.stop(clear=True, timeout=5)

	with_tools = [row for row in rows if row[1]]
	tool_total = sum(row[3] for row in rows)
	dom_total = sum(row[4] for row in rows)

	print('\n  ' + '-' * 60)
	print(f'  sites visited        {len(rows)}')
	print(f'  sites with tools     {len(with_tools)}')
	if with_tools:
		timings = sorted(row[2] for row in with_tools)
		print(f'  median synthesis     {timings[len(timings) // 2]}ms')
	print(f'  tokens as tools      {tool_total}')
	print(f'  tokens as page       {dom_total}')
	if tool_total:
		print(f'  ratio                {dom_total / tool_total:.0f}x less')


if __name__ == '__main__':
	asyncio.run(main())
