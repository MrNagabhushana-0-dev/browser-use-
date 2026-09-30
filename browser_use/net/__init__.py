"""Network transports for browser_use.

Currently this holds the optional Tor transport, an opt-in, off-by-default
SOCKS5 path for reaching content that a network censors or geo-restricts
(the use case: research and education from a blocked network). It is not a
bot-detection bypass: routing through Tor makes sites like Google and YouTube
*harder* to reach, because exit-node addresses are on public block lists. See
`browser_use/net/tor.py` and the "Censorship-resistant transport" section of
`AI.md`.
"""

from browser_use.net.tor import (
	TorConfig,
	TorControlError,
	TorTransport,
	TorUnavailableError,
	should_fall_back,
)

__all__ = [
	'TorConfig',
	'TorControlError',
	'TorTransport',
	'TorUnavailableError',
	'should_fall_back',
]
