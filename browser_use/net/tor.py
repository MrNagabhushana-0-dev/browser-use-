"""Optional Tor transport: a censorship-resistant SOCKS5 path for the browser.

What this is for
----------------
Some content is unreachable from a given network because a country or a network
operator censors it, or because it is geo-restricted. For research and education
in that situation, routing the browser through the Tor network gets you an exit
somewhere the block does not apply. This module manages a `tor` process (or
attaches to one you already run) and hands its SOCKS5 port to
`BrowserProfile.proxy`, which already speaks SOCKS5.

What this is NOT for
--------------------
This is not a way to defeat a site's bot detection. The opposite is true: Tor
exit-node addresses sit on public block lists, so Google, YouTube and similar
score them as high-risk and challenge them *more*, not less. If a site shows a
CAPTCHA or an "unusual traffic" wall, that is reported and left to a human; Tor
does not change that, and `should_fall_back()` deliberately never retries a bot
wall. For a site that simply blocks an address range (YouTube on a datacenter
IP), the honest paths are the person's own browser and connection
(`--cdp-url`), or an alternative front end such as Invidious or Piped.

Design
------
- `TorConfig` is the typed, opt-in configuration. `enabled` is False by default.
- `TorTransport.start()` reuses a Tor already listening on the SOCKS port, else
  launches one from a generated torrc and waits for "Bootstrapped 100%".
- `TorTransport.proxy_settings()` returns the `ProxySettings` to put on a
  `BrowserProfile`.
- `TorTransport.new_circuit()` asks Tor for a fresh circuit (the NEWNYM signal)
  over the control port, subject to Tor's ~10s cooldown.
- `should_fall_back(error_text)` classifies a navigation error as a
  network/censorship failure worth retrying through Tor, versus a bot wall that
  must not be retried.

Everything here is best-effort and honest about its limits: circuit rotation is
skipped (not faked) when the control port is unreachable, and the transport
still works as a plain SOCKS proxy in that case.
"""

from __future__ import annotations

import asyncio
import binascii
import logging
import re
import shutil
import socket
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from browser_use.browser.profile import ProxySettings

logger = logging.getLogger(__name__)

# Chromium reaches Tor over SOCKS5. We keep DNS resolution on the proxy side so
# the local resolver never sees the target host; Chromium's --proxy-server does
# this for socks5:// already, and the control client below asks tor to as well.
_DEFAULT_SOCKS_PORT = 9050
_DEFAULT_CONTROL_PORT = 9051
_BOOTSTRAP_RE = re.compile(r'Bootstrapped (\d+)%')

# Navigation error fragments that mean "the network path failed / was blocked",
# for which trying a different exit through Tor is reasonable. Lower-cased match.
_FALLBACK_ERROR_MARKERS: tuple[str, ...] = (
	'err_connection_reset',
	'err_connection_refused',
	'err_connection_closed',
	'err_connection_timed_out',
	'err_timed_out',
	'err_name_not_resolved',
	'err_address_unreachable',
	'err_network_access_denied',
	'err_blocked_by_administrator',
	'err_blocked_by_response',
	'err_socks_connection_failed',
	'err_tunnel_connection_failed',
	'err_empty_response',
	'net::err_cert_authority_invalid',  # some censors MITM with a bogus cert
	'451',  # HTTP 451: unavailable for legal reasons
)

# Fragments that mean "a human-verification / anti-bot wall", which Tor cannot
# and must not be used to bypass. Presence of any of these vetoes fallback.
_BOT_WALL_MARKERS: tuple[str, ...] = (
	'captcha',
	'recaptcha',
	'hcaptcha',
	'are you a robot',
	'confirm you',
	'unusual traffic',
	'/sorry/',
	'cf-challenge',
	'just a moment',
	'attention required',
	'access denied',
	'verify you are human',
)


class TorUnavailableError(RuntimeError):
	"""Raised when Tor is requested but no running Tor and no tor binary exist."""


class TorControlError(RuntimeError):
	"""Raised when the Tor control port is unreachable or rejects authentication."""


def _validate_country(code: str | None) -> str | None:
	if code is None:
		return None
	code = code.strip().lower()
	assert len(code) == 2 and code.isalpha(), f'exit_country must be a 2-letter code, got {code!r}'
	return code


class TorConfig(BaseModel):
	"""Typed, opt-in configuration for the Tor transport.

	`enabled` is False by default: nothing routes through Tor unless a caller asks.
	"""

	model_config = ConfigDict(extra='forbid', validate_by_name=True, validate_by_alias=True)

	enabled: bool = Field(default=False, description='Route the browser through Tor. Off by default.')
	socks_port: int = Field(default=_DEFAULT_SOCKS_PORT, ge=1, le=65535, description="Tor's SOCKS5 port.")
	control_port: int = Field(default=_DEFAULT_CONTROL_PORT, ge=1, le=65535, description="Tor's control port for NEWNYM.")
	control_password: str | None = Field(default=None, description='Control-port password, if the running Tor uses one.')
	cookie_auth_path: Path | None = Field(default=None, description='Path to control_auth_cookie for cookie auth.')
	tor_binary: str | None = Field(default=None, description="Path to the 'tor' executable; auto-detected if omitted.")
	data_dir: Path | None = Field(default=None, description='DataDirectory for a launched Tor; a temp dir if omitted.')
	bridges: list[str] = Field(default_factory=list, description='obfs4/webtunnel bridge lines for censored networks.')
	exit_country: Annotated[str | None, AfterValidator(_validate_country)] = Field(
		default=None, description="Two-letter country to prefer for the exit, e.g. 'de'."
	)
	bootstrap_timeout_s: float = Field(default=90.0, gt=0, description='How long to wait for Tor to bootstrap.')
	new_circuit_cooldown_s: float = Field(default=10.0, ge=0, description="Tor's minimum gap between NEWNYM signals.")


