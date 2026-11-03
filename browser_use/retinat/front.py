"""What a client first meets of Retinat: the MCP handshake and the tool list, answered at once from light modules,
while the server proper is imported in a worker thread (see `browser_use.serving.Front`)."""

import argparse
import asyncio
import os
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import mcp.types as types

from browser_use.net import NetworkMode, NetworkRouter
from browser_use.net.tools import network_tools
from browser_use.retinat.tools import _tools
from browser_use.serving import Front, serve_until_stopped

if TYPE_CHECKING:
	from browser_use.retinat.server import RetinatServer


def tool_list() -> list[types.Tool]:
	"""What Retinat serves, as `RetinatServer` lists it."""
	return [*_tools(), *network_tools('eye')]


class RetinatFront(Front):
	"""The MCP server a client talks to. It answers the handshake and the tool list itself, and hands each tool call to
	the `RetinatServer` it loads in the background."""

	name = 'eye-mcp-unlimited'
	label = 'Retinat'
	module = 'browser_use.retinat.server'
	inner: 'RetinatServer | None'

	def __init__(self, cdp_url: str | None = None, network: NetworkRouter | None = None, bridge_port: int | None = None) -> None:
		self.cdp_url = cdp_url
		self.bridge_port = bridge_port
		super().__init__(network)

	def tool_list(self) -> list[types.Tool]:
		return tool_list()

	async def make_server(self, module: ModuleType) -> 'RetinatServer':
		bridge = None
		if self.bridge_port is not None:
			from browser_use.bridge import EXTENSION_DIR, BridgeRelay

			bridge = await BridgeRelay(port=self.bridge_port).start()
			print(
				f'Retinat bridge on {bridge.cdp_url}; load the extension from {EXTENSION_DIR} and share a tab.', file=sys.stderr
			)
		inner = module.RetinatServer(cdp_url=self.cdp_url, network=self._network, bridge=bridge)
		await inner._start_cleanup_task()
		return inner


async def main(cdp_url: str | None = None, network: NetworkRouter | None = None, bridge_port: int | None = None) -> None:
	await serve_until_stopped(RetinatFront(cdp_url=cdp_url, network=network, bridge_port=bridge_port))


def cli() -> None:
	parser = argparse.ArgumentParser(
		prog='eye-mcp-unlimited', description='Retinat MCP server: continuous image/video perception for LLMs (stdio).'
	)
	parser.add_argument(
		'--cdp-url', default=os.environ.get('RETINAT_CDP_URL'), help='attach to a Chrome you started with --remote-debugging-port'
	)
	parser.add_argument(
		'--network',
		choices=[m.value for m in NetworkMode],
		default=None,
		help='route: off (direct), auto (default: Tor only after a network/geo block), always (Tor); env BROWSER_USE_NETWORK',
	)
	parser.add_argument(
		'--exit-country', default=None, help='two-letter Tor exit country, e.g. de (env BROWSER_USE_EXIT_COUNTRY)'
	)
	parser.add_argument(
		'--bridge',
		nargs='?',
		type=int,
		const=9333,
		default=int(os.environ['RETINAT_BRIDGE']) if os.environ.get('RETINAT_BRIDGE') else None,
		metavar='PORT',
		help="use the person's own browser through the Retinat bridge extension (relay port, default 9333; env RETINAT_BRIDGE)",
	)
	args = parser.parse_args()
	network = None
	if args.network or args.exit_country:
		base = NetworkRouter.from_env(default=NetworkMode.AUTO)
		network = NetworkRouter(args.network or base.mode, args.exit_country or base.exit_country)
	asyncio.run(main(args.cdp_url, network, args.bridge))
