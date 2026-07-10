"""figtracer/hashing.py — sha256 + the mtime/size memo cache.

The cache is a pure speed-up (re-hashing 2.7 GB objects on every scan is unusable), so
the contract that matters is: a hit when size+mtime are unchanged, a miss + re-hash when
the file content changes.
"""
import os

from figtracer.hashing import HashCache, sha256_file


def _write(p, data: bytes):
    with open(p, "wb") as fh:
        fh.write(data)


def test_sha256_file_deterministic_and_prefixed(tmp_path):
    f = tmp_path / "obj.rds"
    _write(f, b"hello world")
    h1 = sha256_file(str(f))
    h2 = sha256_file(str(f))
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_cache_hit_does_not_rehash(tmp_path):
    f = tmp_path / "obj.qs2"
    _write(f, b"abc")
    cache = HashCache.load(str(tmp_path / ".figtracer" / "cache" / "hashes.json"))
    h1 = cache.hash(str(f))
    # mutate the cached digest to a sentinel; an unchanged file must return the sentinel
    key = os.path.abspath(str(f))
    cache._data[key]["hash"] = "sha256:SENTINEL"
    assert cache.hash(str(f)) == "sha256:SENTINEL"
    assert h1 != "sha256:SENTINEL"


def test_cache_miss_on_content_change(tmp_path):
    f = tmp_path / "obj.qs2"
    _write(f, b"abc")
    cache = HashCache.load(str(tmp_path / "hashes.json"))
    h1 = cache.hash(str(f))
    # change size + mtime
    _write(f, b"abcd")
    os.utime(f, (1, 1))
    h2 = cache.hash(str(f))
    assert h1 != h2


def test_cache_roundtrip_persists(tmp_path):
    f = tmp_path / "obj.RData"
    _write(f, b"xyz")
    cpath = str(tmp_path / ".figtracer" / "cache" / "hashes.json")
    cache = HashCache.load(cpath)
    h = cache.hash(str(f))
    cache.save()
    assert os.path.isfile(cpath)
    reloaded = HashCache.load(cpath)
    assert reloaded.hash(str(f)) == h
    assert reloaded._dirty is False   # served from cache, nothing re-hashed
