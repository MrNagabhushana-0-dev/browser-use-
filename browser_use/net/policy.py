"""Network policy: when a browser goes through Tor, and what to do when a page refuses.

Three modes, chosen per session and changeable by a person (a UI toggle calls the same method) or
by an agent (the `*_network` MCP tools):

- `off`:    direct connection, nothing touches Tor.
- `auto`:   direct first. On a clear network failure or a "not available in your country" page,
            retry once through Tor. Never on a bot wall.
- `always`: every request through Tor.

What this is for: reaching public pages that a network censors or geo-fences, for research. What it
does not do: defeat a site's bot detection. Tor exit addresses are on public block lists, so Google,
YouTube and Cloudflare-fronted sites challenge them more, not less. A human-verification wall is
classified as `walled`, reported, and never retried, rotated around or solved.

Two facts about Chromium shape the design. Its SOCKS5 has no authentication, so a country is chosen
by which local SOCKS port the browser is launched against (see `TorPool`). And proxy settings belong
to the browser, so changing route means relaunching it; the MCP servers do that.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from enum import Enum
from typing import Literal
from urllib.parse import urlparse

from pydantic import ValidationError

from browser_use.net.pool import TorPool
from browser_use.net.tor import (
	TorConfig,
	TorUnavailableError,
	should_fall_back,
	tor_chromium_args,
	tor_version_is_supported,
)

logger = logging.getLogger(__name__)

Outcome = Literal['ok', 'network_error', 'geo_blocked', 'walled']

_GEO_BLOCK = re.compile(
	r'(not|isn.t|is n.t)\s+available\s+in\s+your\s+(country|region|location|area)'
	r'|unavailable\s+in\s+your\s+(country|region|location|area)'
	r'|(blocked|restricted)\s+in\s+your\s+(country|region)'
	r'|not\s+available\s+(to\s+you\s+)?in\s+(this|your)\s+(country|region)'
	r'|451\s+unavailable\s+for\s+legal\s+reasons',
	re.IGNORECASE,
)

_LOOPBACK = {'localhost', '127.0.0.1', '::1', '[::1]'}
_HISTORY_LIMIT = 20


class NetworkMode(str, Enum):
	OFF = 'off'
	AUTO = 'auto'
	ALWAYS = 'always'


class NetworkPolicyError(RuntimeError):
	"""A request the current network policy refuses, or a setting it can't honour."""


def classify_navigation(
	error_text: str = '',
	*,
	url: str = '',
	title: str = '',
	text: str = '',
	frames: list[str] | None = None,
	status: int | None = None,
) -> Outcome:
	"""Sort a navigation result into `ok`, `network_error`, `geo_blocked` or `walled`.

	A bot wall wins over everything: it is reported and never retried, whatever else the page says.
	"""
	from browser_use.explore import walls

	if walls.detect(url, title, text, frames):
		return 'walled'
	if status == 451 or _GEO_BLOCK.search(f'{title}\n{(text or "")[:1500]}'):
		return 'geo_blocked'
	if error_text and should_fall_back(error_text):
		return 'network_error'
	return 'ok'


