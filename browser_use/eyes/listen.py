"""Listening to one tab's CDP events without taking the slot from anyone else.

cdp-use keeps one callback per event method; the downloads and HAR watchdogs, the explorer and the retina all live on
some of them. A TabListener chains onto whoever holds each slot, filters to its own tab's session, and puts the
previous holders back when it stops.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class TabListener:
	"""Base for passive per-tab logs: subclasses list their events in `events()` and enable domains in `enable()`."""

	def __init__(self, browser_session: Any):
		self.browser_session = browser_session
		self._session_id: str | None = None
		self._cdp: Any = None
		self._restore: list[tuple[str, Any, Any]] = []

	@property
	def running(self) -> bool:
		return bool(self._restore)

	def events(self) -> list[tuple[str, Any]]:
		raise NotImplementedError

	async def enable(self, cdp: Any) -> None:
		"""Turn on the domains the events come from. Left on at stop: others use them too."""

	async def start(self, target_id: str | None = None) -> None:
		if self.running:
			return
		cdp = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
		self._cdp, self._session_id = cdp, cdp.session_id
		for method, fn in self.events():
			self._chain(method, fn)
		await self.enable(cdp)

	async def stop(self) -> None:
		registry = self.browser_session.cdp_client._event_registry
		for method, incumbent, ours in reversed(self._restore):
			if registry._handlers.get(method) is ours:
				if incumbent is not None:
					registry.register(method, incumbent)
				else:
					registry.unregister(method)
		self._restore.clear()

	def _chain(self, method: str, fn: Any) -> None:
		registry = self.browser_session.cdp_client._event_registry
		incumbent = registry._handlers.get(method)

		def both(event: Any, session_id: str | None = None) -> Any:
			if session_id == self._session_id:
				try:
					fn(event)
				except Exception as e:
					logger.debug(f'{type(self).__name__}: {method} failed: {e}')
			return incumbent(event, session_id) if incumbent is not None else None

		self._restore.append((method, incumbent, both))
		registry.register(method, both)
