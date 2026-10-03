"""Network routing for browser_use: an optional, off-by-default Tor path.

Use it to reach public pages that a network censors or geo-fences, for research and education, with
a chosen exit country. It is not a bot-detection bypass: Tor exit addresses are on public block
lists, so Google, YouTube and Cloudflare-fronted sites challenge them *more*. A human-verification
wall is reported and never retried or solved. See `browser_use/net/policy.py` and the
"Censorship-resistant transport" section of `AI.md`.
"""

from browser_use.net.control import ExitInfo
from browser_use.net.policy import NetworkMode, NetworkPolicyError, NetworkRouter, Outcome, classify_navigation
from browser_use.net.pool import TorPool
from browser_use.net.tor import (
	TorConfig,
	TorControlError,
	TorTransport,
	TorUnavailableError,
	should_fall_back,
	tor_chromium_args,
)

__all__ = [
	'ExitInfo',
	'NetworkMode',
	'NetworkPolicyError',
	'NetworkRouter',
	'Outcome',
	'TorConfig',
	'TorControlError',
	'TorPool',
	'TorTransport',
	'TorUnavailableError',
	'classify_navigation',
	'should_fall_back',
	'tor_chromium_args',
]
