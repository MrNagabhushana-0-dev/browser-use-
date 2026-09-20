"""Keeps the WebMCP bridge installed on every tab and its tool listing fresh."""

from typing import ClassVar

from bubus import BaseEvent
from pydantic import PrivateAttr

from browser_use.browser.events import (
	BrowserConnectedEvent,
	NavigationCompleteEvent,
	TabClosedEvent,
	TabCreatedEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog
from browser_use.webmcp.service import WebMCPService


class WebMCPWatchdog(BaseWatchdog):
	"""Installs the WebMCP bridge per target and expires its cache on navigation.

	Installation has to happen once per target rather than once per session: an init
	script is registered against a CDP target, so a tab opened later would otherwise
	load its documents without the agent-facing API and every WebMCP-aware site in
	that tab would silently register nothing.

	Nothing here is on the agent's critical path — discovery itself runs lazily when
	browser state is built, so a slow or hostile page cannot stall the event bus.
	"""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		BrowserConnectedEvent,
		TabCreatedEvent,
		TabClosedEvent,
		NavigationCompleteEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	_service: WebMCPService | None = PrivateAttr(default=None)

	@property
	def service(self) -> WebMCPService:
		if self._service is None:
			self._service = WebMCPService(self.browser_session)
		return self._service

	async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
		"""Instrument whatever tab the session opens with."""
		try:
			await self.service.install()
		except Exception as e:
			self.logger.debug(f'🧩 WebMCP install on connect failed: {type(e).__name__}: {e}')

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		try:
			await self.service.install(event.target_id)
		except Exception as e:
			self.logger.debug(f'🧩 WebMCP install on new tab failed: {type(e).__name__}: {e}')

	async def on_TabClosedEvent(self, event: TabClosedEvent) -> None:
		self.service.forget(event.target_id)

	async def on_NavigationCompleteEvent(self, event: NavigationCompleteEvent) -> None:
		"""Tools belong to a document, so a new one invalidates the previous listing."""
		self.service.invalidate(event.target_id)
