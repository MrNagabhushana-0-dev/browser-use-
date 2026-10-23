"""`python -m browser_use.bridge` runs the relay; `... setup` installs and self-tests it; `... extension DIR` writes the
extension; `... doctor` checks the links."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from browser_use.bridge import DEFAULT_PORT, BridgeRelay, write_extension
from browser_use.bridge.selftest import SelfTest

SETUP = """\
Retinat bridge relay on {cdp_url}

1. Load the extension once: open chrome://extensions (edge://extensions, brave://extensions,
   opera://extensions, vivaldi://extensions), switch on Developer mode, press "Load unpacked" and pick
   {ext}
2. On any tab, press the extension's button (or Alt+Shift+A) and "Share this tab with the AI".
3. Point your MCP client at it:  retinat --bridge   (or  retinat --cdp-url {cdp_url})

The AI sees and acts only in tabs you share. Alt+Shift+Z takes the wheel back. Press Cancel on the
"started debugging this browser" bar to unshare everything at once.
"""

REPO = Path(__file__).resolve().parents[2]
# bin/mcp-serve.sh sources this (when present) before starting Retinat, which reads RETINAT_BRIDGE (retinat/front.py).
ENV_FILE = (REPO if (REPO / 'pyproject.toml').exists() else Path.cwd()) / '.retinat.env'
EXTENSION_WAIT_S = 15 * 60
SELFTEST_WAIT_S = 3 * 60  # after the extension connects
GONE_S = 30.0  # the extension away this long (browser closed) ends the wait; a worker restart is back in a second
HINT_S = 20.0


async def _serve(port: int) -> None:
	relay = await BridgeRelay(port=port).start()
	print(SETUP.format(cdp_url=relay.cdp_url, ext=Path(__file__).parent / 'extension'), flush=True)
	await asyncio.Event().wait()


async def _setup(port: int, out: Path, env_file: Path, wait_s: float, click_offset: float) -> int:
	"""Write the extension and .retinat.env, run a relay until the extension's self-test has run, report it."""
	ext = write_extension(out, relay=f'ws://127.0.0.1:{port}/extension', selftest=True)
	env_file.write_text(f'export RETINAT_BRIDGE={port}\n')
	try:
		relay = await BridgeRelay(port=port).start()
	except OSError as e:
		print(_log_port_taken(port, e), flush=True)
		return 1
	relay.selftest_click_offset = click_offset
	loop = asyncio.get_running_loop()
	try:
		print(_log_steps(relay, ext.resolve(), env_file.resolve(), port, wait_s), flush=True)
		started = loop.time()
		try:
			hello = await relay.wait_for_extension(timeout=wait_s)
		except TimeoutError:
			print(_log_no_extension(wait_s, ext.resolve()), flush=True)
			return 1
		print(_log_connected(hello, loop.time() - started), flush=True)
		result = await _await_selftest(relay, port)
		print(_log_result(result, port), flush=True)
		await relay.wait_selftest_shown(timeout=5)  # the page shows the outcome itself; let it read it first
		return 0 if result.state == 'pass' else 1
	finally:
		await relay.stop()


async def _await_selftest(relay: BridgeRelay, port: int) -> SelfTest:
	"""The self-test's outcome, or a FAIL saying why none came: the extension went away, or no self-test tab was shared."""
	loop = asyncio.get_running_loop()
	started, gone_since, hinted = loop.time(), None, False
	while True:
		try:
			return await relay.wait_for_selftest(timeout=0.5)
		except TimeoutError:
			pass
		now = loop.time()
		if relay.extension_connected:
			gone_since = None
		elif gone_since is None:
			gone_since = now
		if gone_since is not None and now - gone_since > GONE_S:
			return SelfTest(
				state='fail',
				reason=f'the extension disconnected (browser closed?) before the self-test finished and stayed away {GONE_S:.0f} s',
			)
		if not hinted and gone_since is None and relay.selftest.state == 'waiting' and now - started > HINT_S:
			hinted = True
			print(_log_no_tab_yet(port), flush=True)
		if now - started > SELFTEST_WAIT_S and relay.selftest.state != 'running':
			return SelfTest(state='fail', reason=f'no self-test tab was shared within {SELFTEST_WAIT_S // 60} min')


