"""Content hashing for data objects, with an mtime/size memo cache.

A saved SCE/Seurat object is 0.7–2.7 GB; re-hashing the whole tree on every
``figtracer data scan`` would be unusable. So we memoise: a file's sha256 is cached
keyed by (path, size, mtime_ns). If those three are unchanged, the cached digest is
trusted and the bytes are never re-read. The cache is a plain JSON file the caller
keeps next to the registry (``.figtracer/cache/hashes.json``, gitignored) — it is a
pure speed-up and is safe to delete.

The hash itself is the algo-prefixed string ``sha256:<hex>`` so records from R
(``digest::digest(file=, algo="sha256")``) and from Python merge by identity.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

_CHUNK = 1 << 20  # 1 MiB reads


def sha256_file(path: str) -> str:
    """``sha256:<hex>`` digest of a file's contents (streamed, constant memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _stat_key(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


@dataclass
class HashCache:
    """An mtime/size-keyed sha256 memo. ``hash(path)`` returns a cached digest when the
    file is unchanged, otherwise hashes and records it. Call ``save()`` to persist."""

    path: str
    _data: dict
    _dirty: bool = False

    @classmethod
    def load(cls, path: str) -> "HashCache":
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        return cls(path=path, _data=data if isinstance(data, dict) else {})

    def hash(self, file_path: str) -> str:
        """sha256 of ``file_path``, served from cache when size+mtime are unchanged."""
        size, mtime = _stat_key(file_path)
        key = os.path.abspath(file_path)
        hit = self._data.get(key)
        if hit and hit.get("size") == size and hit.get("mtime_ns") == mtime:
            return hit["hash"]
        digest = sha256_file(file_path)
        self._data[key] = {"size": size, "mtime_ns": mtime, "hash": digest}
        self._dirty = True
        return digest

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._data, fh, indent=0, sort_keys=True)
        os.replace(tmp, self.path)
        self._dirty = False
