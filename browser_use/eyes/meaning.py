"""Search what the eyes have seen by meaning: archived keyframes as vectors, queried in words.

A retina does not send pictures to the brain; it sends a compact code. This is the practical
version of that for a model behind an API: every archived keyframe is turned into a 512-number
vector by an open image-text model (OpenAI's CLIP ViT-B/32, MIT licence, run locally with
onnxruntime, no PyTorch), stored next to the archive. A question in words ("the moment with the
blue screen", "where the code is shown") becomes a vector too, and the nearest frames come back.
The stream stays on disk as vectors; only the answers reach the model's context.

Measured on this repo's calibration frames: the fp16 weights rank exactly like fp32 (5/5 queries,
same margins) at half the download; the int8 weights do not (they ranked a red frame wrong), so
they are not used. CLIP was trained on English; its model card limits it to English queries.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import numpy as np

from browser_use.eyes.archive import FrameArchive

logger = logging.getLogger(__name__)

MODEL_REPO = 'Xenova/clip-vit-base-patch32'
VISION_FILE = 'onnx/vision_model_fp16.onnx'
TEXT_FILE = 'onnx/text_model_fp16.onnx'
TOKENIZER_FILE = 'tokenizer.json'
SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
CONTEXT = 77  # CLIP's text length, start and end tokens included


class Embedder:
	"""CLIP's image and text encoders, loaded on first use (downloaded once into the HF cache)."""

	def __init__(self, repo: str = MODEL_REPO) -> None:
		self.repo = repo
		self._vision = None
		self._text = None
		self._tok = None

	def _load(self) -> None:
		if self._vision is not None:
			return
		import onnxruntime as ort
		from huggingface_hub import hf_hub_download
		from tokenizers import Tokenizer

		providers = ['CPUExecutionProvider']
		self._vision = ort.InferenceSession(hf_hub_download(self.repo, VISION_FILE), providers=providers)
		self._text = ort.InferenceSession(hf_hub_download(self.repo, TEXT_FILE), providers=providers)
		self._tok = Tokenizer.from_file(hf_hub_download(self.repo, TOKENIZER_FILE))

	@staticmethod
	def _pixels(jpeg: bytes) -> np.ndarray:
		"""CLIP preprocessing: shortest side to 224 (bicubic), centre crop, normalise, CHW."""
		from PIL import Image

		with Image.open(io.BytesIO(jpeg)) as img:
			img = img.convert('RGB')
			w, h = img.size
			scale = SIZE / min(w, h)
			img = img.resize((max(SIZE, round(w * scale)), max(SIZE, round(h * scale))), Image.Resampling.BICUBIC)
			w, h = img.size
			left, top = (w - SIZE) // 2, (h - SIZE) // 2
			arr = np.asarray(img.crop((left, top, left + SIZE, top + SIZE)), dtype=np.float32) / 255.0
		return ((arr - MEAN) / STD).transpose(2, 0, 1)

	@staticmethod
	def _unit(x: np.ndarray) -> np.ndarray:
		x = x.astype(np.float32)
		return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)

	def images(self, jpegs: list[bytes]) -> np.ndarray:
		"""Unit vectors (n, 512) for JPEG images."""
		self._load()
		assert self._vision is not None
		if not jpegs:
			return np.zeros((0, 512), dtype=np.float32)
		batch = np.stack([self._pixels(j) for j in jpegs])
		return self._unit(np.asarray(self._vision.run(None, {'pixel_values': batch})[0]))

	def text(self, query: str) -> np.ndarray:
		"""A unit vector (512,) for a query in words."""
		self._load()
		assert self._text is not None and self._tok is not None
		ids = self._tok.encode(query).ids
		if len(ids) > CONTEXT:  # keep the end token: CLIP pools at it
			ids = ids[: CONTEXT - 1] + ids[-1:]
		return self._unit(np.asarray(self._text.run(None, {'input_ids': np.array([ids], dtype=np.int64)})[0])[0])


class MeaningIndex:
	"""Vectors for an archive's keyframes, kept beside it and brought up to date on demand."""

	def __init__(self, archive: FrameArchive, embedder: Embedder | None = None) -> None:
		self.archive = archive
		self.embedder = embedder or Embedder()
		self.vectors_path = archive.root / 'meaning.npy'
		self.keys_path = archive.root / 'meaning.keys.json'
		self._keys: list[tuple[int, int]] = []
		self._vectors = np.zeros((0, 512), dtype=np.float16)
		self._load()

	def _load(self) -> None:
		try:
			keys = [tuple(k) for k in json.loads(self.keys_path.read_text())]
			vectors = np.load(self.vectors_path)
		except (FileNotFoundError, ValueError):
			return
		if len(keys) == len(vectors):
			self._keys, self._vectors = keys, vectors  # type: ignore[assignment]

	def _save(self) -> None:
		self.archive.root.mkdir(parents=True, exist_ok=True)
		np.save(self.vectors_path, self._vectors)
		self.keys_path.write_text(json.dumps(self._keys))

	def __len__(self) -> int:
		return len(self._keys)

	def update(self, batch: int = 32) -> int:
		"""Embed archived keyframes that have no vector yet; drop vectors of evicted frames. Returns how many added."""
		kept = [i for i, (vid, seq) in enumerate(self._keys) if self.archive.has(vid, seq)]
		if len(kept) != len(self._keys):
			self._keys = [self._keys[i] for i in kept]
			self._vectors = self._vectors[kept]
		known = set(self._keys)
		todo = [(e['vid'], e['seq']) for e in self.archive.entries() if (e['vid'], e['seq']) not in known]
		added = 0
		for start in range(0, len(todo), batch):
			chunk = todo[start : start + batch]
			pairs = [(key, self.archive.read(*key)) for key in chunk]
			pairs = [(key, jpeg) for key, jpeg in pairs if jpeg]
			if not pairs:
				continue
			vectors = self.embedder.images([jpeg for _, jpeg in pairs]).astype(np.float16)
			self._keys += [key for key, _ in pairs]
			self._vectors = np.concatenate([self._vectors, vectors]) if len(self._vectors) else vectors
			added += len(pairs)
		if added or len(kept) != len(known):
			self._save()
		return added

	def search(self, query: str, k: int = 4, vid: int | None = None) -> list[tuple[float, int, int, float]]:
		"""The k best matches as (score, vid, seq, media t), best first. Scores are cosine similarities."""
		assert k >= 1
		if not self._keys:
			return []
		scores = self._vectors.astype(np.float32) @ self.embedder.text(query)
		times = {(e['vid'], e['seq']): e['t'] for e in self.archive.entries()}
		order = np.argsort(-scores)
		out: list[tuple[float, int, int, float]] = []
		for i in order:
			key = self._keys[int(i)]
			if key not in times or (vid is not None and key[0] != vid):
				continue
			out.append((float(scores[int(i)]), key[0], key[1], times[key]))
			if len(out) == k:
				break
		return out


def default_index_dir(now_path: Path) -> Path:
	return now_path.with_name('frames')
