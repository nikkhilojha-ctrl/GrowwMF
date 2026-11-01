"""Local MiniLM embeddings stored as little-endian float32 blobs.

The model truncates each chunk to its window. Full chunk text stays in FTS.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

from app.settings import get_settings

_models: dict[str, object] = {}


def pack_embedding(values: Sequence[float]) -> bytes:
    """Encode one vector as float32 bytes."""
    floats = [float(value) for value in values]
    if not floats:
        return b""
    return struct.pack(f"<{len(floats)}f", *floats)


def unpack_embedding(blob: bytes | memoryview | None) -> tuple[float, ...]:
    """Decode a float32 blob. An empty or misaligned blob is no vector."""
    if not blob:
        return ()
    raw = bytes(blob)
    if len(raw) % 4 != 0:
        return ()
    return struct.unpack(f"<{len(raw) // 4}f", raw)


def embed_texts(texts: list[str], model_name: str | None = None) -> list[bytes]:
    """Embed each text with the configured local model."""
    if not texts:
        return []
    import numpy as np

    name = model_name or get_settings().embedding_model
    model = _load_model(name)
    matrix = model.encode(
        list(texts),
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    array = np.asarray(matrix, dtype="<f4")
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.shape[0] != len(texts):
        raise ValueError(f"expected {len(texts)} embeddings, got {array.shape[0]}")
    return [pack_embedding(row.tolist()) for row in array]


def embed_query(text: str, model_name: str | None = None) -> tuple[float, ...]:
    """Embed one question with the same model used for chunks."""
    blobs = embed_texts([text], model_name)
    return unpack_embedding(blobs[0])


def _load_model(name: str):
    cached = _models.get(name)
    if cached is not None:
        return cached
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(name)
    _models[name] = model
    return model
