"""Labelling a region from cheap visual features.

Synthetic frames, not live captures: CI cannot depend on a game looking the same next
year, and the point under test is that the three features (edge density, colour variance,
shape) map to the right coarse label — which a constructed frame pins down exactly.
"""

from io import BytesIO

import pytest

from browser_use.vision.label import frame_features


def _jpeg(draw) -> bytes:
	from PIL import Image

	image = Image.new('RGB', (640, 400), (245, 245, 245))
	draw(image)
	buffer = BytesIO()
	image.save(buffer, format='JPEG', quality=92)
	return buffer.getvalue()


def _fill(image, x0, y0, x1, y1, colour):
	for y in range(y0, y1):
		for x in range(x0, x1):
			image.putpixel((x, y), colour)


def test_a_flat_solid_region_reads_as_a_button():
	"""A button is a small, flat, low-variance block — the opposite of text or media."""
	# A wide flat block; the region measured is its interior, as a motion box would be —
	# including the block's edge against the page would read as a transition and mislead.
	features = frame_features(_jpeg(lambda im: _fill(im, 240, 290, 420, 360, (40, 120, 220))))
	assert features.label(0.515, 0.81, 0.20, 0.10) == 'button'


def test_a_dense_striped_region_reads_as_text():
	"""Text is many small luminance transitions. Alternating dark rows stand in for lines
	of type — same signal, no font rendering needed in CI."""

	def stripes(image):
		# ~12px line pitch — fine enough to read as lines of type, coarse enough to survive
		# the downscale (3px stripes vanish into grey, which is the opposite of the signal).
		for y in range(100, 220):
			if (y // 6) % 2 == 0:
				_fill(image, 180, y, 460, y + 1, (15, 15, 15))

	features = frame_features(_jpeg(stripes))
	assert features.label(0.5, 0.4, 0.40, 0.28) == 'text'


def test_a_large_noisy_region_reads_as_media():
	"""A photo or video varies wildly across its area; that variance over a large region
	is what separates a stage from a panel."""

	def blocks(image):
		# 16px colour blocks — deterministic (no Math.random) and coarse enough that the
		# variance survives the 48x30 colour grid rather than averaging to grey.
		for y in range(0, 400, 16):
			for x in range(0, 640, 16):
				v = (x * 11 + y * 17) % 256
				_fill(image, x, y, x + 16, y + 16, (v, (v * 5) % 256, (v * 9) % 256))

	features = frame_features(_jpeg(blocks))
	assert features.label(0.5, 0.5, 0.9, 0.9) == 'media'


def test_a_large_flat_region_reads_as_a_panel_not_media():
	"""Big and flat is chrome — a dialog or the page behind one — not a video."""
	features = frame_features(_jpeg(lambda im: _fill(im, 20, 20, 620, 380, (235, 236, 240))))
	assert features.label(0.5, 0.5, 0.85, 0.8) == 'panel'


def test_an_undecodable_frame_labels_everything_region_rather_than_guessing():
	features = frame_features(b'not a jpeg')
	assert not features.usable
	assert features.label(0.5, 0.5, 0.2, 0.2) == 'region'


def test_the_label_channel_flows_through_the_perception_stream():
	"""End to end: every tracked object carries a label from the vocabulary alongside its
	velocity. Not a specific value — a *moving* thing's motion blob straddles its own
	border, so it reads region/media, not the flat 'button' its interior would; the point
	under test is that the channel is present and well-formed, not that motion labels
	furniture."""
	from browser_use.vision.stream import PerceptionStream

	vocabulary = {'text', 'button', 'media', 'panel', 'region'}
	stream = PerceptionStream()
	labelled = 0
	for step in range(8):

		def draw(image, s=step):
			x = 100 + s * 40
			_fill(image, x, 150, x + 160, 250, (40, 120, 220))

		stream.observe(_jpeg(draw), at=step * 0.1)
		for obj in stream.tracker.objects:
			assert obj.label in vocabulary, f'unknown label {obj.label!r}'
			labelled += 1
	assert labelled > 0, 'nothing was tracked, so nothing was labelled'


@pytest.mark.parametrize('bad', [b'', b'\xff\xd8short'])
def test_bad_bytes_never_raise(bad):
	frame_features(bad).label(0.5, 0.5, 0.1, 0.1)
