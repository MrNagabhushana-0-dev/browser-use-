"""Retinat: an MCP server that gives a model eyes. See `server.py`; the process starts from `front.py`.

Imported lazily: the front answers a client's handshake from light modules before the server is loaded."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the real names for the type checker; at run time they load on first use, below
	from browser_use.retinat.front import cli
	from browser_use.retinat.server import RetinatServer, main

__all__ = ['RetinatServer', 'cli', 'main']


def __getattr__(name):
	if name == 'cli':
		from browser_use.retinat.front import cli

		return cli
	if name in ('RetinatServer', 'main'):
		from browser_use.retinat import server

		return getattr(server, name)
	raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
