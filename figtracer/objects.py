"""The pure core of the data-object registry — no I/O side effects beyond reading.

A *registry* (not a blob store): analysis objects (SCE/Seurat ``.qs2``/``.rds``/``.RData``)
stay exactly where R wrote them; their sha256 is their identity in a committed, diffable
lockfile (``figtracer-objects.yml``). This module turns two inputs —

  • ``fs_records``    : a filesystem walk (path, size, sha256, format) — the on-disk truth,
  • ``jsonl_records`` : ``.figtracer/OBJECTS.jsonl`` lines an R save-wrapper appended
                        (provenance + structural summary + lineage; R can introspect the
                        live object) —

— into the lockfile dict, reconciling them by hash (newest ``saved_at`` per hash wins,
mirroring ``figtools.manifest.load_index``), and surfaces duplicate / near-duplicate
groups so the user can ``bless`` a canonical and annotate the rest.

Everything here is pure and unit-tested; the filesystem/CLI shell lives in ``data.py``.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

LOCKFILE_NAME = "figtracer-objects.yml"
OBJECTS_JSONL = os.path.join(".figtracer", "OBJECTS.jsonl")
HASH_CACHE = os.path.join(".figtracer", "cache", "hashes.json")
SCHEMA_VERSION = 1

_EXTS = {".qs2": "qs2", ".rds": "rds", ".RData": "RData", ".rdata": "RData"}
# user-owned fields carried across re-scans, keyed by object path + hash
_USER_FIELDS = ("canonical", "public", "notes")
# trailing timestamp tokens R/CyTOFXT stamp onto filenames (…_20260623_183927, …_193749)
_TS = re.compile(r"(_\d{6,8}){1,2}$")


def format_for(path: str) -> str | None:
    """``qs2`` / ``rds`` / ``RData`` for a recognised object file, else None."""
    return _EXTS.get(os.path.splitext(path)[1].lower()) or _EXTS.get(os.path.splitext(path)[1])


# ── path helpers (relative-to-lockfile keeps the registry portable across the GDrive mount) ──
def to_rel(abs_path: str, base_dir: str) -> str:
    return os.path.relpath(abs_path, base_dir)


def to_abs(rel_path: str, base_dir: str) -> str:
    return os.path.normpath(os.path.join(base_dir, rel_path))


def _norm_stem(path: str) -> str:
    """Filename stem with trailing timestamp tokens stripped (for near-dup grouping)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return _TS.sub("", stem)


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _identity_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _unique_name(base: str, hash_: str, path: str, taken: set[str]) -> str:
    """Return a stable-enough unique logical name without collapsing exact copies."""
    if base not in taken:
        return base
    digest = hash_.split(":")[-1][:6] or "object"
    candidate = f"{base}__{digest}"
    if candidate not in taken:
        return candidate
    path_digest = hashlib.sha1(_identity_path(path).encode("utf-8")).hexdigest()[:6]
    candidate = f"{base}__{digest}_{path_digest}"
    n = 2
    while candidate in taken:
        candidate = f"{base}__{digest}_{path_digest}_{n}"
        n += 1
    return candidate


# ── the record ────────────────────────────────────────────────────────────────
@dataclass
class ObjectRecord:
    name: str
    hash: str
    path: str                       # relative to the lockfile dir
    format: str | None = None
    obj_class: str | None = None    # SingleCellExperiment | Seurat  (emitted as `class`)
    size: int | None = None
    saved_at: str = ""
    provenance: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    lineage: dict = field(default_factory=dict)
    canonical: bool = False
    public: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        d: dict = {"hash": self.hash, "path": self.path}
        if self.format:
            d["format"] = self.format
        if self.obj_class:
            d["class"] = self.obj_class
        if self.size is not None:
            d["size"] = self.size
        if self.saved_at:
            d["saved_at"] = self.saved_at
        if self.provenance:
            d["provenance"] = self.provenance
        if self.summary:
            d["summary"] = self.summary
        if self.lineage:
            d["lineage"] = self.lineage
        d["canonical"] = self.canonical
        d["public"] = self.public
        if self.notes:
            d["notes"] = self.notes
        return d

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "ObjectRecord":
        return cls(
            name=name, hash=d.get("hash", ""), path=d.get("path", ""),
            format=d.get("format"), obj_class=d.get("class"), size=d.get("size"),
            saved_at=d.get("saved_at", ""), provenance=d.get("provenance", {}) or {},
            summary=d.get("summary", {}) or {}, lineage=d.get("lineage", {}) or {},
            canonical=bool(d.get("canonical", False)), public=bool(d.get("public", False)),
            notes=d.get("notes", "") or "",
        )