def _log_steps(relay: BridgeRelay, ext: Path, env_file: Path, port: int, wait_s: float) -> str:
	return f"""\
Retinat bridge setup
  extension folder  {ext}
  wrote             {env_file}  (export RETINAT_BRIDGE={port})
  relay             {relay.cdp_url} (loopback only; stops when this command ends)

In Chrome:
  1. Open chrome://extensions
  2. Switch on "Developer mode" (top right)
  3. Press "Load unpacked" and pick this folder:
       {ext}
     (Loaded from there before? Press the reload arrow on its card instead.)
  4. Pin it: the puzzle-piece icon in the toolbar, then the pin next to "Retinat bridge".
A tab opens by itself and the self-test runs in it. Waiting up to {wait_s / 60:.0f} min for the extension...
"""


def _log_connected(hello: dict, after_s: float) -> str:
	ua = hello.get('userAgent', '')
	browser = next((p for p in ua.split() if p.startswith(('Edg/', 'OPR/', 'Vivaldi/', 'Chrome/'))), 'a Chromium browser')
	return (
		f'Extension {hello.get("extension")} (Manifest V{hello.get("manifest")}) connected from {browser} '
		f'after {after_s:.1f} s. Running the self-test...'
	)


def _log_no_tab_yet(port: int) -> str:
	return (
		'No self-test tab yet. The extension opens it once per version; if this version already did, open\n'
		f'  http://127.0.0.1:{port}/selftest\n'
		'yourself, then press the Retinat bridge button in the toolbar and "Share this tab with the AI".'
	)


def _log_result(t: SelfTest, port: int) -> str:
	head = 'PASS' if t.state == 'pass' else 'FAIL'
	lines = [f'{head}  self-test through relay -> extension -> tab' + (f': {t.reason}' if t.reason else '')]
	if t.look1_ms is not None:
		size = f'{t.shot[0]}x{t.shot[1]} px, {t.scale} px per CSS px' if t.shot else ''
		lines.append(f'  attach  {t.attach_ms} ms  (first call, Page.getLayoutMetrics)')
		lines.append(f'  look 1  {t.look1_ms} ms  (Page.captureScreenshot, {size})')
	if t.click_ms is not None and t.click_at is not None:
		lines.append(
			f'  click   {t.click_ms} ms  (mousePressed + mouseReleased at {t.click_at[0]:.0f},{t.click_at[1]:.0f} CSS px)'
		)
	if t.look2_ms is not None:
		green = f'green {t.green_after_ms} ms after the click' if t.green_after_ms is not None else 'never green'
		lines.append(f'  look 2  {t.look2_ms} ms  ({green})')
	if t.before is not None:
		lines.append(f'  button  mean rgb {t.before} -> {t.after} (grey -> green is a pass; {t.looks} looks in all)')
	if t.state == 'pass':
		lines.append(
			'Next: open the YouTube video you were given (captions off), press the Retinat bridge button in the toolbar,\n'
			'      then "Share this tab with the AI". Then start Claude as checklist step 6 says\n'
			'      (cd ~/retinat, then claude --remote-control --permission-mode auto).'
		)
	else:
		lines.append(
			'To retry: run this command again. It opens the self-test tab itself; do not share the relay\'s own\n'
			'          address by hand (the bridge refuses that).'
		)
	return '\n'.join(lines)


def _log_no_extension(wait_s: float, ext: Path) -> str:
	return (
		f'FAIL  the extension did not connect within {wait_s / 60:.0f} min.\n'
		f'Load {ext} with "Load unpacked" on chrome://extensions (Developer mode on), then run this command again.'
	)


