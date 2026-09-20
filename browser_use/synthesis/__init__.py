"""Synthesize a WebMCP-shaped tool surface for sites that publish none."""

from browser_use.synthesis.service import SiteToolSynthesizer
from browser_use.synthesis.views import Locator, SiteManifest, SynthesizedTool, ToolStep, to_identifier

__all__ = ['SiteToolSynthesizer', 'SiteManifest', 'SynthesizedTool', 'ToolStep', 'Locator', 'to_identifier']
