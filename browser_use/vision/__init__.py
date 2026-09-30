"""Watch a page over time, and spend tokens only on the frames that differ.

For a video specifically, `VideoWatcher` seeks it and reads the picture instead of the
transcript; `Overlay` draws a token meter and the agent's cursor in the page.
"""

from browser_use.vision.label import FrameFeatures, frame_features
from browser_use.vision.live import Frame, LiveView, WatchResult, frame_signature, signature_distance
from browser_use.vision.overlay import Overlay
from browser_use.vision.perceive import Blob, Scene, perceive
from browser_use.vision.stream import PerceptionStream, SceneTracker, Tracked
from browser_use.vision.video import NoVideoError, Shot, TokenLedger, VideoSummary, VideoWatcher, estimate_image_tokens

__all__ = [
	'Blob',
	'Frame',
	'FrameFeatures',
	'LiveView',
	'NoVideoError',
	'Overlay',
	'PerceptionStream',
	'Scene',
	'SceneTracker',
	'Shot',
	'TokenLedger',
	'Tracked',
	'VideoSummary',
	'VideoWatcher',
	'WatchResult',
	'estimate_image_tokens',
	'frame_features',
	'frame_signature',
	'perceive',
	'signature_distance',
]