# ── lockfile (de)serialisation ─────────────────────────────────────────────────
def load_lockfile(path: str) -> dict:
    """Parse a ``figtracer-objects.yml`` (returns an empty skeleton if absent/unparseable)."""
    import yaml
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    data.setdefault("version", SCHEMA_VERSION)
    data.setdefault("objects", {})
    data.setdefault("duplicates", [])
    return data


def dump_lockfile(lock: dict, path: str) -> None:
    import yaml
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(lock, fh, sort_keys=False, default_flow_style=False, allow_unicode=True)
    os.replace(tmp, path)


def records_from_lock(lock: dict) -> dict[str, ObjectRecord]:
    return {name: ObjectRecord.from_dict(name, d) for name, d in lock.get("objects", {}).items()}


# ── reconciliation ──────────────────────────────────────────────────────────────
def _newest(a: dict, b: dict) -> dict:
    """Of two records sharing a hash, keep the one with the newer ``saved_at`` (ISO sorts)."""
    return a if a.get("saved_at", "") >= b.get("saved_at", "") else b


def reconcile(fs_records, jsonl_records, *, base_dir, prev=None, experiment_id=None) -> dict:
    """Build the lockfile dict from a filesystem walk + R-appended JSONL records.

    * ``fs_records``    : list of {path (abs), size, hash, format}.
    * ``jsonl_records`` : list of parsed ``OBJECTS.jsonl`` dicts (may be []).
    * ``prev``          : the previously-written lockfile dict — its user-owned fields
                          (canonical / public / notes, and a duplicate group's
                          ``resolution``) are carried forward. Physical copies use
                          path+hash identity so exact duplicates remain independently
                          blessable; a single moved file may fall back to hash identity.
    Records merge by hash; the JSONL side supplies provenance/summary/lineage, the FS
    side supplies the authoritative on-disk path/size/format.
    """
    prev = prev or {}
    prev_records = list(records_from_lock(prev).values())
    prev_by_identity = {
        (r.hash, _identity_path(r.path)): r for r in prev_records
    }
    prev_by_hash: dict[str, list[ObjectRecord]] = {}
    for r in prev_records:
        prev_by_hash.setdefault(r.hash, []).append(r)
    prev_res = {tuple(sorted(g.get("hashes", []))): g.get("resolution", "pending")
                for g in prev.get("duplicates", [])}

    current_hash_counts: dict[str, int] = {}
    for fr in fs_records:
        current_hash_counts[fr["hash"]] = current_hash_counts.get(fr["hash"], 0) + 1

    # newest JSONL record per hash
    jl: dict[str, dict] = {}
    for rec in jsonl_records or []:
        h = rec.get("hash")
        if not h:
            continue
        jl[h] = _newest(jl[h], rec) if h in jl else rec

    objects: dict[str, ObjectRecord] = {}
    taken: set[str] = set()

    for fr in sorted(fs_records, key=lambda r: r["path"]):
        h = fr["hash"]
        j = jl.get(h, {})
        rel_path = to_rel(fr["path"], base_dir)
        base_name = str(j.get("name") or _stem(fr["path"]))
        name = _unique_name(base_name, h, rel_path, taken)
        taken.add(name)

        rec = ObjectRecord(
            name=name, hash=h,
            path=rel_path,
            format=fr.get("format") or format_for(fr["path"]),
            obj_class=j.get("class"),
            size=fr.get("size"),
            saved_at=j.get("saved_at", ""),
            provenance=j.get("provenance") or _provenance_from_flat(j),
            summary=j.get("summary", {}) or {},
            lineage=_lineage_from(j),
        )
        # Exact duplicates share a hash but are separate files. Carry user choices by
        # path+hash so blessing one copy never blesses or demotes another on re-scan.
        pu = prev_by_identity.get((h, _identity_path(rel_path)))
        if pu is None and current_hash_counts[h] == 1 and len(prev_by_hash.get(h, [])) == 1:
            pu = prev_by_hash[h][0]  # single-file rename/move compatibility
        if pu is not None:
            rec.canonical, rec.public, rec.notes = pu.canonical, pu.public, pu.notes
        objects[name] = rec

    dups = find_duplicates(list(objects.values()))
    for g in dups:
        g["resolution"] = prev_res.get(tuple(sorted(g["hashes"])), "pending")

    lock: dict = {"version": SCHEMA_VERSION}
    if experiment_id:
        lock["experiment_id"] = experiment_id
    lock["objects"] = {name: rec.to_dict() for name, rec in
                       sorted(objects.items(), key=lambda kv: kv[1].path)}
    lock["duplicates"] = dups
    return lock


