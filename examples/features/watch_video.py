"""Watch a video from its pixels and report what it cost, optionally recording the whole run.

    uv run python examples/features/watch_video.py URL --record demo/

Opens the video in a real (headful, on a virtual display) browser, clicks play the way a person
would, then seeks through it to find the shots and lays one keyframe of each on a contact
sheet. A token meter and the pointer are drawn in the page, so the recording shows the run as
a person would see it. No language model is involved: the interaction is scripted input and
the watching is pixel comparison, so the run itself costs zero tokens. What it prints is what
it would cost to *hand the result to* a model, against screenshotting once a second.

Pass a direct media URL, or any page with a <video> that is not behind DRM or a bot check.
A site that asks you to sign in to confirm you are not a bot is asking for a person; use the
co-browse handover (`python -m browser_use.cobrowse`) to sign in once, and point this at the
same profile.
"""

import argparse
import asyncio
import contextlib
import os
from pathlib import Path

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ViewportSize
from browser_use.human import HumanInput
from browser_use.vision.overlay import Overlay
from browser_use.vision.screenrec import record_display, virtual_display
from browser_use.vision.video import VideoWatcher

WIDTH, HEIGHT, FPS = 1280, 800, 12


async def run(url: str, out_dir: Path, record: bool, browser_path: str | None, proxy_ca: str | None, max_frames: int) -> None:
	out_dir.mkdir(parents=True, exist_ok=True)
	async with contextlib.AsyncExitStack() as stack:
		if record:
			display = await stack.enter_async_context(virtual_display(WIDTH, HEIGHT))
			os.environ['DISPLAY'] = display

		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=not record,
				executable_path=browser_path,
				chromium_sandbox=os.geteuid() != 0,
				user_data_dir=None,
				keep_alive=False,
				proxy_ca_cert=proxy_ca,
				window_size=ViewportSize(width=WIDTH, height=HEIGHT),
				window_position=ViewportSize(width=0, height=0),
				args=['--autoplay-policy=no-user-gesture-required'],
			)
		)
		await session.start()
		stack.push_async_callback(session.kill)

		if record:
			video_path = out_dir / 'demo.mp4'
			await stack.enter_async_context(record_display(display, video_path, WIDTH, HEIGHT, fps=FPS))

		overlay = Overlay(session)
		await asyncio.sleep(1.0)  # let the window map before there is anything to look at
		await overlay.install()
		await session.navigate_to(url)
		await overlay.show(['browser-use  vision tokens', 'opening video...'])
		await asyncio.sleep(2.0)

		# Start playback like a person: glide to the picture and click it. Chrome's own media
		# page toggles play on a click, so no selector and no model are needed.
		human = HumanInput(session)
		await human.move_to(WIDTH * 0.35, HEIGHT * 0.3)
		await asyncio.sleep(0.4)
		await human.click(WIDTH * 0.5, HEIGHT * 0.45)
		await overlay.show(['browser-use  vision tokens', 'playing... (0 tokens spent)'])
		await asyncio.sleep(3.0)

		await overlay.show(['browser-use  vision tokens', 'watching by seeking...'])
		summary = await VideoWatcher(session).watch(max_frames=max_frames)

		sheet = summary.contact_sheet(columns=4, tile_width=320)
		(out_dir / 'contact-sheet.jpg').write_bytes(sheet)
		print(summary.describe())
		print(f'signatures: {summary.signature_mode}; ledger: {summary.ledger.describe()}')
		await overlay.show_ledger(summary.ledger, summary.duration, summary.video_width or WIDTH, summary.video_height or HEIGHT)
		await asyncio.sleep(5.0)  # leave the final meter on screen for the recording
		print(f'wrote {out_dir / "contact-sheet.jpg"}' + (f' and {out_dir / "demo.mp4"}' if record else ''))


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('url')
	parser.add_argument('--record', metavar='DIR', help='record the run on a virtual display into DIR/demo.mp4')
	parser.add_argument('--out', default='video-watch', help='where to write the contact sheet when not recording')
	parser.add_argument('--browser-path', help='a Chrome/Chromium that can play the video (H.264 needs a full Chrome build)')
	parser.add_argument('--proxy-ca', help='CA certificate of a TLS-intercepting proxy to trust')
	parser.add_argument('--max-frames', type=int, default=8)
	args = parser.parse_args()
	out_dir = Path(args.record or args.out)
	asyncio.run(run(args.url, out_dir, bool(args.record), args.browser_path, args.proxy_ca, args.max_frames))


if __name__ == '__main__':
	main()
