"""Protocol v2b rig: this container's stand-in for a person's own browser on the P2 path (scaffolding, never shipped).

`up` starts an Xvfb display, the protocol v2 film server, and the container Chromium with the Retinat bridge extension
added by --load-extension (standing in for the person's Load unpacked), set to share the film page and nothing else.
Retinat, started in bridge mode (RETINAT_BRIDGE in the repo's .retinat.env, read by bin/mcp-serve.sh), runs the relay
the extension dials. `down` stops what `up` started, by the PIDs it wrote down.

usage: python scripts/contract/rig_v2b.py up --stim DIR [--port 9333] [--display :99]
       python scripts/contract/rig_v2b.py down
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from browser_use.bridge import write_extension
from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog

STATE = Path.home() / '.config' / 'browseruse' / 'rig_v2b.json'
FILM_PORT = 8765
FILM_SITE = f'http://127.0.0.1:{FILM_PORT}'


def up(stim: Path, port: int, display: str) -> dict:
	assert (stim / 'watch.html').exists() and (stim / 'bbb480.webm').exists(), f'protocol v2 stimulus missing in {stim}'
	assert not STATE.exists(), f'a rig is already up ({STATE}); run down first'
	root = STATE.parent / 'rig_v2b'
	pids: dict[str, int] = {}
	if not Path(f'/tmp/.X11-unix/X{display.lstrip(":")}').exists():
		pids['xvfb'] = subprocess.Popen(
			['Xvfb', display, '-screen', '0', '1280x900x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
		).pid
		time.sleep(1.0)
	pids['film'] = subprocess.Popen(
		[sys.executable, '-m', 'http.server', str(FILM_PORT), '--bind', '127.0.0.1'],
		cwd=stim,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	).pid
	ext = write_extension(
		root / 'ext',
		relay=f'ws://127.0.0.1:{port}/extension',
		always_share=[f'{FILM_SITE}/*'],
		always_allow=[FILM_SITE],
	)
	binary = LocalBrowserWatchdog._find_installed_browser_path()
	assert binary, 'no Chromium found'
	args = [
		binary,
		f'--user-data-dir={root / "profile"}',
		f'--load-extension={ext}',
		f'--disable-extensions-except={ext}',
		'--no-first-run',
		'--no-default-browser-check',
		'--window-position=0,0',
		'--window-size=1200,860',
		# the test build turns on testing field trials, one of which breaks cross-site frames; a person's browser has none
		'--disable-field-trial-config',
		'--autoplay-policy=no-user-gesture-required',
	]
	if os.geteuid() == 0:
		args[1:1] = ['--no-sandbox', '--test-type']  # root needs --no-sandbox; --test-type drops its warning bar
	pids['browser'] = subprocess.Popen(
		[*args, f'{FILM_SITE}/watch.html'],
		env={**os.environ, 'DISPLAY': display},
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	).pid
	state = {'pids': pids, 'port': port, 'display': display, 'extension': str(ext), 'browser': binary, 'started': time.time()}
	STATE.parent.mkdir(parents=True, exist_ok=True)
	STATE.write_text(json.dumps(state, indent=1))
	return state


def down() -> dict:
	if not STATE.exists():
		return {'down': 'nothing was up'}
	state = json.loads(STATE.read_text())
	for name in ('browser', 'film', 'xvfb'):
		pid = state['pids'].get(name)
		if pid:
			try:
				os.kill(pid, 15)
			except ProcessLookupError:
				pass
	STATE.unlink()
	return {'down': state['pids']}


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	sub = parser.add_subparsers(dest='command', required=True)
	u = sub.add_parser('up')
	u.add_argument('--stim', type=Path, required=True)
	u.add_argument('--port', type=int, default=9333)
	u.add_argument('--display', default=':99')
	sub.add_parser('down')
	args = parser.parse_args()
	print(json.dumps(up(args.stim, args.port, args.display) if args.command == 'up' else down(), indent=1))
