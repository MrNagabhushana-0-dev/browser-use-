"""WebMCP: call the tools a page declares, instead of inferring them from pixels."""

from browser_use.webmcp.service import WebMCPService
from browser_use.webmcp.views import (
	WebMCPPageTools,
	WebMCPSource,
	WebMCPTool,
	WebMCPToolCallResult,
)

__all__ = [
	'WebMCPService',
	'WebMCPPageTools',
	'WebMCPSource',
	'WebMCPTool',
	'WebMCPToolCallResult',
]
