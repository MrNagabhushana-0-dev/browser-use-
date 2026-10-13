"""One `Page.captureScreenshot` at a time per browser connection.

Two captures in flight on one tab can hand each other's picture back. Found with the eyes' compositor sight of a
cross-origin film (captures of the film's box) while reflex rules captured the whole viewport: 20-24 of ~30 of the
eyes' pictures came back as the page, shrunk and tiled; 0 of ~30 with the captures taken one at a time. Every capture
in the library goes through `capture_screenshot`, which holds a lock per CDP client, so per browser connection (a lock
lives in one event loop, and so does a client).
"""

import asyncio
import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from cdp_use import CDPClient

_locks: 'weakref.WeakKeyDictionary[CDPClient, asyncio.Lock]' = weakref.WeakKeyDictionary()


def capture_lock(client: 'CDPClient') -> asyncio.Lock:
	"""The lock every capture through this client holds."""
	lock = _locks.get(client)
	if lock is None:
		lock = _locks[client] = asyncio.Lock()
	return lock


async def capture_screenshot(client: 'CDPClient', params: Any, session_id: str | None) -> Any:
	"""`Page.captureScreenshot` with `params` on `session_id`, never overlapping another capture through `client`."""
	async with capture_lock(client):
		return await client.send.Page.captureScreenshot(params=params, session_id=session_id)
