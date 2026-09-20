"""Telemetry, removed.

This package used to ship an anonymized-analytics client: a PostHog project key, an
EU ingest host, a device id persisted under the config directory, and — when that file
could not be written — a fallback fingerprint hashed from the machine's MAC address and
hostname. Every agent run, every MCP tool call and every registry action reported to it.

All of that is gone. What remains is an inert object with the same shape, because
`capture()` and `flush()` are called from roughly forty places across the agent, the
registry, the MCP client and the MCP server. Keeping the surface means those call sites
need no surgery and no future upstream merge silently re-enables collection: there is
nothing left to re-enable, and the posthog dependency is no longer installed.

Nothing here touches the network, the filesystem, or the machine's identity.
"""

import logging

from browser_use.telemetry.views import BaseTelemetryEvent
from browser_use.utils import singleton

logger = logging.getLogger(__name__)

# Kept so that code reading an id gets something stable and meaningless, rather than
# a value derived from the machine.
ANONYMOUS_USER_ID = 'local'


def get_or_create_device_id() -> str:
	"""No device is identified any more. Retained so importers do not break."""
	return ANONYMOUS_USER_ID


@singleton
class ProductTelemetry:
	"""Accepts telemetry calls and discards them."""

	def capture(self, event: BaseTelemetryEvent) -> None:
		# Debug-level only: useful when tracing what the agent did locally, and it never
		# leaves the process.
		logger.debug(f'Telemetry disabled, discarding event: {event.name}')

	def flush(self) -> None:
		return None

	@property
	def user_id(self) -> str:
		return ANONYMOUS_USER_ID
