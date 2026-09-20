"""Play a list of browser games and report what actually happened.

	python -m browser_use.play games.json --seconds 95 --out ./play-out

Each game gets its own browser, its own recording and its own scoreboard row. One
session per game on purpose: a game that hangs, or an advert that takes the tab
somewhere strange, then costs that row and not the run.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile, ViewportSize
from browser_use.browser.session import BrowserSession
from browser_use.play.arena import GameArena
from browser_use.play.views import GameReport, Scoreboard


def _log(message: str) -> None:
	print(message, flush=True)


async def play_one(name: str, url: str, seconds: float, out: Path, record: bool) -> GameReport:
	report = GameReport(name=name, url=url)
	proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
	videos = out / 'recordings' / name.replace(' ', '_')

	session = BrowserSession(
		browser_profile=BrowserProfile(
			args=[f'--proxy-server={proxy}'] if proxy else [],
			proxy_ca_cert=os.environ.get('BROWSER_USE_PROXY_CA_CERT'),
			headless=True,
			user_data_dir=None,
			window_size=ViewportSize(width=1280, height=800),
			record_video_dir=videos if record else None,
			record_video_size=ViewportSize(width=1280, height=800),
			record_video_framerate=8,
		)
	)
	began = time.monotonic()
	try:
		await session.start()
		event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
		await event
		await event.event_result(raise_if_any=False, raise_if_none=False)
		await asyncio.sleep(6)

		arena = GameArena(session, out / 'frames')
		if await arena.enter_game(report):
			await arena.play(report, seconds=seconds)
		else:
			report.seconds_played = round(time.monotonic() - began, 1)
	except Exception as e:  # one bad game must not end the run
		report.note = (report.note + f' error: {type(e).__name__}: {e};').strip()
	finally:
		try:
			await session.kill()
		except Exception:
			pass
		await asyncio.sleep(1.5)

	if record and videos.exists():
		files = sorted(videos.glob('*.mp4'), key=lambda p: p.stat().st_size, reverse=True)
		if files:
			report.recording = str(files[0])
	return report


async def main() -> None:
	parser = argparse.ArgumentParser(prog='browser_use.play')
	parser.add_argument('games', help='JSON file of [{"name":..., "url":...}]')
	parser.add_argument('--seconds', type=float, default=95.0, help='play time per game')
	parser.add_argument('--limit', type=int, default=30)
	parser.add_argument('--skip', type=int, default=0)
	parser.add_argument('--out', default='./play-out')
	parser.add_argument('--no-record', action='store_true')
	args = parser.parse_args()

	out = Path(args.out)
	out.mkdir(parents=True, exist_ok=True)
	games = json.loads(Path(args.games).read_text())[args.skip : args.skip + args.limit]

	board = Scoreboard()
	for index, game in enumerate(games, start=1):
		_log(f'\n[{index}/{len(games)}] {game["name"]}  {game["url"]}')
		report = await play_one(game['name'], game['url'], args.seconds, out, not args.no_record)
		board.reports.append(report)
		_log('   ' + report.summary())
		if report.strategy:
			_log(f'   controls it learned: {report.strategy}')
		(out / 'scoreboard.json').write_text(board.model_dump_json(indent=1))
		(out / 'scoreboard.txt').write_text(board.render())

	_log('\n' + board.render())


if __name__ == '__main__':
	sys.exit(asyncio.run(main()) or 0)
