"""`python -m browser_use.bridge` runs the relay; `... extension DIR` writes the extension; `... doctor` checks the links."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from browser_use.bridge import DEFAULT_PORT, BridgeRelay, write_extension

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


async def _serve(port: int) -> None:
	relay = await BridgeRelay(port=port).start()
	print(SETUP.format(cdp_url=relay.cdp_url, ext=Path(__file__).parent / 'extension'), flush=True)
	await asyncio.Event().wait()


def main() -> None:
	parser = argparse.ArgumentParser(prog='python -m browser_use.bridge', description=__doc__)
	sub = parser.add_subparsers(dest='command')
	ext = sub.add_parser('extension', help='write the extension to a folder for "Load unpacked"')
	ext.add_argument('out', type=Path)
	ext.add_argument('--port', type=int, default=DEFAULT_PORT, help='relay port the extension dials')
	ext.add_argument('--mv2', action='store_true', help='Manifest V2 variant for Chromium older than 88')
	ext.add_argument('--always-share', action='append', default=None, metavar='GLOB', help='URL glob shared without asking')
	doctor = sub.add_parser('doctor', help='check relay, extension, browser, policy, shared tabs and wheel; say how to fix')
	doctor.add_argument('--port', type=int, default=DEFAULT_PORT, help='relay port to check')
	doctor.add_argument('--json', action='store_true', help='print the checks as JSON')
	parser.add_argument('--port', type=int, default=DEFAULT_PORT)
	args = parser.parse_args()
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
			manifest_version=2 if args.mv2 else 3,
		)
		print(f'Wrote the extension to {out}; load it with "Load unpacked".')
		return
	logging.basicConfig(level=logging.INFO, format='%(message)s')
	asyncio.run(_serve(args.port))


if __name__ == '__main__':
	main()
