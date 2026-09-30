"""Explore every page of a site and write a bug report, with a live token and time meter.

    uv run python examples/features/explore_site.py https://example.com/
    uv run python examples/features/explore_site.py https://example.com/ --record out/   # watch it on video

The meter in the page's corner shows the page being explored, time so far, the time left, and
the tokens an agent reading the results has spent, next to what one DOM-dump step per page
would have cost. No Playwright; CDP only. Nothing is submitted and nothing is logged into.
"""

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ViewportSize
from browser_use.explore import Explorer, render_markdown, render_sheet
from browser_use.vision.screenrec import record_display, virtual_display

WIDTH, HEIGHT = 1280, 800


async def run(args: argparse.Namespace) -> None:
	out = Path(args.out)
	out.mkdir(parents=True, exist_ok=True)
	async with contextlib.AsyncExitStack() as stack:
		if args.record:
			display = await stack.enter_async_context(virtual_display(WIDTH, HEIGHT))
			os.environ['DISPLAY'] = display
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=not args.record,
				executable_path=args.browser_path,
				chromium_sandbox=os.geteuid() != 0,
				user_data_dir=None,
				keep_alive=False,
				window_size=ViewportSize(width=WIDTH, height=HEIGHT),
				window_position=ViewportSize(width=0, height=0),
			)
		)
		await session.start()
		stack.push_async_callback(session.kill)
		if args.record:
			await stack.enter_async_context(record_display(display, out / 'explore.mp4', WIDTH, HEIGHT, fps=10))
		explorer = Explorer(session, max_pages=args.max_pages, mobile=not args.no_mobile)
		report = await explorer.run(args.url)

	(out / 'report.md').write_text(render_markdown(report))
	(out / 'report.json').write_text(json.dumps(report.model_dump(), indent=1, default=str))
	sheet = render_sheet(explorer.looks)
	if sheet:
		(out / 'pages.jpg').write_bytes(sheet)
	print(render_markdown(report))
	print(f'wrote {out}/report.md, report.json' + (', pages.jpg' if sheet else '') + (', explore.mp4' if args.record else ''))


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('url')
	parser.add_argument('--max-pages', type=int, default=40)
	parser.add_argument('--no-mobile', action='store_true', help='skip the phone-viewport check')
	parser.add_argument('--out', default='explore-out')
	parser.add_argument('--record', action='store_true', help='run on a virtual display and record it (Linux, needs Xvfb)')
	parser.add_argument('--browser-path', default=None)
	asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
	main()