class NetworkRouter:
	"""The session's route: direct, or Tor exiting in a chosen country."""

	def __init__(
		self,
		mode: NetworkMode | str = NetworkMode.OFF,
		exit_country: str | None = None,
		*,
		tor: TorConfig | None = None,
		allow_http: bool = False,
		max_instances: int = 3,
	) -> None:
		self.mode = NetworkMode(mode)
		self.exit_country = self._country(exit_country)
		self.allow_http = allow_http
		self._pool = TorPool(tor or TorConfig(enabled=True), max_instances=max_instances)
		self._engaged = False
		self.last_outcome: Outcome | None = None
		self.last_error: str | None = None
		self.history: list[dict[str, str]] = []

	@classmethod
	def from_env(cls, default: NetworkMode = NetworkMode.OFF) -> NetworkRouter:
		"""Read BROWSER_USE_NETWORK (off|auto|always) and BROWSER_USE_EXIT_COUNTRY (e.g. de)."""
		raw = os.environ.get('BROWSER_USE_NETWORK', '').strip().lower()
		try:
			mode = NetworkMode(raw) if raw else default
		except ValueError as e:
			raise NetworkPolicyError(f'BROWSER_USE_NETWORK must be off, auto or always, not {raw!r}') from e
		return cls(mode, os.environ.get('BROWSER_USE_EXIT_COUNTRY') or None)

	@staticmethod
	def _country(code: str | None) -> str | None:
		if code is None or code.strip().lower() in {'', 'any'}:
			return None
		try:
			return TorConfig(exit_country=code).exit_country
		except ValidationError as e:
			raise NetworkPolicyError(f'exit_country must be a two-letter country code such as "de", not {code!r}') from e

	# --- state -------------------------------------------------------------

	@property
	def uses_tor(self) -> bool:
		return self.mode is NetworkMode.ALWAYS or (self.mode is NetworkMode.AUTO and self._engaged)

	@property
	def route(self) -> str:
		return f'tor:{(self.exit_country or "any").upper()}' if self.uses_tor else 'direct'

	def _record(self, what: str, reason: str = '') -> None:
		self.history.append({'at': time.strftime('%H:%M:%S'), 'what': what, 'reason': reason})
		del self.history[:-_HISTORY_LIMIT]

	# --- changing the route ------------------------------------------------

	async def set_network(self, mode: NetworkMode | str, exit_country: str | None = None, reason: str = '') -> str:
		"""Set the mode and exit country (`None` or `any` = no preference). Returns the new status line.

		`always` starts Tor now, so a missing Tor fails here rather than mid-task. Raises
		NetworkPolicyError for a bad mode or country or a Tor that can't start; nothing changes then.
		"""
		try:
			new_mode = NetworkMode(mode)
		except ValueError as e:
			raise NetworkPolicyError(f'mode must be off, auto or always, not {mode!r}') from e
		country = self._country(exit_country)
		if new_mode is NetworkMode.ALWAYS:
			await self._transport(country)  # fail before changing anything
		self.mode, self.exit_country, self._engaged = new_mode, country, False
		self._record(f'set {new_mode.value}/{country or "any"}', reason)
		return self.summary()

	async def _transport(self, country: str | None):
		try:
			transport = await self._pool.get(country)
		except TorUnavailableError as e:
			self.last_error = str(e)
			raise NetworkPolicyError(str(e)) from e
		try:
			version = await transport.tor_version()
		except Exception:
			version = None  # the control port is optional; the proxy works without it
		if version and not tor_version_is_supported(version[1]):
			logger.warning('🧅 Tor %s is older than 0.4.8.13; upgrade it (Conflux bug fix)', version[0])
		return transport

	def wants_fallback(self, outcome: Outcome) -> bool:
		"""Whether `auto` should retry through Tor: a network failure or geo-block, never a bot wall."""
		return self.mode is NetworkMode.AUTO and not self._engaged and outcome in ('network_error', 'geo_blocked')

	async def engage(self, reason: str = '') -> bool:
		"""In `auto`, switch to Tor. False (with `last_error` set) when Tor can't be started."""
		if self.mode is not NetworkMode.AUTO:
			return self.uses_tor
		try:
			await self._transport(self.exit_country)
		except NetworkPolicyError:
			self._record('fallback failed: Tor unavailable', reason)
			return False
		self._engaged = True
		self._record(f'fell back to tor:{(self.exit_country or "any").upper()}', reason)
		return True

	def note_outcome(self, outcome: Outcome) -> None:
		self.last_outcome = outcome

	# --- applying the route to a browser ------------------------------------

	async def session_kwargs(self) -> dict:
		"""BrowserProfile fields for the current route: empty when direct.

		Through Tor the browser gets the SOCKS5 proxy (DNS stays proxy-side), flags that stop QUIC, WebRTC
		and IPv6 going around it, a throwaway profile (no cookies or logins carried in) and no extensions.
		"""
		if not self.uses_tor:
			return {}
		transport = await self._transport(self.exit_country)
		return {
			'proxy': transport.proxy_settings(),
			'args': tor_chromium_args(),
			'user_data_dir': None,
			'enable_default_extensions': False,
		}

	def check_url(self, url: str) -> None:
		"""Refuse plain http:// over Tor: the exit relay can read and alter it. Loopback isn't proxied."""
		if not self.uses_tor or self.allow_http:
			return
		parsed = urlparse(url)
		if parsed.scheme == 'http' and (parsed.hostname or '') not in _LOOPBACK:
			raise NetworkPolicyError(
				'Plain http:// through Tor is visible to (and editable by) the exit relay. Use https://, '
				'or set allow_http=True if you accept that for this public page.'
			)

	# --- reporting ---------------------------------------------------------

	def summary(self) -> str:
		country = (self.exit_country or 'any').upper()
		if self.mode is NetworkMode.AUTO and not self._engaged:
			return f'Network: auto (direct now; falls back to Tor, exit {country}, on a network or geo block). Route: direct.'
		return f'Network: {self.mode.value}, exit {country}. Route: {self.route}.'

	async def status(self) -> str:
		"""A multi-line report: mode, route, the exit Tor says we use, and recent events."""
		lines = [self.summary()]
		if self.uses_tor:
			try:
				transport = await self._pool.get(self.exit_country)
				exit_info = await transport.observed_exit()
			except Exception as e:
				exit_info = None
				lines.append(f'Exit: not readable ({e})')
			if exit_info:
				seen = (exit_info.country or 'unknown').upper()
				lines.append(f'Exit: {exit_info.ip or "unknown ip"} in {seen} (Tor GeoIP, approximate).')
				if self.exit_country and exit_info.country and exit_info.country != self.exit_country:
					lines.append(
						f'WARNING: asked for {self.exit_country.upper()} but Tor reports {seen}. Do not assume the country.'
					)
		elif self.mode is not NetworkMode.OFF and shutil.which('tor') is None:
			lines.append('Tor is not installed here (and none is running), so a fallback would fail. Install Tor to enable it.')
		if self.last_outcome:
			lines.append(f'Last page: {self.last_outcome}.')
		if self.last_error:
			lines.append(f'Last Tor problem: {self.last_error}')
		for event in self.history[-3:]:
			lines.append(f'  {event["at"]} {event["what"]}' + (f' ({event["reason"]})' if event['reason'] else ''))
		return '\n'.join(lines)

	async def close(self) -> None:
		await self._pool.stop_all()
		self._engaged = False
