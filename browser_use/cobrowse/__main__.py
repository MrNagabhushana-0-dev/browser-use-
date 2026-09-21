"""Open a browser to sign into, then hand it to the agent.

	python -m browser_use.cobrowse

Sign in to whatever you need — Google, Instagram, your bank's staging site. Leave the
window open. The command prints a CDP URL; give that to the agent and it continues in the
same browser, same cookies, same tab, driving the UI as you would.

The profile directory is persistent, so you only sign in once. Closing this command shuts
the browser down cleanly, which is what commits your cookies to disk.
"""

import argparse
import asyncio
import logging
import os
import sys

from browser_use.cobrowse.service import launch_for_human

logger = logging.getLogger(__name__)


def _default_profile() -> str:
	from browser_use.config import CONFIG

	return str(CONFIG.BROWSER_USE_CONFIG_DIR / 'profiles' / 'cobrowse')


async def main() -> int:
	parser = argparse.ArgumentParser(prog='browser_use.cobrowse', description=__doc__)
	parser.add_argument('--profile', default=_default_profile(), help='Persistent profile directory')
	parser.add_argument('--port', type=int, default=None, help='Debugging port (default: a free one)')
	parser.add_argument('--url', default=None, help='Page to open first')
	parser.add_argument(
		'--proxy-ca-cert',
		dest='proxy_ca_cert',
		default=None,
		help='CA certificate a TLS-terminating proxy presents, so HTTPS pages load (defaults to $BROWSER_USE_PROXY_CA_CERT)',
	)
	parser.add_argument('--headless', action='store_true', help='For testing; defeats the purpose otherwise')
	args = parser.parse_args()

	logging.basicConfig(level=logging.INFO, format='%(message)s')

	# '--' guards the URL: without it a value like --headless would land in Chrome's argv
	# as a flag rather than as the page to open.
	extra = ['--', args.url] if args.url else []
	browser = await launch_for_human(
		user_data_dir=args.profile,
		port=args.port,
		headless=args.headless,
		extra_args=extra,
		proxy_ca_cert=args.proxy_ca_cert or os.environ.get('BROWSER_USE_PROXY_CA_CERT'),
	)

	print('\n  Browser is open. Sign in to whatever you need, and leave it running.\n')
	print(f'  CDP URL   {browser.cdp_url}')
	print(f'  Profile   {browser.user_data_dir}')
	print('\n  Hand it to the agent with:\n')
	print('      from browser_use.cobrowse import attach, focus_human_tab')
	print(f"      session = await attach('{browser.cdp_url}')")
	print('      await focus_human_tab(session)\n')
	print('  Ctrl-C here when you are done (this is what saves your cookies).\n')

	try:
		# Outlive the launcher: the person needs time to sign in. Waiting on the process
		# itself rather than polling means Ctrl-C lands immediately.
		if browser.process is not None:
			await browser.process.wait()
	except (KeyboardInterrupt, asyncio.CancelledError):
		print('\n  Closing the browser cleanly...')
	finally:
		await browser.close()
	return 0


if __name__ == '__main__':
	try:
		sys.exit(asyncio.run(main()))
	except KeyboardInterrupt:
		sys.exit(130)