class TorTransport:
	"""Manages a Tor SOCKS5 endpoint for a BrowserProfile.

	Usage::

	    tor = TorTransport(TorConfig(enabled=True))
	    await tor.start()
	    profile = BrowserProfile(proxy=tor.proxy_settings())
	    ...
	    await tor.new_circuit()  # fresh exit, subject to the ~10s cooldown
	    await tor.stop()
	"""

	def __init__(self, config: TorConfig | None = None) -> None:
		self.config = config or TorConfig()
		self._process: asyncio.subprocess.Process | None = None
		self._owns_process = False
		self._bootstrapped = False
		self._data_dir_created: Path | None = None
		self._last_newnym: float = 0.0

	# --- lifecycle ---------------------------------------------------------

	async def start(self) -> None:
		"""Ensure a bootstrapped Tor is reachable on the SOCKS port.

		Reuses an already-running Tor if one is listening; otherwise launches one.
		"""
		assert self.config.enabled, 'TorTransport.start() called while config.enabled is False'

		if await self._socks_is_open():
			logger.info(self._log_using_existing())
			self._bootstrapped = True
			return

		binary = self.config.tor_binary or shutil.which('tor')
		if not binary:
			raise TorUnavailableError(
				'Tor is enabled but no Tor is running on the SOCKS port and no "tor" binary was found. '
				'Install Tor (e.g. `apt install tor` / `brew install tor`) or point TorConfig.tor_binary at it, '
				'or run your own Tor and browser_use will attach to it.'
			)

		await self._launch(binary)

	async def _launch(self, binary: str) -> None:
		data_dir = self._resolve_data_dir()
		torrc = self._torrc(data_dir)
		torrc_path = data_dir / 'torrc'
		torrc_path.write_text(torrc)

		logger.info(self._log_launching(binary))
		self._process = await asyncio.create_subprocess_exec(
			binary,
			'-f',
			str(torrc_path),
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.STDOUT,
		)
		self._owns_process = True

		try:
			await asyncio.wait_for(self._await_bootstrap(), timeout=self.config.bootstrap_timeout_s)
		except TimeoutError as e:
			await self.stop()
			raise TorUnavailableError(
				f'Tor did not bootstrap within {self.config.bootstrap_timeout_s:.0f}s. '
				'On a censored network you usually need bridges (set TorConfig.bridges).'
			) from e
		self._bootstrapped = True
		logger.info(self._log_bootstrapped())

	async def _await_bootstrap(self) -> None:
		assert self._process is not None and self._process.stdout is not None
		while True:
			raw = await self._process.stdout.readline()
			if not raw:
				raise TorUnavailableError('Tor exited before bootstrapping; check the torrc and bridges.')
			line = raw.decode(errors='replace').rstrip()
			match = _BOOTSTRAP_RE.search(line)
			if match:
				logger.debug('[tor] %s', line)
				if int(match.group(1)) >= 100:
					return

	async def stop(self) -> None:
		"""Stop a Tor we launched. A Tor we merely attached to is left alone."""
		if self._process is not None and self._owns_process:
			if self._process.returncode is None:
				self._process.terminate()
				try:
					await asyncio.wait_for(self._process.wait(), timeout=10)
				except TimeoutError:
					self._process.kill()
					await self._process.wait()
		self._process = None
		self._bootstrapped = False
		if self._data_dir_created is not None:
			import shutil as _sh

			_sh.rmtree(self._data_dir_created, ignore_errors=True)
			self._data_dir_created = None

	# --- proxy wiring ------------------------------------------------------

	def proxy_settings(self) -> ProxySettings:
		"""The ProxySettings to hand to a BrowserProfile so its Chromium uses Tor."""
		assert self._bootstrapped, 'call await start() before proxy_settings()'
		return ProxySettings(server=f'socks5://127.0.0.1:{self.config.socks_port}')

	# --- circuit rotation --------------------------------------------------

	async def new_circuit(self) -> bool:
		"""Request a fresh Tor circuit (new exit) via the NEWNYM control signal.

		Returns True if the signal was accepted. Respects Tor's cooldown by
		waiting out the remainder before signalling. Raises TorControlError if
		the control port is unreachable or authentication fails; callers that
		treat rotation as best-effort should catch it.
		"""
		loop = asyncio.get_event_loop()
		elapsed = loop.time() - self._last_newnym
		if elapsed < self.config.new_circuit_cooldown_s:
			await asyncio.sleep(self.config.new_circuit_cooldown_s - elapsed)

		reader, writer = await self._open_control()
		try:
			await self._authenticate(reader, writer)
			writer.write(b'SIGNAL NEWNYM\r\n')
			await writer.drain()
			resp = await reader.readline()
			if not resp.startswith(b'250'):
				raise TorControlError(f'Tor refused NEWNYM: {resp.decode(errors="replace").strip()}')
			self._last_newnym = loop.time()
			logger.info(self._log_new_circuit())
			return True
		finally:
			writer.write(b'QUIT\r\n')
			try:
				await writer.drain()
			except (ConnectionError, OSError):
				pass
			writer.close()

	async def _open_control(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
		try:
			return await asyncio.open_connection('127.0.0.1', self.config.control_port)
		except (ConnectionError, OSError) as e:
			raise TorControlError(
				f'Tor control port {self.config.control_port} is unreachable; circuit rotation is unavailable. '
				'The transport still works as a plain SOCKS proxy.'
			) from e

	async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
		# Prefer an explicit password, then a cookie file, then null auth.
		if self.config.control_password is not None:
			token = self.config.control_password.replace('"', '\\"')
			writer.write(f'AUTHENTICATE "{token}"\r\n'.encode())
		elif (cookie := self._cookie_bytes()) is not None:
			writer.write(f'AUTHENTICATE {binascii.hexlify(cookie).decode()}\r\n'.encode())
		else:
			writer.write(b'AUTHENTICATE\r\n')
		await writer.drain()
		resp = await reader.readline()
		if not resp.startswith(b'250'):
			raise TorControlError(
				f'Tor control authentication failed: {resp.decode(errors="replace").strip()}. '
				'Set TorConfig.control_password or TorConfig.cookie_auth_path.'
			)

	def _cookie_bytes(self) -> bytes | None:
		path = self.config.cookie_auth_path
		if path is None and self._data_dir_created is not None:
			candidate = self._data_dir_created / 'control_auth_cookie'
			if candidate.exists():
				path = candidate
		if path is not None and Path(path).exists():
			return Path(path).read_bytes()
		return None

	# --- helpers -----------------------------------------------------------

	async def _socks_is_open(self) -> bool:
		return await asyncio.get_event_loop().run_in_executor(None, self._port_open, self.config.socks_port)

	@staticmethod
	def _port_open(port: int) -> bool:
		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
			sock.settimeout(0.5)
			return sock.connect_ex(('127.0.0.1', port)) == 0

	def _resolve_data_dir(self) -> Path:
		if self.config.data_dir is not None:
			path = Path(self.config.data_dir)
			path.mkdir(parents=True, exist_ok=True)
			return path
		import tempfile

		path = Path(tempfile.mkdtemp(prefix='browser_use_tor_'))
		self._data_dir_created = path
		return path

	def _torrc(self, data_dir: Path) -> str:
		lines = [
			f'SocksPort {self.config.socks_port}',
			f'ControlPort {self.config.control_port}',
			'CookieAuthentication 1',
			f'DataDirectory {data_dir}',
		]
		if self.config.exit_country:
			lines.append(f'ExitNodes {{{self.config.exit_country}}}')
			lines.append('StrictNodes 1')
		if self.config.bridges:
			lines.append('UseBridges 1')
			# obfs4 needs a pluggable-transport binary; name it if present.
			obfs4 = shutil.which('obfs4proxy') or shutil.which('lyrebird')
			if obfs4:
				lines.append(f'ClientTransportPlugin obfs4 exec {obfs4}')
			for bridge in self.config.bridges:
				lines.append(f'Bridge {bridge}')
		return '\n'.join(lines) + '\n'

	# --- logging (kept out of the main logic per repo style) ---------------

	def _log_using_existing(self) -> str:
		return f'🧅 Using the Tor already listening on 127.0.0.1:{self.config.socks_port}'

	def _log_launching(self, binary: str) -> str:
		return f'🧅 Launching Tor ({binary}) on SOCKS 127.0.0.1:{self.config.socks_port}'

	def _log_bootstrapped(self) -> str:
		where = f' via {len(self.config.bridges)} bridge(s)' if self.config.bridges else ''
		exit_note = f', exit in {self.config.exit_country.upper()}' if self.config.exit_country else ''
		return f'🧅 Tor bootstrapped{where}{exit_note}'

	def _log_new_circuit(self) -> str:
		return '🧅 Requested a fresh Tor circuit (new exit)'


def should_fall_back(error_text: str) -> bool:
	"""Whether a navigation error is a network/censorship failure worth a Tor retry.

	True for connection-level and legal-block errors. Always False when the text
	shows a human-verification wall (CAPTCHA, "unusual traffic", Cloudflare
	challenge): Tor cannot bypass those and must not be used to try. Also False
	for an empty string.
	"""
	assert isinstance(error_text, str)
	low = error_text.lower()
	if any(marker in low for marker in _BOT_WALL_MARKERS):
		return False
	return any(marker in low for marker in _FALLBACK_ERROR_MARKERS)
