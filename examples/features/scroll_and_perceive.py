"""Scroll a page like a person while a live perception stream reports what is on screen.

    uv run python examples/features/scroll_and_perceive.py URL --record demo/

Real wheel events, in notches, with pauses as if reading. Meanwhile the page's screencast is
fed to `PerceptionStream`, which turns each frame into one short line of text (scroll direction,
where the regions are, what kind they look like, which way they are moving). The line and its
running token cost are drawn live in the corner of the page, next to what it would have cost to
screenshot every tick instead.

No language model is involved in any of that. It is classical perception, so it knows *where*
and *what kind* (text, media, button) but not what the text *says*: for that, a model has to
look at an image, and the run ends by laying the frames that changed on one contact sheet for
exactly that purpose.

The meter is drawn in the page, so the screencast contains it. Left alone, the stream would
perceive its own numbers changing as motion in the corner and report them; the frame is masked
there before it is observed.
"""

import argparse
import asyncio
import contextlib
import os
from io import BytesIO
from pathlib import Path

from PIL import Image

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ViewportSize
from browser_use.human import HumanInput
from browser_use.vision import (
	LiveView,
	PerceptionStream,
	Shot,
	VideoSummary,
	estimate_image_tokens,
)
from browser_use.vision.overlay import Overlay
from browser_use.vision.screenrec import record_display, virtual_display

WIDTH, HEIGHT, FPS = 1280, 800, 12
TICK_SECONDS = 0.25
# Where the meter sits, as fractions of the frame: masked so the stream does not perceive itself.
HUD_MASK = (0.68, 0.0, 1.0, 0.17)
MAX_KEYFRAMES = 10


def _masked(jpeg: bytes) -> bytes:
	image = Image.open(BytesIO(jpeg)).convert('RGB')
	w, h = image.size
	x0, y0, x1, y1 = HUD_MASK
	image.paste((128, 128, 128), (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
	out = BytesIO()
	image.save(out, format='JPEG', quality=70)
	return out.getvalue()


class _Meter:
	"""Running token arithmetic for the stream, against a screenshot per tick."""

	def __init__(self) -> None:
		self.ticks = 0
		self.stream_tokens = 0
		self.naive_per_tick = estimate_image_tokens(WIDTH, HEIGHT - 100)

	def add(self, line: str) -> None:
		self.ticks += 1
		self.stream_tokens += max(1, round(len(line) / 4))

	def lines(self, latest: str) -> list[str]:
		naive = self.ticks * self.naive_per_tick
		saved = 100 * (1 - self.stream_tokens / naive) if naive else 0.0
		return [
			'browser-use  live perception',
			latest[:46],
			f'ticks {self.ticks:>4}   stream ~{self.stream_tokens:,} tok',
			f'1 shot/tick     ~{naive:,} tok',
			f'saved {saved:5.1f} %  (estimates)',
		]


async def _perceive(live: LiveView, overlay: Overlay, stream: PerceptionStream, meter: _Meter) -> None:
	"""Every new frame goes to the stream, but only one line per tick is 'sent' and counted.

	Frames are fed one by one because a few wheel notches between two ticks can be more than a
	screen of movement, which is further than consecutive frames ever are; the cost that matters
	is what would be handed to a reader, which is the tick's most informative line.
	"""
	seen_at = -1.0
	while True:
		await asyncio.sleep(TICK_SECONDS)
		fresh = [frame for frame in live.frames if frame.at > seen_at]
		if not fresh:
			continue
		seen_at = fresh[-1].at
		lines = [line for frame in fresh if (line := stream.observe(_masked(frame.data), at=frame.at)) is not None]
		if not lines:
			continue
		scrolls = [line for line in lines if 'scroll=' in line]
		shown = (scrolls or lines)[-1]
		meter.add(shown)
		await overlay.show(meter.lines(shown))


async def run(url: str, out_dir: Path, record: bool, browser_path: str | None, proxy_ca: str | None, scrolls: int) -> None:
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
			)
		)
		await session.start()
		stack.push_async_callback(session.kill)
		if record:
			await stack.enter_async_context(record_display(display, out_dir / 'demo.mp4', WIDTH, HEIGHT, fps=FPS))

		overlay = Overlay(session)
		await asyncio.sleep(1.0)
		await overlay.install()
		await session.navigate_to(url)
		await asyncio.sleep(2.0)

		live = LiveView(session)
		await live.start(max_width=640, quality=60)
		stream, meter = PerceptionStream(), _Meter()
		kept: list[tuple[float, bytes]] = []
		watcher = asyncio.create_task(_perceive(live, overlay, stream, meter))

		human = HumanInput(session)
		await human.move_to(WIDTH * 0.5, HEIGHT * 0.55)
		await asyncio.sleep(1.0)
		for i in range(scrolls):
			# Mostly down; one glance back up, as a reader does when they lose the thread.
			delta = -350 if i == scrolls // 2 else 420 + 60 * (i % 3)
			await human.wheel(delta)
			await asyncio.sleep(1.2 + 0.3 * (i % 3))  # reading
			if live.frames and len(kept) < MAX_KEYFRAMES:
				kept.append((live.frames[-1].at, live.frames[-1].data))  # what is on screen after this scroll

		await asyncio.sleep(1.0)
		# Ground truth, read from the page itself, against what the stream inferred from pixels alone.
		cdp = await session.get_or_create_cdp_session(focus=False)
		truth = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'window.scrollY / window.innerHeight', 'returnByValue': True}, session_id=cdp.session_id
		)
		actual = float(truth['result'].get('value') or 0.0)
		watcher.cancel()
		with contextlib.suppress(asyncio.CancelledError):
			await watcher
		await live.stop()

		shots = []
		for index, (at, jpeg) in enumerate(kept):
			end = kept[index + 1][0] if index + 1 < len(kept) else at + 1.0
			image = Image.open(BytesIO(jpeg))
			shots.append(Shot(start=at, end=end, at=at, keyframe=jpeg, width=image.width, height=image.height))
		summary = VideoSummary(
			duration=kept[-1][0] + 1.0 if kept else 0.0,
			shots=shots,
			samples_taken=meter.ticks,
			signature_mode='screencast',
		)
		(out_dir / 'scroll-sheet.jpg').write_bytes(summary.contact_sheet(columns=4, tile_width=300))
		(out_dir / 'perception-lines.txt').write_text('\n'.join(stream.lines) + '\n')

		print(
			f'page position: actual {actual:.2f}h, inferred from pixels {stream.position:.2f}h{"" if stream.position_exact else " (marked uncertain)"} (error {abs(actual - stream.position):.2f}h)'
		)
		print(
			f'{meter.ticks} ticks, {len(shots)} keyframes; stream ~{meter.stream_tokens:,} tok vs ~{meter.ticks * meter.naive_per_tick:,} tok'
		)
		print('last lines of the stream:\n' + stream.digest(8))
		await overlay.show(meter.lines('done'))
		await asyncio.sleep(3.0)


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('url')
	parser.add_argument('--record', metavar='DIR', help='record the run on a virtual display into DIR/demo.mp4')
	parser.add_argument('--out', default='scroll-perceive', help='where to write outputs when not recording')
	parser.add_argument('--browser-path')
	parser.add_argument('--proxy-ca', help='CA certificate of a TLS-intercepting proxy to trust')
	parser.add_argument('--scrolls', type=int, default=10)
	args = parser.parse_args()
	out_dir = Path(args.record or args.out)
	asyncio.run(run(args.url, out_dir, bool(args.record), args.browser_path, args.proxy_ca, args.scrolls))


if __name__ == '__main__':
	main()
