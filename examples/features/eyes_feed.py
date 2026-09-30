"""Scroll a short-video feed with the browser as your eyes: watch, get bored, flick, repeat.

    uv run python examples/features/eyes_feed.py URL                      # headless
    uv run python examples/features/eyes_feed.py URL --record out/        # on a virtual display, recorded
    uv run python examples/features/eyes_feed.py https://www.instagram.com/reels/ \\
        --profile ~/.config/browseruse/profiles/default --login           # your own feed, on your machine

Each item is watched from its own decoded frames and its own audio (tapped inside the page,
muted or not), until it stops showing anything new or loops; then a thumb flick moves to the
next one, and the move is confirmed by sight before it counts. At the end you get one sheet
image (a row per item: keyframes plus a spectrogram strip) and a timeline in text, and the
reading is drawn live in the corner of the page as it happens.

`--login` opens a visible browser on the given profile and waits for you to sign in, once;
the profile keeps the session afterwards. Nothing here automates a login or a challenge.
Instagram serves H.264, which Chromium builds without proprietary codecs cannot decode, so
point `--browser-path` at Google Chrome if the feed stays black.
"""

import argparse
import asyncio
import contextlib
import os
import time
from pathlib import Path

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ViewportSize
from browser_use.eyes import Eyes
from browser_use.vision.overlay import Overlay
from browser_use.vision.screenrec import record_display, virtual_display

WIDTH, HEIGHT, FPS = 1280, 800, 15


async def _narrate(eyes: Eyes, overlay: Overlay, started: float, stop: asyncio.Event) -> None:
	"""Draw the eyes' current reading in the page once a second, for the person watching along."""
	while not stop.is_set():
		line = eyes.now_line()
		parts = line.split(' · ')
		await overlay.show(
			['browser-use eyes', *[p[:48] for p in parts[:5]], f'{time.monotonic() - started:5.1f}s  no screenshots']
		)
		try:
			await asyncio.wait_for(stop.wait(), 1.0)
		except TimeoutError:
			pass


async def run(args: argparse.Namespace) -> None:
	out_dir = Path(args.out)
	out_dir.mkdir(parents=True, exist_ok=True)
	async with contextlib.AsyncExitStack() as stack:
		if args.record:
			display = await stack.enter_async_context(virtual_display(WIDTH, HEIGHT))
			os.environ['DISPLAY'] = display
		headless = not (args.record or args.login or args.headful)
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=headless,
				executable_path=args.browser_path,
				chromium_sandbox=os.geteuid() != 0,
				user_data_dir=args.profile,
				keep_alive=False,
				proxy_ca_cert=args.proxy_ca,
				window_size=ViewportSize(width=WIDTH, height=HEIGHT),
				window_position=ViewportSize(width=0, height=0),
				args=['--autoplay-policy=no-user-gesture-required'],
			)
		)
		await session.start()
		stack.push_async_callback(session.kill)
		await session.navigate_to(args.url)
		if args.login:
			await asyncio.to_thread(input, 'Sign in in the browser window if needed, open the feed, then press Enter here... ')
		if args.record:
			await stack.enter_async_context(record_display(display, out_dir / 'eyes-feed.mp4', WIDTH, HEIGHT, fps=FPS))

		overlay = Overlay(session)
		await overlay.install()
		eyes = Eyes(session)
		await eyes.open()
		await asyncio.sleep(1.5)

		stop = asyncio.Event()
		narrator = asyncio.create_task(_narrate(eyes, overlay, time.monotonic(), stop))
		try:
			percept = await eyes.browse(
				items=args.items, max_seconds=args.max_seconds, min_seconds=args.min_seconds, detail=args.detail
			)
		finally:
			stop.set()
			await narrator

		(out_dir / 'eyes-feed.txt').write_text(percept.text)
		if percept.image:
			(out_dir / 'eyes-feed-sheet.jpg').write_bytes(percept.image)
		print(percept.text)
		print(f'\nwrote {out_dir}/eyes-feed.txt' + (' and eyes-feed-sheet.jpg' if percept.image else ''))


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('url')
	parser.add_argument('--items', type=int, default=5)
	parser.add_argument('--max-seconds', type=float, default=15.0)
	parser.add_argument('--min-seconds', type=float, default=3.0)
	parser.add_argument('--detail', choices=['glance', 'look', 'study'], default='glance')
	parser.add_argument('--out', default='eyes-out')
	parser.add_argument('--record', action='store_true', help='run on a virtual display and record it (Linux, needs Xvfb)')
	parser.add_argument('--headful', action='store_true')
	parser.add_argument('--login', action='store_true', help='open a visible browser and wait for you to sign in')
	parser.add_argument('--profile', default=None, help='persistent profile directory (keeps your login)')
	parser.add_argument('--browser-path', default=None)
	parser.add_argument('--proxy-ca', default=None)
	asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
	main()
