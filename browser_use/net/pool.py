"""One Tor process per exit country.

Tor's `ExitNodes` is a per-process setting and Chromium's SOCKS5 has no authentication, so a
browser cannot pick a country per request: each country needs its own local SOCKS port, and the
browser is launched against the one it wants. This pool starts those processes lazily, reuses
them, and keeps the count small (the volunteer exit pool is shared, and more instances only add
load for diminishing returns).
"""

from __future__ import annotations

import logging
import socket

from browser_use.net.tor import TorConfig, TorTransport

logger = logging.getLogger(__name__)


def _free_port() -> int:
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		sock.bind(('127.0.0.1', 0))
		return sock.getsockname()[1]


class TorPool:
	"""Lazily started, size-capped Tor instances keyed by exit country (None = any exit)."""

	def __init__(self, base: TorConfig | None = None, max_instances: int = 3) -> None:
		assert max_instances >= 1, 'max_instances must be at least 1'
		self.base = base or TorConfig(enabled=True)
		self.max_instances = max_instances
		self._transports: dict[str | None, TorTransport] = {}  # insertion order = least recently used first

	def _config_for(self, country: str | None) -> TorConfig:
		data = self.base.model_dump()
		data.update(enabled=True, exit_country=country)
		if country is not None:
			# A country gets private ports and its own data dir, so it never attaches to a stranger's Tor.
			data.update(socks_port=_free_port(), control_port=_free_port(), data_dir=None)
		return TorConfig(**data)

	async def get(self, country: str | None) -> TorTransport:
		"""A started transport exiting in `country`, evicting the least recently used when over the cap."""
		transport = self._transports.pop(country, None)
		if transport is None:
			while len(self._transports) >= self.max_instances:
				oldest = next(iter(self._transports))
				logger.info('🧅 Stopping the Tor for %s to stay within %d instances', oldest or 'any', self.max_instances)
				await self._transports.pop(oldest).stop()
			transport = TorTransport(self._config_for(country))
		self._transports[country] = transport  # most recently used goes last
		try:
			await transport.start()
		except BaseException:
			self._transports.pop(country, None)
			raise
		return transport

	def peek(self, country: str | None) -> TorTransport | None:
		"""The running transport for `country`, without starting, restarting or reordering anything."""
		return self._transports.get(country)

	async def stop_all(self) -> None:
		transports, self._transports = list(self._transports.values()), {}
		for transport in transports:
			await transport.stop()
