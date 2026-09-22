"""Watch a page over time, and spend tokens only on the frames that differ."""

from browser_use.vision.label import FrameFeatures, frame_features
from browser_use.vision.live import Frame, LiveView, WatchResult, frame_signature, signature_distance
from browser_use.vision.perceive import Blob, Scene, perceive
from browser_use.vision.stream import PerceptionStream, SceneTracker, Tracked

__all__ = [
	'Blob',
	'Frame',
	'FrameFeatures',
	'LiveView',
	'PerceptionStream',
	'Scene',
	'SceneTracker',
	'Tracked',
	'WatchResult',
	'frame_features',
	'frame_signature',
	'perceive',
	'signature_distance',
]
