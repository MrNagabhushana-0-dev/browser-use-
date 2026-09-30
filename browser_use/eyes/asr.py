"""The optional speech tier: where the speech is (Silero VAD), and what it says (Whisper).

The heuristic labeller in `hearing.py` cannot reliably tell speech from music: pumping
electronic music and a narrator share the 4 Hz envelope and the pauses. A 2 MB voice
activity model can, and a 39 M-parameter Whisper can then say what was said. Both run on
the CPU, locally; audio never leaves the machine.

This is an extra, not a dependency (`pip install "browser-use[eyes]"` pulls in
faster-whisper). Without it `available()` is False and every percept says, in words, that
speech was judged by heuristics only.

The model is chosen by `BROWSER_USE_EYES_ASR_MODEL` (default `tiny.en`: ~75 MB download on
first use, about 7x faster than real time on one CPU core here). Use `base` or `small` for
other languages or better accuracy.
"""

import logging
import os
import threading
from dataclasses import dataclass

import numpy as np

from browser_use.eyes.retina import AudioHop

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
DEFAULT_MODEL = 'tiny.en'

_model = None
_model_lock = threading.Lock()


@dataclass
class Utterance:
	t0: float  # media time
	t1: float
	text: str


def available() -> bool:
	try:
		import faster_whisper  # noqa: F401
	except Exception:
		return False
	return True


def pcm_of(hops: list[AudioHop]) -> tuple[np.ndarray, np.ndarray]:
	"""(float32 samples at 16 kHz, media time of every hop's first sample) for the first pass.

	Stops at the first loop: the second time round is the same words again.
	"""
	chunks: list[np.ndarray] = []
	starts: list[float] = []
	prev_t = None
	for h in hops:
		if prev_t is not None and h.t < prev_t - 0.4 and prev_t >= 1.0:
			break
		prev_t = h.t
		if not h.pcm:
			continue
		chunks.append(np.frombuffer(h.pcm, dtype='<i2'))
		starts.append(h.t)
	if not chunks:
		return np.zeros(0, dtype=np.float32), np.zeros(0)
	return np.concatenate(chunks).astype(np.float32) / 32768.0, np.array(starts)


def _time_at(sample: int, hop_starts: np.ndarray, samples_per_hop: float) -> float:
	i = min(len(hop_starts) - 1, max(0, int(sample / samples_per_hop)))
	return float(hop_starts[i] + (sample - i * samples_per_hop) / SAMPLE_RATE)


def speech_regions(hops: list[AudioHop]) -> list[tuple[float, float]] | None:
	"""Media-time spans that contain speech, or None if the model or the PCM is missing."""
	if not available():
		return None
	audio, starts = pcm_of(hops)
	if len(audio) < SAMPLE_RATE // 2:
		return None if not len(audio) else []
	from faster_whisper.vad import VadOptions, get_speech_timestamps

	per_hop = len(audio) / len(starts)
	spans = get_speech_timestamps(audio, VadOptions(min_speech_duration_ms=200, min_silence_duration_ms=300))
	return [(_time_at(s['start'], starts, per_hop), _time_at(s['end'], starts, per_hop)) for s in spans]


def _load():
	global _model
	with _model_lock:
		if _model is None:
			from faster_whisper import WhisperModel

			name = os.environ.get('BROWSER_USE_EYES_ASR_MODEL', DEFAULT_MODEL)
			logger.info(f'👂 Loading speech model {name} (first use downloads it)')
			_model = WhisperModel(name, device='cpu', compute_type='int8')
		return _model


def transcribe(hops: list[AudioHop], language: str | None = None) -> list[Utterance] | None:
	"""Timed utterances for the first pass of this audio, or None if unavailable. Blocking."""
	if not available():
		return None
	audio, starts = pcm_of(hops)
	if len(audio) < SAMPLE_RATE // 2:
		return None if not len(audio) else []
	model = _load()
	per_hop = len(audio) / len(starts)
	name = os.environ.get('BROWSER_USE_EYES_ASR_MODEL', DEFAULT_MODEL)
	lang = language or ('en' if name.endswith('.en') else None)
	segments, _info = model.transcribe(
		audio, language=lang, vad_filter=True, beam_size=1, condition_on_previous_text=False, without_timestamps=False
	)
	out: list[Utterance] = []
	for seg in segments:
		text = seg.text.strip()
		if not text or seg.no_speech_prob > 0.8:
			continue
		out.append(
			Utterance(
				_time_at(int(seg.start * SAMPLE_RATE), starts, per_hop),
				_time_at(int(seg.end * SAMPLE_RATE), starts, per_hop),
				text,
			)
		)
	return out
