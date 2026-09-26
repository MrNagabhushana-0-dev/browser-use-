"""Regression test for a domain-restriction bypass in SecurityWatchdog._is_url_match.

Full URL patterns (e.g. `allowed_domains=['https://example.com']`) were matched with a
raw `url.startswith(pattern)` string comparison. Because 'https://example.com' is a
literal string prefix of 'https://example.com.evil.com' (there is no host boundary
check), an attacker-controlled redirect to a completely different, attacker-owned
domain would be treated as allowed as long as it shared the allowed pattern as a
textual prefix.
"""

from bubus import EventBus

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog


class TestFullUrlPatternHostBoundary:
	"""Full URL patterns in allowed_domains/prohibited_domains must match the exact host."""

	def test_allowed_full_url_pattern_does_not_match_suffixed_host(self):
		"""'https://example.com' must not allow 'https://example.com.evil.com'."""
		browser_profile = BrowserProfile(allowed_domains=['https://example.com'], headless=True, user_data_dir=None)
		browser_session = BrowserSession(browser_profile=browser_profile)
		event_bus = EventBus()
		watchdog = SecurityWatchdog(browser_session=browser_session, event_bus=event_bus)

		# The legitimate host (and paths under it) stay allowed.
		assert watchdog._is_url_allowed('https://example.com') is True
		assert watchdog._is_url_allowed('https://example.com/path') is True

		# A different host that merely shares the pattern as a string prefix must be blocked.
		assert watchdog._is_url_allowed('https://example.com.evil.com') is False
		assert watchdog._is_url_allowed('https://example.com.evil.com/path') is False
		# Even with no dot boundary at all between the allowed host and the attacker suffix.
		assert watchdog._is_url_allowed('https://example.comevil.com') is False

	def test_prohibited_full_url_pattern_does_not_over_block_unrelated_host(self):
		"""'https://malicious.com' in prohibited_domains must not also block unrelated hosts
		that merely start with that string (the same host-boundary bug, in the prohibited-list
		direction: a different host must not be caught by a raw string-prefix match)."""
		browser_profile = BrowserProfile(prohibited_domains=['https://malicious.com'], headless=True, user_data_dir=None)
		browser_session = BrowserSession(browser_profile=browser_profile)
		event_bus = EventBus()
		watchdog = SecurityWatchdog(browser_session=browser_session, event_bus=event_bus)

		# The exact prohibited host is blocked.
		assert watchdog._is_url_allowed('https://malicious.com') is False
		assert watchdog._is_url_allowed('https://malicious.com/path') is False

		# A distinct host that happens to start with the same characters is unrelated
		# and must stay allowed (prohibited_domains must not over-block).
		assert watchdog._is_url_allowed('https://malicious.com.example.com') is True
		assert watchdog._is_url_allowed('https://malicious.comevil.com') is True

	def test_allowed_full_url_pattern_still_respects_path_prefix(self):
		"""Patterns with an explicit path still constrain to that path once the host matches exactly."""
		browser_profile = BrowserProfile(allowed_domains=['https://good.com/some/path'], headless=True, user_data_dir=None)
		browser_session = BrowserSession(browser_profile=browser_profile)
		event_bus = EventBus()
		watchdog = SecurityWatchdog(browser_session=browser_session, event_bus=event_bus)

		assert watchdog._is_url_allowed('https://good.com/some/path') is True
		assert watchdog._is_url_allowed('https://good.com/some/path/deeper') is True
		assert watchdog._is_url_allowed('https://good.com/other') is False
		# Host-suffix bypass must still be blocked even with a path-scoped pattern.
		assert watchdog._is_url_allowed('https://good.com.evil.com/some/path') is False