def _provenance_from_flat(j: dict) -> dict:
    """Pull the flat f2-style provenance keys out of a JSONL record, if present."""
    keys = ("qmd_path", "chunk_label", "git_commit", "git_branch", "r_version")
    prov = {k: j[k] for k in keys if j.get(k) is not None}
    return prov


def _lineage_from(j: dict) -> dict:
    lin = j.get("lineage")
    if isinstance(lin, dict) and lin:
        return lin
    out: dict = {}
    if j.get("derived_from"):
        out["derived_from"] = j["derived_from"]
    if j.get("op"):
        out["op"] = j["op"]
    return out


# ── duplicate / near-duplicate detection ────────────────────────────────────────
def _within(a: int | None, b: int | None, frac: float) -> bool:
    if not a or not b:
        return False
    return abs(a - b) <= frac * max(a, b)


def _prefix_related(a: str, b: str, min_len: int = 6) -> bool:
    """True if two timestamp-stripped stems are equal or one is a prefix of the other."""
    if a == b:
        return len(a) >= min_len
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= min_len and long.startswith(short)


def find_duplicates(records: list[ObjectRecord], near_frac: float = 0.01) -> list[dict]:
    """Group records into duplicate sets.

    * **exact** — identical hash (definitively the same bytes).
    * **near**  — different hash but same/prefix-related timestamp-stripped stem AND
      size within ``near_frac`` (default 1%). Catches the real cases: the Tube3
      ``…_183927`` / ``…_190239`` pair, and the three ``sceHI2_clustered*`` variants.
      Never auto-resolved — flagged for the user to ``bless`` + annotate.
    """
    groups: list[dict] = []

    # exact: by hash
    by_hash: dict[str, list[ObjectRecord]] = {}
    for r in records:
        by_hash.setdefault(r.hash, []).append(r)
    exact_members = set()
    for h, members in by_hash.items():
        if len(members) > 1:
            members = sorted(members, key=lambda r: r.path)
            groups.append(_group("exact", members))
            exact_members.update(id(m) for m in members)

    # near: union-find over (size + stem) among distinct-hash records
    cand = [r for r in records if id(r) not in exact_members]
    n = len(cand)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    stems = [_norm_stem(r.path) for r in cand]
    for i in range(n):
        for k in range(i + 1, n):
            if cand[i].hash == cand[k].hash:
                continue
            if _within(cand[i].size, cand[k].size, near_frac) and _prefix_related(stems[i], stems[k]):
                union(i, k)

    clusters: dict[int, list[ObjectRecord]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(cand[i])
    for members in clusters.values():
        if len(members) > 1:
            members = sorted(members, key=lambda r: r.path)
            groups.append(_group("near", members))

    groups.sort(key=lambda g: (g["kind"] != "near", g["names"]))
    return groups


def _group(kind: str, members: list[ObjectRecord]) -> dict:
    return {
        "kind": kind,
        "names": [m.name for m in members],
        "hashes": [m.hash for m in members],
        "paths": [m.path for m in members],
        "sizes": [m.size for m in members],
        "resolution": "pending",
    }


# ── lineage graph ───────────────────────────────────────────────────────────────
def lineage_graph(records: dict[str, ObjectRecord]) -> dict:
    """{name -> [parent names]} resolved from each record's ``lineage.derived_from`` hashes.

    Roots (no recognised parent) map to []. Parent hashes pointing outside the registry
    are dropped (can't name them); kept only when they resolve to a known object.
    """
    by_hash = {r.hash: name for name, r in records.items()}
    graph: dict[str, list[str]] = {}
    for name, r in records.items():
        parents = []
        for ph in (r.lineage.get("derived_from") or []):
            if ph in by_hash and by_hash[ph] != name:
                parents.append(by_hash[ph])
        graph[name] = parents
    return graph
