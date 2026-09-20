"""Share one browser between a person and the agent: they sign in, the agent continues."""

from browser_use.cobrowse.service import (
	HumanBrowser,
	attach,
	cdp_url_for,
	describe_session,
	focus_human_tab,
	launch_for_human,
)

__all__ = ['HumanBrowser', 'attach', 'cdp_url_for', 'describe_session', 'focus_human_tab', 'launch_for_human']
