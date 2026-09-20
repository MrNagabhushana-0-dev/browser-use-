"""Show the tool surface of any website.

	python -m browser_use.synthesis https://news.ycombinator.com

Prints the typed tools an agent can call on that page — whether the site published them
or they were worked out from its own accessibility layer — and what they cost in tokens
against reading the page instead.

Against a site you are signed into, point it at a browser you are already driving:

	python -m browser_use.cobrowse                     # sign in here
	python -m browser_use.synthesis --cdp ws://... https://site/account
"""

import argparse
import asyncio
import json
import logging
import os
import sys

logger = logging.getLogger(__name__)


def _proxy_args() -> list[str]:
	"""Respect an ambient HTTPS proxy, which is how most corporate networks reach the web."""
	proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
	return [f'--proxy-server={proxy}'] if proxy else []


async def main() -> int:
	parser = argparse.ArgumentParser(prog='browser_use.synthesis', description=__doc__)
	parser.add_argument('url', help='Page to inspect')
	parser.add_argument('--cdp', default=None, help='Attach to a browser you are already using')
	parser.add_argument('--json', action='store_true', help='Machine-readable output')
	parser.add_argument('--headful', action='store_true', help='Show the browser')
	parser.add_argument('--wait', type=float, default=2.0, help='Seconds to let the page settle')
	args = parser.parse_args()

	logging.basicConfig(level=logging.WARNING, format='%(message)s')

	from browser_use.browser.events import NavigateToUrlEvent
	from browser_use.browser.profile import BrowserProfile
	from browser_use.browser.session import BrowserSession

	if args.cdp:
		from browser_use.cobrowse import attach

		session = await attach(args.cdp)
	else:
		session = BrowserSession(
			browser_profile=BrowserProfile(headless=not args.headful, user_data_dir=None, keep_alive=True, args=_proxy_args())
		)
		await session.start()

	try:
		event = session.event_bus.dispatch(NavigateToUrlEvent(url=args.url))
		await event
		await event.event_result(raise_if_any=False, raise_if_none=False)
		await asyncio.sleep(args.wait)

		page_tools = await session.get_webmcp_tools()
		state = await session.get_browser_state_summary(include_screenshot=False)
		dom_chars = len(state.dom_state.llm_representation())
		tools_chars = len(page_tools.prompt_description())

		if args.json:
			print(
				json.dumps(
					{
						'url': page_tools.url,
						'origin': page_tools.origin,
						'tools': [
							{
								'name': t.name,
								'signature': t.signature(),
								'description': t.description,
								'source': t.source,
								'verified': t.verified,
								'input_schema': t.input_schema,
							}
							for t in page_tools.tools
						],
						'chars': {'tools': tools_chars, 'serialized_dom': dom_chars},
					},
					indent=1,
				)
			)
			return 0

		declared = [t for t in page_tools.tools if t.source != 'synthesized']
		worked_out = [t for t in page_tools.tools if t.source == 'synthesized']

		print(f'\n  {page_tools.origin or args.url}')
		if page_tools.modal_note:
			print(f'  (a dialog is open: showing only {page_tools.modal_note})')
		print()

		if declared:
			print(f'  Published by the site ({len(declared)}):')
			for tool in declared:
				print(f'    {tool.signature()}')
			print()
		if worked_out:
			print(f'  Worked out from the page ({len(worked_out)}):')
			for tool in worked_out:
				mark = '·' if not tool.verified else '✓'
				print(f'    {mark} {tool.signature()}')
			print()
		if not page_tools.tools:
			print('  No tools: nothing on this page looks operable.\n')
			return 0

		if dom_chars:
			print(f'  {tools_chars} chars of tools vs {dom_chars} chars of serialized page')
			print(f'  ({dom_chars / max(1, tools_chars):.0f}x less to tell a model what it can do)\n')
		for error in page_tools.errors[:3]:
			print(f'  note: {error}')
		return 0
	finally:
		if not args.cdp:
			await session.kill()
		await session.event_bus.stop(clear=True, timeout=5)


if __name__ == '__main__':
	try:
		sys.exit(asyncio.run(main()))
	except KeyboardInterrupt:
		sys.exit(130)
