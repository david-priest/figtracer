"""figtracer figsync — keep Obsidian lab-note figures in sync with the latest f2 render.

The qmd (+ its `MANIFEST.jsonl`) is the single source of truth; the vault note is a
derived presentation layer. figsync resolves each figure's latest render from the
MANIFEST and overwrites the **stable PNG** the note already embeds
(`<exp>_<title>.png`) — the note prose is never touched. Provenance goes in a
separate auto-generated index note.

  figtracer figsync index  [--exp ID] [--committed-only]      latest-per-title
  figtracer figsync drift  [--exp ID] [--committed-only]      note<->figure rename/orphan report
  figtracer figsync sync   [--exp ID] [--committed-only] [-y] materialize embedded figures
  figtracer figsync prune  [--exp ID] [--keep N] [-y]         trash superseded f2 renders (keep newest N/title)

Design notes:
- **Stable filename, never dated** in the embed -> note text never churns; history
  lives in git + the dated f2 originals.
- **Note-driven + embed-flagged**: only figures BOTH flagged `embed = TRUE` in f2()
  AND actually placed in a note are materialized (no orphan PNGs).
- **--committed-only** resolves "latest" to the newest *git-committed* render
  (via MANIFEST `git_commit`), so a synced note points at a reproducible figure.
- **drift** reports the two failure modes that rot quietly: note embeds that match
  no f2 title (rename/standardise needed) and embed=TRUE figures placed nowhere.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess

from figtools import links as _links
from labkit import config as lkconfig
from figtracer.sync import resolve


def _link_style_default() -> str:
    """Preferred embed style from the labkit user config (`link_style:` in
    ~/.config/labkit/config.yaml); shipped fallback is portable `html`. Lets a
    vault owner pin `obsidian` once instead of passing --link-style every time."""
    return lkconfig.user_config().get("link_style") or "html"


def _key(e):
    return e.get("saved_at") or e.get("timestamp") or ""


def _manifests(analysis_dir: str) -> list[str]:
    # new layout: single outputs/MANIFEST.jsonl ; old: one per dated folder
    m = set(glob.glob(os.path.join(analysis_dir, "outputs", "MANIFEST.jsonl")))
    m |= set(glob.glob(os.path.join(analysis_dir, "*", "MANIFEST.jsonl")))
    return sorted(m)


def _rasterizable(e) -> bool:
    """A render figsync can materialise into a PNG (copy or pdftoppm). SVG (and any
    other format) is NOT rasterizable here — this is the guard that stops a newer
    figtools .svg from shadowing a note figure's .pdf."""
    p = (e.get("_path") or "").lower()
    return p.endswith(".pdf") or p.endswith(".png")