def _log_port_taken(port: int, error: OSError) -> str:
	return (
		f'FAIL  port {port} is taken ({error.strerror or error}). Most likely Retinat is already running its relay there\n'
		'(a Claude session with RETINAT_BRIDGE set). Close that session and run this again, or pick another port:\n'
		'  python -m browser_use.bridge setup --port 9334'
	)


def main() -> None:
	parser = argparse.ArgumentParser(prog='python -m browser_use.bridge', description=__doc__)
	sub = parser.add_subparsers(dest='command')
	setup = sub.add_parser(
		'setup', help='write the extension and .retinat.env, wait for Load unpacked, run the self-test (exit 0 on PASS)'
	)
	setup.add_argument('--port', type=int, default=DEFAULT_PORT, help='relay port (default %(default)s)')
	setup.add_argument(
		'--out', type=Path, default=Path.home() / 'retinat-extension', help='extension folder (default %(default)s)'
	)
	setup.add_argument('--env-file', type=Path, default=ENV_FILE, help='where RETINAT_BRIDGE is written (default %(default)s)')
	setup.add_argument('--wait', type=float, default=EXTENSION_WAIT_S, help='seconds to wait for the extension (default 900)')
	setup.add_argument('--selftest-click-offset', type=float, default=0.0, help=argparse.SUPPRESS)  # a FAIL on purpose
	ext = sub.add_parser('extension', help='write the extension to a folder for "Load unpacked"')
	ext.add_argument('out', type=Path)
	ext.add_argument('--port', type=int, default=DEFAULT_PORT, help='relay port the extension dials')
	ext.add_argument('--mv2', action='store_true', help='Manifest V2 variant for Chromium older than 88')
	ext.add_argument('--selftest', action='store_true', help='open the self-test tab once after install or update')
	ext.add_argument('--always-share', action='append', default=None, metavar='GLOB', help='URL glob shared without asking')
	ext.add_argument(
		'--always-allow', action='append', default=None, metavar='SITE', help='site the AI may use without asking (* for all)'
	)
	ext.add_argument(
		'--ask-every-time', action='append', default=None, metavar='SITE', help='site asked about on every visit (a bank)'
	)
	doctor = sub.add_parser('doctor', help='check relay, extension, browser, policy, shared tabs and wheel; say how to fix')
	doctor.add_argument('--port', type=int, default=DEFAULT_PORT, help='relay port to check')
	doctor.add_argument('--json', action='store_true', help='print the checks as JSON')
	parser.add_argument('--port', type=int, default=DEFAULT_PORT)
	args = parser.parse_args()
	if args.command == 'setup':
		logging.basicConfig(level=logging.WARNING, format='%(message)s')
		try:
			code = asyncio.run(_setup(args.port, args.out, args.env_file, args.wait, args.selftest_click_offset))
		except KeyboardInterrupt:
			code = 130
		sys.exit(code)
	if args.command == 'doctor':
		from browser_use.bridge.doctor import _log_checks, diagnose

		found = asyncio.run(diagnose(args.port))
		print(json.dumps([c.model_dump() for c in found], indent=1) if args.json else _log_checks(found))
		sys.exit(1 if any(c.status == 'fail' for c in found) else 0)
	if args.command == 'extension':
		out = write_extension(
			args.out,
			relay=f'ws://127.0.0.1:{args.port}/extension',
			always_share=args.always_share,
			always_allow=args.always_allow,
			ask_every_time=args.ask_every_time,
			manifest_version=2 if args.mv2 else 3,
			selftest=True if args.selftest else None,
		)
		print(f'Wrote the extension to {out}; load it with "Load unpacked".')
		return
	logging.basicConfig(level=logging.INFO, format='%(message)s')
	asyncio.run(_serve(args.port))


if __name__ == '__main__':
	main()
