"""Tor control-port reply parsing, kept pure so it can be tested without a Tor.

The control protocol (https://spec.torproject.org/control-spec/) answers `GETINFO` with lines like
`250-key=value`, `250+key=` followed by data lines and a lone `.`, and a final `250 OK`. These
helpers turn such replies into the facts the router reports: which relay is the exit of the circuit
carrying our traffic, its address, what country Tor's GeoIP says it is in, and Tor's version.
"""

from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, ConfigDict

# Conflux had a client bug fixed in 0.4.8.13 that caused extra circuit building and load.
MIN_TOR_VERSION: tuple[int, ...] = (0, 4, 8, 13)

_FINAL_OK = re.compile(r'^250 ')
_ERROR = re.compile(r'^[45]\d\d[ -]')


class ExitInfo(BaseModel):
	"""The exit relay of a circuit, as Tor itself reports it (GeoIP is approximate)."""

	model_config = ConfigDict(extra='forbid')

	fingerprint: str
	ip: str | None = None
	country: str | None = None


async def read_reply(reader: asyncio.StreamReader) -> list[str]:
	"""Read one control-port reply: every line up to and including the `250 ...` or error line."""
	lines: list[str] = []
	in_data = False  # inside a `250+key=` block, whose lines (circuit ids like "250 BUILT") must not end the reply
	while True:
		raw = await reader.readline()
		if not raw:
			break
		line = raw.decode(errors='replace').rstrip('\r\n')
		lines.append(line)
		if in_data:
			in_data = line != '.'
		elif line.startswith('250+'):
			in_data = True
		elif _FINAL_OK.match(line) or _ERROR.match(line):
			break
	return lines


def _fingerprint(hop: str) -> str:
	# A path hop looks like `$FINGERPRINT~nickname` or `$FINGERPRINT=nickname`.
	return re.split(r'[~=]', hop.lstrip('$'), maxsplit=1)[0]


def parse_stream_circuit(lines: list[str]) -> str | None:
	"""The circuit id of the newest SUCCEEDED stream (`GETINFO stream-status`), if any."""
	found: str | None = None
	for line in lines:
		parts = line.removeprefix('250+stream-status=').removeprefix('250-stream-status=').split()
		if len(parts) >= 3 and parts[1] == 'SUCCEEDED' and parts[0].isdigit():
			found = parts[2]
	return found


def parse_circuit_exit(lines: list[str], prefer_circuit: str | None = None) -> str | None:
	"""The exit fingerprint of a BUILT general-purpose circuit (`GETINFO circuit-status`).

	Uses `prefer_circuit` (the circuit carrying our stream) when it is built, otherwise the most
	recently created general circuit.
	"""
	best: tuple[str, str] | None = None
	for line in lines:
		parts = line.removeprefix('250+circuit-status=').removeprefix('250-circuit-status=').split()
		if len(parts) < 3 or parts[1] != 'BUILT' or 'PURPOSE=GENERAL' not in line:
			continue
		fingerprint = _fingerprint(parts[2].split(',')[-1])
		if prefer_circuit is not None and parts[0] == prefer_circuit:
			return fingerprint
		created = next((p.removeprefix('TIME_CREATED=') for p in parts if p.startswith('TIME_CREATED=')), '')
		if best is None or created >= best[0]:
			best = (created, fingerprint)
	return best[1] if best else None


def parse_router_ip(lines: list[str]) -> str | None:
	"""The address from a router status entry (`GETINFO ns/id/<fp>`): `r nick id digest date time IP ...`."""
	for line in lines:
		if line.startswith('r '):
			parts = line.split()
			if len(parts) >= 7:
				return parts[6]
	return None


def parse_country(lines: list[str]) -> str | None:
	"""The two-letter code from `GETINFO ip-to-country/<ip>`; Tor answers `??` when it doesn't know."""
	for line in lines:
		match = re.match(r'^250[- ]ip-to-country/[^=]+=(\S+)', line)
		if match:
			code = match.group(1).lower()
			return None if code == '??' else code
	return None


def parse_version(lines: list[str]) -> tuple[str, tuple[int, ...]] | None:
	"""Tor's version text and its numeric tuple from `GETINFO version`."""
	for line in lines:
		match = re.match(r'^250[- ]version=(\d+(?:\.\d+)*)', line)
		if match:
			return match.group(1), tuple(int(n) for n in match.group(1).split('.'))
	return None