def _load_versions(analysis_dir: str) -> dict:
    """{(channel, title): [all MANIFEST entries, each with _path + _channel set]}
    across every MANIFEST. `channel` is an orthogonal second coordinate to `title`
    (= intent/consumer, e.g. "note" vs "panel"); entries with NO channel field
    resolve as "note" for back-compat. MANIFEST stays append-only."""
    versions: dict[tuple[str, str], list] = {}
    for mpath in _manifests(analysis_dir):
        folder = os.path.dirname(mpath)
        with open(mpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("title")
                if not t:
                    continue
                ch = e.get("channel") or "note"  # missing/null -> note (back-compat)
                e["_channel"] = ch
                rel = e.get("rel_path")
                e["_path"] = os.path.join(folder, rel) if rel else os.path.join(folder, e.get("fig", ""))
                versions.setdefault((ch, t), []).append(e)
    return versions


def resolve_figures(analysis_dir: str, committed_only: bool = False,
                    channel: str = "note") -> dict:
    """{title: latest render for `channel` whose file exists}, keyed by title alone
    (title is the figure identity). Only entries in the requested `channel` are
    considered, so a figtools "panel" render can never shadow a "note" figure.

    Within the channel: prefer the newest render that figsync can rasterise
    (pdf/png), falling back to any existing render (e.g. svg) only if no pdf/png
    exists. Note figures always emit a pdf, so this restores them past a newer svg.
    With committed_only, prefer entries carrying a git_commit (reproducible).
    Sets _missing when nothing exists on disk."""
    versions = _load_versions(analysis_dir)
    out = {}
    for (ch, t), vs in versions.items():
        if ch != channel:
            continue
        vs.sort(key=_key, reverse=True)
        pool = [e for e in vs if e.get("git_commit")] if committed_only else vs
        if not pool:
            pool = vs
        on_disk = [e for e in pool if os.path.exists(e["_path"])]
        # prefer newest rasterizable; fall back to any existing render (svg)
        chosen = next((e for e in on_disk if _rasterizable(e)), None)
        if chosen is None:
            chosen = on_disk[0] if on_disk else None
        if chosen is None:
            chosen = dict(pool[0])
            chosen["_missing"] = True
        chosen["_n"] = len(vs)
        out[t] = chosen
    return out


def _exp_paths(args):
    cfg = lkconfig.load(args.config) if args.config else lkconfig.load()
    exp = resolve(cfg, exp=args.exp)
    eid = str(exp.get("experiment_id"))
    note = exp["_note"]
    note_dir = os.path.dirname(note)
    data_dir = os.path.abspath(os.path.expanduser(exp["data_dir"]))
    attach = os.path.join(note_dir, "attachments")
    notes = [p for p in glob.glob(os.path.join(note_dir, "*.md"))
             if "Figure provenance" not in os.path.basename(p)]
    qmd = exp.get("analysis_qmd")
    qmd = os.path.abspath(os.path.expanduser(qmd)) if qmd else None
    return eid, data_dir, note_dir, attach, notes, qmd


def _f2_calls(src: str):
    """Yield the full source text of each f2(...) call, brace-balanced and
    quote-aware (so parens inside a title string don't break balancing), skipping
    calls that begin on a commented line."""
    for m in re.finditer(r"\bf2\s*\(", src):
        ls = src.rfind("\n", 0, m.start()) + 1
        if src[ls:m.start()].lstrip().startswith("#"):
            continue
        depth, j, n, in_str = 0, m.end() - 1, len(src), None
        while j < n:
            c = src[j]
            if in_str:
                if c == in_str and src[j - 1] != "\\":
                    in_str = None
            elif c in "\"'":
                in_str = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield src[m.start():j + 1]


def _qmd_embed_titles(qmd: str | None) -> set:
    """Titles of f2() calls flagged embed = TRUE/T in the qmd (named or positional).
    Whole-call (multi-line safe), not per-line."""
    titles: set[str] = set()
    if not qmd or not os.path.exists(qmd):
        return titles
    with open(qmd, encoding="utf-8") as f:
        src = f.read()
    for call in _f2_calls(src):
        if not re.search(r"\bembed\s*=\s*(TRUE|T)\b", call):
            continue
        m = re.search(r'\btitle\s*=\s*"([^"]+)"', call) or re.search(r'"([^"]+)"', call)
        if m:
            titles.add(m.group(1))
    return titles


def _note_embeds(eid: str, notes: list[str]) -> dict:
    # recognise the figure in ANY embed style (Obsidian wikilink, markdown, or html img),
    # so a vault stays fully tracked whichever style figures were placed in
    rx = _links.embed_pattern(re.escape(eid) + r"_[^/|\]\)\"]+?")
    ref: dict[str, list] = {}
    for n in notes:
        with open(n, encoding="utf-8") as f:
            txt = f.read()
        for m in rx.finditer(txt):
            slug = _links.embed_filename(m)[len(eid) + 1:-4]
            ref.setdefault(slug, []).append(os.path.basename(n))
    return ref


def cmd_index(figs: dict) -> None:
    for t in sorted(figs):
        e = figs[t]
        flag = "EMBED" if e.get("embed") else "     "
        miss = "  (NO on-disk render)" if e.get("_missing") else ""
        print(f"{e.get('saved_at', ''):26} {flag} v{e['_n']:<2} "
              f"{(e.get('git_commit') or '-')[:8]:8} {t}{miss}")


def cmd_drift(figs: dict, eid: str, notes: list[str], qmd_titles: set,
              attach: str | None = None) -> None:
    ref = _note_embeds(eid, notes)
    titles = set(figs)
    embed_titles = {t for t, e in figs.items() if e.get("embed")}
    awaiting = orphan = not_mat = 0
    print("== note embeds -> figure ==")
    for slug in sorted(ref):
        where = ", ".join(sorted(set(ref[slug])))
        if slug in titles:
            if figs[slug].get("_missing"):
                tag = "DANGLING (title ok, no on-disk render)"
            elif attach is not None and not os.path.exists(
                    os.path.join(attach, _attachment_name(eid, slug, "note"))):
                # resolvable but the attachment PNG isn't on disk: an unrasterizable
                # render (svg) or a sync that FAILed. Must NOT read "ok".
                tag = "NOT MATERIALISED (run figsync sync)"
                not_mat += 1
            else:
                tag = "ok"
        elif slug in qmd_titles:
            tag = "AWAITING RE-RUN (f2 embed=TRUE exists; not yet rendered)"
            awaiting += 1
        else:
            tag = "ORPHAN (no f2 source — manual fig, or needs embed=TRUE/rename)"
            orphan += 1
        print(f"  [{tag}]  {slug}  ({where})")
    unplaced = sorted(embed_titles - set(ref))
    print(f"\n== embed=TRUE figures (in MANIFEST) not placed in any note ({len(unplaced)}) ==")
    for t in unplaced:
        print(f"  UNPLACED  {t}")
    print(f"\nsummary: {len(ref)} embeds — {awaiting} awaiting re-run, "
          f"{not_mat} not materialised (run sync), "
          f"{orphan} orphaned (no f2 source), {len(unplaced)} unplaced")


def _attachment_name(eid: str, title: str, channel: str = "note") -> str:
    """Stable attachment PNG basename. channel="note" is IMPLICIT so existing
    embeds ![[<eid>_<title>.png]] are unchanged; other channels get a visible
    <channel>_ prefix so they occupy a separate namespace that can't collide."""
    if channel == "note":
        return f"{eid}_{title}.png"
    return f"{eid}_{channel}_{title}.png"


def _rasterize(src: str, dst: str, dpi: int = 300) -> None:
    low = src.lower()
    if low.endswith(".png"):
        shutil.copy(src, dst)
        return
    if not low.endswith(".pdf"):
        raise ValueError(f"can't rasterize non-PDF/PNG source: {src}")
    stem = dst[:-4] if dst.lower().endswith(".png") else dst
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", src, stem], check=True)


def _write_provenance(eid: str, note_dir: str, mat: dict, ref: dict | None = None) -> str:
    ref = ref or {}
    out = os.path.join(note_dir, f"{eid} — Figure provenance (auto).md")
    # `title:` so Obsidian front-matter-title plugins show a readable name in the explorer
    # instead of the raw filename. Deliberately NO `experiment_id` — that key is what marks a
    # note as an experiment note, and this auto-generated index must not show up as one
    # (Mission Control / sync scan on it).
    lines = ["---", "title: Figure provenance", "---", "",
             f"# {eid} — Figure provenance (auto-generated)", "",
             "> [!info] Auto-generated by `figtracer figsync` — do not edit by hand. Each row",
             "> ties an embedded figure to the note it appears in + the f2() render + qmd chunk",
             "> + git commit it was rendered from.", "",
             "| note figure | embedded in | rendered | git commit | qmd chunk | source file |",
             "| --- | --- | --- | --- | --- | --- |"]
    for t in sorted(mat):
        e = mat[t]
        where = ", ".join(f"[[{os.path.splitext(n)[0]}]]" for n in sorted(set(ref.get(t, [])))) or "—"
        lines.append(f"| `{eid}_{t}.png` | {where} | {e.get('saved_at', '')} | "
                     f"`{e.get('git_commit') or '-'}` | `{e.get('chunk_label') or '-'}` | "
                     f"{os.path.basename(e.get('_path', ''))} |")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out


def materialize(figs, eid, note_dir, attach, notes, dpi=300, execute=False,
                channel="note") -> dict:
    """Core sync step (reusable by `figtracer sync`). Rasterizes the latest render
    of each figure that is BOTH embed=TRUE AND referenced in a note, overwriting
    the stable attachment PNG (`<eid>_<title>.png` for the note channel), and
    writes the provenance index. Returns a summary dict; does not print."""
    ref = _note_embeds(eid, notes)
    targets = sorted(t for t, e in figs.items() if e.get("embed") and t in ref)
    unplaced = sorted(t for t, e in figs.items() if e.get("embed") and t not in ref)
    if execute and targets:
        os.makedirs(attach, exist_ok=True)
    synced, missing, failed, mat = [], [], [], {}
    for t in targets:
        e = figs[t]
        if e.get("_missing") or not os.path.exists(e["_path"]):
            missing.append(t)
            continue
        if execute:
            try:
                _rasterize(e["_path"], os.path.join(attach, _attachment_name(eid, t, channel)), dpi)
            except Exception as exc:  # one bad figure mustn't abort the whole sync
                failed.append((t, str(exc)))
                continue
        mat[t] = e
        synced.append(t)
    # write provenance whenever there are targets (so it never goes stale), even
    # if some skipped — it reflects what's actually materialized right now.
    prov = _write_provenance(eid, note_dir, mat, ref) if (targets and execute) else None
    return {"synced": synced, "missing": missing, "failed": failed,
            "unplaced": unplaced, "provenance": prov}


def cmd_sync(figs, eid, attach, note_dir, notes, dpi, execute) -> int:
    r = materialize(figs, eid, note_dir, attach, notes, dpi, execute)
    print(f"{len(r['synced'])} figure(s) {'synced' if execute else 'to sync'} "
          f"(embed=TRUE and referenced in a note)"
          f"{'' if execute else '  [DRY RUN — pass -y to write]'}")
    for t in r["synced"]:
        e = figs[t]
        verb = "wrote" if execute else "would write"
        print(f"  {verb} {eid}_{t}.png  <- {os.path.basename(e['_path'])}  ({e.get('saved_at', '')})")
    for t in r["missing"]:
        print(f"  SKIP {t}: no on-disk render (re-run the chunk)")
    for t, err in r.get("failed", []):
        print(f"  FAIL {t}: {err}")
    if r["unplaced"]:
        print(f"  note: {len(r['unplaced'])} embed=TRUE figure(s) not in any note (skipped): "
              f"{', '.join(r['unplaced'])}")
    if r["provenance"]:
        print(f"  provenance -> {os.path.basename(r['provenance'])}")
    return 0


def cmd_place(args, eid, qmd, figs, notes) -> int:
    """Print (and optionally insert) the correct embed for a figure in the chosen
    link-style, so the title<->filename<->embed contract is never hand-typed."""
    title = args.title
    if not title:
        print("usage: figtracer figsync place <title> [--note <name>] [--width N] [--caption ...] [-y]")
        return 2
    known = _qmd_embed_titles(qmd) | set(figs)
    block = _links.image_embed(f"{eid}_{title}.png", args.width,
                               getattr(args, "link_style", None) or _link_style_default(), alt=title)
    if args.caption:
        block += f"\n_{args.caption}_"
    if title not in known:
        print(f"  ! warning: '{title}' is not a known embed=TRUE f2 title or MANIFEST figure.")
        print(f"    known: {', '.join(sorted(known)) or '(none)'}\n")
    print("embed block:\n  " + block.replace("\n", "\n  ") + "\n")
    if not args.note:
        print("(no --note: copy the block above, or pass --note <name> -y to insert it)")
        return 0
    cands = [n for n in notes if args.note in os.path.basename(n)]
    if len(cands) != 1:
        print(f"--note '{args.note}' matched {len(cands)}: "
              f"{[os.path.basename(c) for c in cands]}")
        return 2
    note = cands[0]
    if title in _note_embeds(eid, [note]):
        print(f"'{title}' already embedded in {os.path.basename(note)} — nothing to do.")
        return 0
    if not args.yes:
        print(f"(dry run) would insert into {os.path.basename(note)} — pass -y to write.")
        return 0
    txt = open(note, encoding="utf-8").read()
    idx = txt.find("\n# Log")
    insert = "\n" + block + "\n"
    new = (txt[:idx] + insert + txt[idx:]) if idx != -1 else (txt.rstrip("\n") + "\n" + insert)
    with open(note, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"inserted into {os.path.basename(note)}"
          f"{' (before # Log)' if idx != -1 else ''}.")
    print("  next: `figtracer figsync sync -y` to materialize the PNG.")
    return 0


def _render_siblings(fig_path: str) -> list[str]:
    """All artifacts of one f2 render sharing a timestamp+title stem: the figure
    file itself, its other-format twin, the saveRData .RData and the saveExcel
    _data.xlsx. Matched explicitly (not glob) so a prefix-sharing title can't be
    swept in. The .rmd/_sessioninfo.txt session snapshots do NOT share this stem
    and are never touched."""
    stem, _ = os.path.splitext(fig_path)
    cands = [stem + s for s in (".pdf", ".png", ".RData", "_data.xlsx")]
    return [p for p in cands if os.path.exists(p)]


def _to_trash(paths: list[str]) -> None:
    """Move paths to the macOS Trash (recoverable). Prefer the `trash` CLI; fall
    back to ~/.Trash with collision-safe renaming."""
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return
    if shutil.which("trash"):
        subprocess.run(["trash", *paths], check=True)
        return
    tdir = os.path.expanduser("~/.Trash")
    os.makedirs(tdir, exist_ok=True)
    for p in paths:
        dest = os.path.join(tdir, os.path.basename(p))
        n = 1
        while os.path.exists(dest):
            base, ext = os.path.splitext(os.path.basename(p))
            dest = os.path.join(tdir, f"{base} {n}{ext}")
            n += 1
        shutil.move(p, dest)


def prune_old_renders(analysis_dir: str, keep: int = 1, execute: bool = False) -> dict:
    """For each (channel, title), keep the newest `keep` renders that exist on disk
    and trash all older ones (+ each render's siblings). MANIFEST.jsonl is left
    intact (append-only provenance; resolve_figures tolerates the now-dangling
    entries). Returns a summary; moves files to Trash only when execute=True.

    Guard: the newest rasterizable (pdf/png) render of a channel is NEVER trashed,
    even if it sorts beyond `keep`. This is exactly the case that deleted the note
    PDFs — a newer non-rasterizable svg pushed the pdf past the keep window."""
    versions = _load_versions(analysis_dir)
    drop, per_title = [], []
    for (ch, t), vs in versions.items():
        on_disk = [e for e in vs if os.path.exists(e["_path"])]
        on_disk.sort(key=_key, reverse=True)
        losers = on_disk[keep:]
        # never let the newest rasterizable render fall into losers
        newest_raster = next((e for e in on_disk if _rasterizable(e)), None)
        if newest_raster is not None:
            losers = [e for e in losers if e is not newest_raster]
        if not losers:
            continue
        files = []
        for e in losers:
            files += _render_siblings(e["_path"])
        files = sorted(set(files))
        label = t if ch == "note" else f"{ch}:{t}"
        per_title.append({"title": label, "kept": len(on_disk) - len(losers),
                          "dropped": len(losers), "files": files})
        drop += files
    drop = sorted(set(drop))
    freed = sum(os.path.getsize(p) for p in drop if os.path.exists(p))
    if execute and drop:
        _to_trash(drop)
    return {"per_title": sorted(per_title, key=lambda d: -d["dropped"]),
            "files": drop, "freed": freed, "keep": keep}


def cmd_prune(analysis_dir, keep, execute) -> int:
    r = prune_old_renders(analysis_dir, keep=keep, execute=execute)
    mb = r["freed"] / 1e6
    if not r["files"]:
        print(f"nothing to prune — every title already has <= {keep} render(s) on disk.")
        return 0
    verb = "trashed" if execute else "would trash"
    print(f"{verb} {len(r['files'])} file(s) across {len(r['per_title'])} title(s), "
          f"~{mb:.1f} MB (keeping newest {keep}/title)"
          f"{'' if execute else '  [DRY RUN — pass -y to move to Trash]'}\n")
    for d in r["per_title"]:
        print(f"  {d['title']}: drop {d['dropped']} old render(s), keep {d['kept']}")
    if not execute:
        print("\nMANIFEST.jsonl is left intact (provenance); the resolver ignores the dangling entries.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="figtracer figsync",
                                 description="Sync lab-note figures to the latest f2 render.")
    ap.add_argument("action", choices=["index", "drift", "sync", "place", "prune"])
    ap.add_argument("title", nargs="?", help="figure title (for `place`)")
    ap.add_argument("--exp", help="experiment_id (default: resolve from current directory)")
    ap.add_argument("--config", help="path to projects.yaml")
    ap.add_argument("--committed-only", action="store_true",
                    help="resolve 'latest' to the newest git-committed render (reproducible)")
    ap.add_argument("--note", help="place: note (basename substring) to insert the embed into")
    ap.add_argument("--width", type=int, default=720, help="place: embed width (default 720)")
    ap.add_argument("--link-style", choices=["markdown", "html", "obsidian"], default=None,
                    help="place: embed syntax (default from labkit config `link_style`, else html). "
                         "html = portable + width; markdown = portable no width; obsidian = wikilink")
    ap.add_argument("--caption", help="place: italic caption line under the embed")
    ap.add_argument("--dpi", type=int, default=300, help="raster DPI (default 300)")
    ap.add_argument("--keep", type=int, default=1,
                    help="prune: newest renders to keep per title (default 1)")
    ap.add_argument("-y", "--yes", action="store_true", help="execute (sync/place/prune default to a dry run)")
    args = ap.parse_args(argv)

    eid, data_dir, note_dir, attach, notes, qmd = _exp_paths(args)
    figs = resolve_figures(data_dir, committed_only=args.committed_only)
    print(f"experiment {eid} — {len(figs)} figure title(s) in MANIFEST(s); {len(notes)} note(s)\n")

    if args.action == "index":
        cmd_index(figs)
        return 0
    if args.action == "drift":
        cmd_drift(figs, eid, notes, _qmd_embed_titles(qmd), attach)
        return 0
    if args.action == "place":
        return cmd_place(args, eid, qmd, figs, notes)
    if args.action == "prune":
        return cmd_prune(data_dir, args.keep, args.yes)
    return cmd_sync(figs, eid, attach, note_dir, notes, args.dpi, args.yes)


if __name__ == "__main__":
    import sys
    sys.exit(main())
