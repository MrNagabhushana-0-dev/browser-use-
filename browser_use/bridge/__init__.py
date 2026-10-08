"""Let an AI drive the person's own Chromium browser - their profile, their logins - through a small extension.

See `relay.BridgeRelay` for the endpoint and `extension/` for the extension the person loads.
"""

import json
import shutil
from pathlib import Path

from browser_use.bridge.relay import DEFAULT_PORT, EXTENSION_ID, BridgeError, BridgeRelay

EXTENSION_DIR = Path(__file__).parent / 'extension'

__all__ = [
	'DEFAULT_PORT',
	'EXTENSION_DIR',
	'EXTENSION_ID',
	'BridgeError',
	'BridgeRelay',
	'bridge_session_kwargs',
	'write_extension',
]


def bridge_session_kwargs(cdp_url: str) -> dict:
	"""BrowserProfile fields for attaching through the bridge: the person's window keeps its own size and
	permissions, so nothing tries to override them (the relay would refuse it anyway)."""
	return {
		'cdp_url': cdp_url,
		'is_local': False,
		'headless': False,
		'no_viewport': True,
		'device_scale_factor': None,
		'permissions': [],
	}


def write_extension(
	out: Path,
	relay: str | None = None,
	always_share: list[str] | None = None,
	manifest_version: int = 3,
	always_allow: list[str] | None = None,
	resume_after_ms: int | None = None,
) -> Path:
	"""Copy the extension to `out`, ready for "Load unpacked".

	`relay` overrides the WebSocket address it dials (default ws://127.0.0.1:9333/extension); `always_share` lists
	URL globs the person has decided to share without asking each time; `always_allow` lists sites (origins, `https://*.example.com` for subdomains, `*` for all) the AI may use without asking; `resume_after_ms` is how long after the
	person's last click or key in a shared tab the AI may carry on (0: only when handed back). `manifest_version=2` writes the variant for
	Chromium older than 88, which has no Manifest V3.
	"""
	assert manifest_version in (2, 3), manifest_version
	out = Path(out)
	shutil.copytree(EXTENSION_DIR, out, dirs_exist_ok=True)
	settings = json.loads((out / 'settings.json').read_text())
	if relay is not None:
		settings['relay'] = relay
	if always_share is not None:
		settings['alwaysShare'] = list(always_share)
	if always_allow is not None:
		settings['alwaysAllow'] = list(always_allow)
	if resume_after_ms is not None:
		settings['resumeAfterMs'] = resume_after_ms
	(out / 'settings.json').write_text(json.dumps(settings, indent=2) + '\n')
	if manifest_version == 2:
		manifest = json.loads((out / 'manifest.json').read_text())
		manifest['manifest_version'] = 2
		manifest['minimum_chrome_version'] = '80'
		manifest['background'] = {'scripts': ['worker.js'], 'persistent': True}
		manifest['browser_action'] = manifest.pop('action')
		(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
	assert (out / 'manifest.json').exists()
	return out
