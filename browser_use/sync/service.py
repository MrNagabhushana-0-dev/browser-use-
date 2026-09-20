"""Cloud event sync, removed.

This used to POST every agent event — task text, visited URLs, the full serialized
event — to the Browser Use cloud API, tagged with a device id. It was on by default:
`BROWSER_USE_CLOUD_SYNC` falls back to `ANONYMIZED_TELEMETRY`, which defaults to
`'true'`, so a fresh install uploaded runs unless the operator knew to opt out.

The upload path is gone. `CloudSync` remains as an inert object because the agent
constructs one and `cloud_events` reads `.auth_client` off it; keeping the shape means
those call sites need no surgery, and there is no longer any code path that could send
an event even if something set `enabled = True`.

`browser_use.sync.auth` is left in place: it is only reached by the opt-in cloud-browser
backend, and on its own it does nothing but read a local config file.
"""

import logging

from bubus import BaseEvent

logger = logging.getLogger(__name__)


class CloudSync:
	"""Accepts agent events and discards them."""

	def __init__(self, base_url: str | None = None, allow_session_events_for_auth: bool = False):
		self.base_url = base_url or ''
		# Read by browser_use.agent.cloud_events; None means "no device identity".
		self.auth_client = None
		self.session_id: str | None = None
		self.allow_session_events_for_auth = allow_session_events_for_auth
		self.auth_flow_active = False
		# Not configurable: there is no sending code left behind this flag.
		self.enabled = False

	async def handle_event(self, event: BaseEvent) -> None:
		# Session id is still tracked so local code that reads it keeps working.
		if event.event_type == 'CreateAgentSessionEvent' and hasattr(event, 'id'):
			self.session_id = str(event.id)  # type: ignore[attr-defined]

	def set_auth_flow_active(self) -> None:
		return None

	async def authenticate(self, show_instructions: bool = True) -> bool:
		if show_instructions:
			logger.info('Cloud sync has been removed from this build; nothing to authenticate against.')
		return False
