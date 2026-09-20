"""Learn a site's tool surface once, not once per session.

Synthesis is cheap but not free — one script, one round trip, a little reasoning about
what the controls mean. Doing it again on every run of every agent against the same site
is waste, and worse, it throws away the one thing that makes a synthesized tool
trustworthy: the record that it has actually been run and worked.

So manifests persist per origin, carrying their verification state with them. The second
agent to visit a site inherits what the first one established.

The hard part is staleness. A cached tool surface for a page that has since been
redesigned is worse than no cache, because it fails in a way that looks like the agent
being wrong rather than the cache being old. Each manifest therefore stores a fingerprint
of the affordances it was built from; when the page no longer matches, the manifest is
rebuilt instead of trusted.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from browser_use.synthesis.views import SiteManifest

logger = logging.getLogger(__name__)

# Origins kept. Past this the oldest go; a manifest costs little but not nothing.
MAX_ORIGINS = 200


def fingerprint(affordances: dict[str, Any]) -> str:
	"""A short hash of a page's shape, used to notice a redesign.

	Built from what things are *called*, not from counts or positions: a site that adds a
	row to a table has not changed its tool surface, while one that renames "Sign in" to
	"Log in" has. Text is what the locators bind to, so text is what invalidates them.
	"""
	parts: list[str] = []
	for form in affordances.get('forms') or []:
		controls = ','.join(sorted(str(c.get('name', '')) for c in (form.get('controls') or [])))
		parts.append(f'form:{form.get("name", "")}:{controls}')
	for key in ('buttons', 'views', 'toggles'):
		for item in affordances.get(key) or []:
			parts.append(f'{key[:-1]}:{item.get("name", "")}')
	for table in affordances.get('tables') or []:
		parts.append(f'table:{table.get("name", "")}:{",".join(table.get("headers") or [])}')
	for pager in affordances.get('pagers') or []:
		parts.append(f'pager:{pager.get("kind", "")}')

	digest = hashlib.sha256('|'.join(sorted(parts)).encode()).hexdigest()
	return digest[:16]


class ManifestStore:
	"""Per-origin synthesized tool surfaces, on disk."""

	def __init__(self, path: Path | str | None = None, enabled: bool | None = None) -> None:
		from browser_use.config import CONFIG

		self.enabled = CONFIG.BROWSER_USE_SITE_TOOLS_CACHE if enabled is None else enabled
		self.path = Path(path).expanduser() if path else self._default_path()
		self._manifests: dict[str, SiteManifest] = {}
		self._loaded = False

	@staticmethod
	def _default_path() -> Path:
		from browser_use.config import CONFIG

		return CONFIG.BROWSER_USE_CONFIG_DIR / 'site_tools.json'

	def load(self) -> None:
		"""Read what previous sessions learned. Never fatal: a cache is an optimization."""
		if self._loaded:
			return
		self._loaded = True
		if not self.enabled or not self.path.exists():
			return
		try:
			raw = json.loads(self.path.read_text())
		except (OSError, json.JSONDecodeError) as e:
			logger.debug(f'🔧 Could not read site tools at {self.path}: {type(e).__name__}: {e}')
			return
		if not isinstance(raw, dict):
			return
		for origin, entry in raw.items():
			try:
				self._manifests[origin] = SiteManifest.model_validate(entry)
			except ValidationError:
				continue

	def save(self) -> None:
		if not self.enabled:
			return
		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			payload = {origin: manifest.model_dump(mode='json') for origin, manifest in self._manifests.items()}
			self.path.write_text(json.dumps(payload, indent=1))
		except OSError as e:
			logger.debug(f'🔧 Could not write site tools to {self.path}: {type(e).__name__}: {e}')

	def get(self, origin: str, expected_fingerprint: str | None = None) -> SiteManifest | None:
		"""A cached manifest, or None when there is none or the page has changed."""
		if not self.enabled or not origin:
			return None
		self.load()
		manifest = self._manifests.get(origin)
		if manifest is None:
			return None
		if expected_fingerprint and manifest.fingerprint and manifest.fingerprint != expected_fingerprint:
			logger.debug(f'🔧 {origin} has changed shape since it was learned; re-synthesizing')
			return None
		return manifest

	def put(self, manifest: SiteManifest) -> None:
		if not self.enabled or not manifest.origin:
			return
		self.load()
		self._manifests[manifest.origin] = manifest
		if len(self._manifests) > MAX_ORIGINS:
			oldest = sorted(self._manifests.items(), key=lambda pair: pair[1].created_at)
			for origin, _ in oldest[: len(self._manifests) - MAX_ORIGINS]:
				self._manifests.pop(origin, None)
		self.save()

	def forget(self, origin: str) -> None:
		self.load()
		if self._manifests.pop(origin, None) is not None:
			self.save()

	@property
	def origins(self) -> list[str]:
		self.load()
		return sorted(self._manifests)
