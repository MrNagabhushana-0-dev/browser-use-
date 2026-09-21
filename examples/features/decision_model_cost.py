"""What it costs to ask a page a question, two ways.

An agent makes a lot of small decisions that are not writing: which tool here does what I
want, is this a login wall, did that click work. Routing those through a model that
generates prose means paying for a paragraph to get one name back — and paying to put the
whole page in front of it to ask.

This measures the input side of the cheaper route. `page_state()` hands a decision model
the synthesized tool surface rather than the markup, so the two reductions compose:
synthesis makes the page small, and a decision model reads it without generating anything.

    uv run examples/features/decision_model_cost.py

Two runs over the five sites below in September 2026 gave 171x and 141x — 539 tokens of
decision state against 92,211 of page HTML, then 464 against 65,523. The gap between the
two runs is one site failing to load, which contributes no HTML and so drags the ratio
down; that is the honest shape of a live-site measurement and the reason the script ships
rather than the number. No output tokens are billed either way.

Needs no API key: it measures what would be sent, not what comes back.
"""

import asyncio
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

load_dotenv()

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.decide import page_state

SITES = [
	'https://news.ycombinator.com',
	'https://pypi.org',
	'https://www.gov.uk',
	'https://crates.io',
	'https://openlibrary.org',
]


def count_tokens(text: str) -> int:
	"""Real tokens where tiktoken is installed, a reasonable stand-in where it is not."""
	try:
		import tiktoken

		return len(tiktoken.get_encoding('cl100k_base').encode(text or ''))
	except Exception:
		return len(text or '') // 4


async def main() -> None:
	proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
	session = BrowserSession(
		browser_profile=BrowserProfile(
			args=[f'--proxy-server={proxy}'] if proxy else [],
			proxy_ca_cert=os.environ.get('BROWSER_USE_PROXY_CA_CERT'),
			headless=True,
			user_data_dir=None,
		)
	)
	await session.start()
	total_state = total_page = 0
	try:
		for url in SITES:
			event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
			await event
			await event.event_result(raise_if_any=False, raise_if_none=False)
			await asyncio.sleep(3)

			page_tools = await session.get_webmcp_tools()
			title = (await session.run_page_script('return document.title;')).value or ''
			state_tokens = count_tokens(json.dumps(page_state(page_tools, title)))

			markup = await session.run_page_script("return document.body ? document.body.innerHTML : '';", max_chars=400_000)
			page_tokens = count_tokens(markup.value or '')

			total_state += state_tokens
			total_page += page_tokens
			ratio = page_tokens / max(1, state_tokens)
			print(
				f'  {url:<34} tools {len(page_tools.tools):<3} '
				f'decision state {state_tokens:<6} page html {page_tokens:<7} {ratio:.0f}x'
			)
	finally:
		await session.kill()

	print(
		f'\n  {total_state} tokens of decision state against {total_page} of page html '
		f'— {total_page / max(1, total_state):.0f}x less to ask which tool to use'
	)


if __name__ == '__main__':
	asyncio.run(main())
