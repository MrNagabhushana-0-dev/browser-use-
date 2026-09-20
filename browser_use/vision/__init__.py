"""Watch a page over time, and spend tokens only on the frames that differ."""

from browser_use.vision.live import Frame, LiveView, WatchResult, frame_signature, signature_distance

__all__ = ['Frame', 'LiveView', 'WatchResult', 'frame_signature', 'signature_distance']
