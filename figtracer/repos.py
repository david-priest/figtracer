"""figtracer repos — a read-only health check of the git repositories under ~/code.

    figtracer repos                      # every checkout under ~/code whose origin is yours
    figtracer repos --format markdown    # the same findings as a note
    figtracer repos --owner <login> --root <dir> --stale-days 14 --no-gh

One line per finding, nothing for a clean repository. Exit 0 when nothing needs action, 1
otherwise (including when a check could not run, such as `gh` being unavailable), 2 on a usage
error.

WHY THIS EXISTS
---------------
seekit was tangled for four months (2026-06 to 2026-10-21) by half-finished states that each
looked harmless on the day: a live branch fed by cherry-picks and local-only commits while main
was fed by squash merges; 21 commits never pushed; nine PRs of a planned split left open for
five weeks; about 20 worktrees left behind after their PRs merged; and a `core.hooksPath`
pointing at a folder that existed on no branch, so the pre-push guard never ran. Nothing showed
any of it until the day it was untangled. This check shows each of them while it is small.

WHAT IS CHECKED, per repository (main checkout and every worktree)
------------------------------------------------------------------
  uncommitted       modified, staged or untracked files in the main checkout or a worktree.
  unpushed          the checked-out branch is ahead of origin/<branch>.
  local-only        the branch has no origin/<branch> and commits on no origin ref and no PR
                    head: work that exists on one disk. A detached HEAD whose commits are on no
                    branch is reported the same way. The configured upstream is not used: a
                    worktree made with `worktree add -b feat/x <path> origin/main` tracks
                    origin/main, which says nothing about whether feat/x reached GitHub.
  retire-worktree   a linked worktree whose branch's newest PR is merged or closed.
  stale-record      a worktree record whose folder is gone or has lost its .git file.
  old-pr            an open PR older than --stale-days (14), with its age.
  hooks-path        `core.hooksPath` names a folder that does not exist, in the main checkout
                    or a worktree. Git then runs no hook there, including a guard in .git/hooks.
  live-*            for a repository named under `repos.live_checkouts` in the labkit user
                    config, whose main checkout stays on a live branch that merges origin/<base>
                    and is backed up to a branch of its own: HEAD ahead of that backup,
                    origin/<base> commits not merged into the live branch, and the listed paths
                    differing from origin/<base>. Those comparisons are only as good as the last
                    fetch, so origin/<base> and the backup ref are compared with GitHub through
                    `git ls-remote`, which changes nothing. With no entry, no live checkout is
                    checked. An entry that cannot be used is a `config` finding.

THE LIVE CHECKOUTS, in ~/.config/labkit/config.yaml
---------------------------------------------------
  repos:
    live_checkouts:
      - repo: toolkit            # the repository's name on GitHub, without its owner
        backup: live/checkout    # the branch on origin that backs up the live checkout's HEAD
        base: main               # optional, default main
        paths: [R, man, NAMESPACE, DESCRIPTION, tests]   # compared with origin/<base>

WHAT IT NEVER DOES
------------------
It never fetches, commits, prunes, removes or pushes, and runs git with GIT_OPTIONAL_LOCKS=0 so
that even `git status` does not refresh the index of a tree another agent may be working in.
The remedies in its output are for a person to run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

import yaml

from labkit import config as lkconfig

GIT_TIMEOUT = 60
GH_TIMEOUT = 60


@dataclass(frozen=True)
class LiveSpec:
    backup: str             # the branch on origin that backs up the live checkout's HEAD
    paths: tuple            # package paths that must match origin/<base>
    base: str = "main"


_LIVE_KEYS = ("repo", "backup", "base", "paths")

_GITHUB_URL = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


@dataclass
class Finding:
    repo: str
    kind: str
    text: str


@dataclass
class Worktree:
    path: str
    head: str | None = None
    branch: str | None = None       # short name; None when detached
    is_main: bool = False
    bare: bool = False
    locked: str | None = None


@dataclass
class Repo:
    name: str
    slug: str                       # owner/name on GitHub
    common_dir: str
    worktrees: list = field(default_factory=list)

    @property
    def main(self):
        return next((w for w in self.worktrees if w.is_main and not w.bare), None)


@dataclass
class Report:
    root: str
    owner: str
    repos: list
    skipped: list
    findings: list


class GhUnavailable(Exception):
    pass


# ── git and gh ──────────────────────────────────────────────────────────────────────────────

def _git(cwd, *args, check=True):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       timeout=GIT_TIMEOUT, env=env)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r.stdout if check else r


def _ref_sha(cwd, ref):
    r = _git(cwd, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _count(cwd, *revs):
    return int(_git(cwd, "rev-list", "--count", *revs).strip() or 0)


def gh_login(timeout=GH_TIMEOUT):
    return _gh(["api", "user", "--jq", ".login"], timeout).strip()


def fetch_prs(slug, all_states, timeout=GH_TIMEOUT):
    """PRs of `slug` from gh: every state when a worktree's PR state is needed, else open ones."""
    out = _gh(["pr", "list", "-R", slug, "--state", "all" if all_states else "open",
               "--limit", "2000", "--json",
               "number,state,headRefName,headRefOid,createdAt,mergedAt,closedAt,isDraft,title"],
              timeout)
    return json.loads(out or "[]")


def _gh(args, timeout):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1"))
    except FileNotFoundError:
        raise GhUnavailable("gh is not on PATH")
    except subprocess.TimeoutExpired:
        raise GhUnavailable(f"gh did not answer within {timeout} s")
    if r.returncode != 0:
        msg = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[0]
        raise GhUnavailable(f"gh failed: {msg}")
    return r.stdout


# ── discovery ───────────────────────────────────────────────────────────────────────────────

def github_slug(url):
    m = _GITHUB_URL.search(url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _parse_worktrees(text):
    out = []
    for i, block in enumerate(b for b in text.strip().split("\n\n") if b.strip()):
        wt = Worktree(path="", is_main=(i == 0))
        for line in block.splitlines():
            key, _, val = line.partition(" ")
            if key == "worktree":
                wt.path = val
            elif key == "HEAD":
                wt.head = val
            elif key == "branch":
                wt.branch = val[len("refs/heads/"):] if val.startswith("refs/heads/") else val
            elif key == "bare":
                wt.bare = True
            elif key == "locked":
                wt.locked = val or "locked"
        out.append(wt)
    return out


def discover(root, owner):
    """Repositories with a checkout directly under `root` whose origin is github.com/<owner>/*.

    A worktree folder under `root` stands for its repository, and a symlink to a checkout is
    the checkout, so each repository is listed once. Returns (repos, skipped folder names).
    """
    repos, skipped, seen = {}, [], set()
    for entry in sorted(os.listdir(root), key=str.lower):
        path = os.path.join(root, entry)
        if not os.path.isdir(path) or not os.path.exists(os.path.join(path, ".git")):
            continue
        real = os.path.realpath(path)
        r = _git(real, "rev-parse", "--git-common-dir", check=False)
        if r.returncode != 0:
            skipped.append(f"{entry} (its .git does not lead to a repository)")
            continue
        common = os.path.realpath(os.path.join(real, r.stdout.strip()))
        if common in seen:
            continue
        seen.add(common)
        slug = github_slug(_git(real, "config", "--get", "remote.origin.url", check=False).stdout.strip())
        if not slug or slug.split("/")[0].lower() != owner.lower():
            skipped.append(entry)
            continue
        listed = _git(real, "worktree", "list", "--porcelain", check=False)
        if listed.returncode != 0:
            skipped.append(f"{entry} (git worktree list failed)")
            continue
        wts = _parse_worktrees(listed.stdout)
        main = next((w for w in wts if w.is_main), None)
        if main and not main.bare:
            name = os.path.basename(main.path)
        else:
            name = os.path.basename(common[:-4] if common.endswith(".git") else common)
        repos[common] = Repo(name=name, slug=slug, common_dir=common, worktrees=wts)
    return sorted(repos.values(), key=lambda r: r.name.lower()), skipped


# ── live checkouts ──────────────────────────────────────────────────────────────────────────

def live_checkouts():
    """The live checkouts under `repos.live_checkouts` in the labkit user config.

    Returns ({repository name: LiveSpec}, [problems]). Nothing is assumed about any repository:
    with no entry no live checkout is checked. An entry that cannot be used is a problem, which
    `run` reports as a `config` finding, and its repository is checked as any other.
    """
    where = _short(lkconfig._USER_CONFIG)
    try:
        cfg = lkconfig.user_config()
    except (OSError, yaml.YAMLError) as e:
        mark = getattr(e, "problem_mark", None)
        msg = getattr(e, "problem", None) or getattr(e, "strerror", None) or str(e)
        at = f" at line {mark.line + 1}" if mark else ""
        return {}, [f"{where} could not be read ({msg}{at}); no live checkout is checked"]
    section = cfg.get("repos") if isinstance(cfg, dict) else None
    if section is None:
        return {}, []
    if not isinstance(section, dict):
        return {}, [f"`repos` in {where} is not a mapping; no live checkout is checked"]
    entries = section.get("live_checkouts")
    if entries is None:
        return {}, []
    if not isinstance(entries, list):
        return {}, [f"`repos.live_checkouts` in {where} is not a list; no live checkout is checked"]
    specs, problems = {}, []
    for i, entry in enumerate(entries, 1):
        name, spec, why = _live_spec(entry)
        if why:
            problems.append(f"`repos.live_checkouts` entry {i} in {where} {why}; it is not checked")
        elif name in specs:
            problems.append(f"`repos.live_checkouts` entry {i} in {where} names {name} again; "
                            f"only the first entry for it is checked")
        else:
            specs[name] = spec
    return specs, problems


def _live_spec(entry):
    """(repository name, LiveSpec, None) for a usable entry, else (None, None, what is wrong)."""
    def text(v):
        return isinstance(v, str) and bool(v.strip())

    if not isinstance(entry, dict):
        return None, None, "is not a mapping"
    unknown = sorted(str(k) for k in entry if k not in _LIVE_KEYS)
    if unknown:
        return None, None, f"has unknown keys {', '.join(unknown)} (known: {', '.join(_LIVE_KEYS)})"
    name, backup, base, paths = (entry.get(k) for k in _LIVE_KEYS)
    if not text(name):
        return None, None, "needs `repo`, the repository's name on GitHub"
    if "/" in name:
        return None, None, f"has `repo: {name}`; give the repository's name without its owner"
    if not text(backup):
        return None, None, "needs `backup`, the branch on origin that backs up the live checkout"
    if base is not None and not text(base):
        return None, None, "has a `base` that is not a branch name"
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths or not all(text(p) for p in paths):
        return None, None, "needs `paths`, a list of the paths compared with the base branch"
    spec = LiveSpec(backup.strip(), tuple(p.strip() for p in paths), (base or "main").strip())
    return name.strip(), spec, None


# ── checks ──────────────────────────────────────────────────────────────────────────────────

def _short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if home and path.startswith(home + os.sep) else path


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _sample(items, n=3):
    items = list(items)
    more = f", +{len(items) - n} more" if len(items) > n else ""
    return ", ".join(items[:n]) + more


def _label(wt):
    if wt.is_main:
        return f"main checkout ({wt.branch or 'detached HEAD'})"
    return f"worktree {_short(wt.path)} ({wt.branch or 'detached HEAD'})"


def _alive(wt):
    if wt.bare:
        return False
    dotgit = os.path.join(wt.path, ".git")
    return os.path.isdir(dotgit) if wt.is_main else os.path.isfile(dotgit)


def check_uncommitted(wt):
    lines = [ln for ln in _git(wt.path, "status", "--porcelain").splitlines() if ln.strip()]
    if not lines:
        return None
    untracked = sum(ln.startswith("??") for ln in lines)
    parts = [p for p in (_plural(len(lines) - untracked, "modified") if len(lines) > untracked else "",
                         f"{untracked} untracked" if untracked else "") if p]
    return f"{_label(wt)}: uncommitted: {', '.join(parts)} ({_sample(ln.strip() for ln in lines)})"


def check_pushed(wt, prs_by_branch):
    """The checked-out branch against origin/<branch>, or against every origin ref and PR head."""
    if wt.branch:
        if _ref_sha(wt.path, f"refs/remotes/origin/{wt.branch}"):
            ahead = _count(wt.path, f"refs/remotes/origin/{wt.branch}..HEAD")
            if ahead:
                return ("unpushed",
                        f"{_label(wt)}: {_plural(ahead, 'commit')} ahead of origin/{wt.branch}, not pushed")
            return None
        # No copy under its own name. A squash-merged PR whose branch was deleted on GitHub
        # still holds the commits in its head, so a PR head that is here counts as a copy.
        heads = [p["headRefOid"] for p in prs_by_branch.get(wt.branch, [])
                 if p.get("headRefOid") and _ref_sha(wt.path, p["headRefOid"])]
        only = _count(wt.path, "HEAD", "--not", "--remotes=origin", *heads)
        if only:
            return ("local-only", f"{_label(wt)}: no copy on GitHub, "
                                  f"{_plural(only, 'commit')} only on this disk")
        return None
    if wt.head:
        only = _count(wt.path, "HEAD", "--not", "--remotes=origin", "--branches", "--tags")
        if only:
            return ("local-only", f"{_label(wt)} at {wt.head[:7]}: {_plural(only, 'commit')} "
                                  f"on no branch and not on GitHub")
    return None


def check_retire(wt, prs_by_branch):
    prs = prs_by_branch.get(wt.branch) if wt.branch else None
    if not prs or any(p["state"] == "OPEN" for p in prs):
        return None
    pr = max(prs, key=lambda p: p["number"])
    if pr["state"] == "MERGED":
        when = (pr.get("mergedAt") or "")[:10]
        return f"{_label(wt)}: PR #{pr['number']} merged {when}; retire the worktree"
    when = (pr.get("closedAt") or "")[:10]
    return f"{_label(wt)}: PR #{pr['number']} closed unmerged {when}; retire the worktree"


def check_stale_record(wt):
    if wt.is_main or wt.bare:
        return None
    locked = f" (locked: {wt.locked})" if wt.locked else ""
    if not os.path.isdir(wt.path):
        return f"worktree record {_short(wt.path)}{locked}: the folder is gone (git worktree prune drops the record)"
    if not os.path.isfile(os.path.join(wt.path, ".git")):
        return f"worktree record {_short(wt.path)}{locked}: the folder has lost its .git file"
    return None


def check_hooks_path(repo):
    main = repo.main
    probe = main.path if main else repo.common_dir
    value = _git(probe, "config", "--get", "core.hooksPath", check=False).stdout.strip()
    if not value:
        return None
    expanded = os.path.expanduser(value)
    missing = []
    for wt in repo.worktrees:
        if wt.bare or not _alive(wt):
            continue
        target = expanded if os.path.isabs(expanded) else os.path.join(wt.path, expanded)
        if not os.path.isdir(target):
            missing.append(wt)
    if not missing:
        return None
    where = []
    if any(w.is_main for w in missing):
        where.append("the main checkout")
    linked = [_short(w.path) for w in missing if not w.is_main]
    if linked:
        where.append(f"{_plural(len(linked), 'worktree')} ({_sample(linked)})")
    return (f'core.hooksPath is "{value}", which does not exist in {" and ".join(where)}: '
            f"git runs no hook there, including any pre-push guard in .git/hooks")


def check_old_prs(prs, now, stale_days):
    out = []
    for pr in sorted(prs, key=lambda p: p["createdAt"]):
        if pr["state"] != "OPEN":
            continue
        created = dt.datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        age = (now - created).days
        if age > stale_days:
            draft = ", draft" if pr.get("isDraft") else ""
            title = pr.get("title", "")
            title = title if len(title) <= 70 else title[:67] + "..."
            out.append(f"PR #{pr['number']} open {age} days ({pr['headRefName']}{draft}): {title}")
    return out


def _ls_remote(cwd, refs):
    try:
        r = _git(cwd, "ls-remote", "origin", *refs, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git ls-remote did not answer within {GIT_TIMEOUT} s")
    if r.returncode != 0:
        msg = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[0]
        raise RuntimeError(msg)
    found = {}
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        found[ref] = sha
    return found


def check_live(repo, spec):
    """The live checkout against its backup branch and origin/<base>."""
    main = repo.main
    if not main or not _alive(main):
        return []
    out, cwd, here = [], main.path, _short(main.path)
    base, backup = f"origin/{spec.base}", f"origin/{spec.backup}"

    try:
        remote = _ls_remote(cwd, [f"refs/heads/{spec.base}", f"refs/heads/{spec.backup}"])
        for name in (spec.base, spec.backup):
            local, gh = _ref_sha(cwd, f"refs/remotes/origin/{name}"), remote.get(f"refs/heads/{name}")
            if gh and local != gh:
                out.append(("live-fetch", f"origin/{name} here is {(local or 'missing')[:7]} but GitHub "
                                          f"has {gh[:7]}; the live checks use the local copy "
                                          f"(git -C {here} fetch origin, then run this again)"))
    except RuntimeError as e:
        out.append(("live-fetch", f"could not compare origin/{spec.base} with GitHub ({e}); "
                                  f"the live checks use the local copy"))

    if _ref_sha(cwd, f"refs/remotes/{backup}"):
        ahead = _count(cwd, f"refs/remotes/{backup}..HEAD")
        if ahead:
            out.append(("live-unpushed",
                        f"live branch {main.branch or 'HEAD'} is {_plural(ahead, 'commit')} ahead of "
                        f"{backup} (git -C {here} push origin HEAD:refs/heads/{spec.backup})"))
    else:
        out.append(("live-unpushed", f"{backup} does not exist here; the live checkout has no backup "
                                     f"on GitHub (git -C {here} push origin HEAD:refs/heads/{spec.backup})"))

    if not _ref_sha(cwd, f"refs/remotes/{base}"):
        out.append(("live-behind", f"{base} does not exist here, so the live checkout cannot be compared"))
        return out
    behind = _count(cwd, f"HEAD..refs/remotes/{base}")
    if behind:
        out.append(("live-behind", f"{base} has {_plural(behind, 'commit')} not merged into the live "
                                   f"branch (git -C {here} merge {base})"))
    changed = set(_git(cwd, "diff", "--name-only", f"refs/remotes/{base}", "--", *spec.paths).splitlines())
    changed |= set(_git(cwd, "ls-files", "--others", "--exclude-standard", "--", *spec.paths).splitlines())
    if changed:
        names = ", ".join(p + "/" if os.path.isdir(os.path.join(cwd, p)) else p for p in spec.paths)
        out.append(("live-differs", f"{names} differ from {base} in {_plural(len(changed), 'file')}: "
                                    f"{_sample(sorted(changed))}"))
    return out


def check_repo(repo, prs, now, stale_days, live=None):
    """Every finding for one repository. `prs` is gh's list, or a GhUnavailable for this repo."""
    found = []

    def add(kind, text):
        if text:
            found.append(Finding(repo.name, kind, text))

    add("hooks-path", check_hooks_path(repo))
    spec = (live or {}).get(repo.slug.split("/")[1])
    if spec:
        for kind, text in check_live(repo, spec):
            add(kind, text)

    pr_error = prs if isinstance(prs, GhUnavailable) else None
    prs = [] if pr_error else (prs or [])
    by_branch = {}
    for pr in prs:
        by_branch.setdefault(pr["headRefName"], []).append(pr)

    main_first = sorted(repo.worktrees, key=lambda w: (not w.is_main, w.path.lower()))
    for wt in main_first:
        if wt.bare:
            continue
        stale = check_stale_record(wt)
        if stale:
            add("stale-record", stale)
            continue
        add("uncommitted", check_uncommitted(wt))
        if not (spec and wt.is_main):
            pushed = check_pushed(wt, by_branch)
            if pushed:
                add(*pushed)
        if not wt.is_main and not pr_error:
            add("retire-worktree", check_retire(wt, by_branch))

    if pr_error:
        add("gh", f"PR state for {repo.slug} unavailable ({pr_error}); worktree and open-PR checks skipped")
    for text in check_old_prs(prs, now, stale_days):
        add("old-pr", text)
    return found


def run(root, owner, prs_for=fetch_prs, now=None, stale_days=14, live=None, jobs=8):
    """Discover and check every repository. `prs_for(slug, all_states)` is None to skip gh;
    `live` is None to read the live checkouts from the user config."""
    root = os.path.realpath(os.path.expanduser(root))
    now = now or dt.datetime.now(dt.timezone.utc)
    problems = []
    if live is None:
        live, problems = live_checkouts()
    repos, skipped = discover(root, owner)

    def one(repo):
        prs = None
        if prs_for is not None:
            needs_all = any(not w.is_main and w.branch for w in repo.worktrees)
            try:
                prs = prs_for(repo.slug, needs_all)
            except GhUnavailable as e:
                prs = e
        try:
            return check_repo(repo, prs, now, stale_days, live)
        except (subprocess.SubprocessError, OSError) as e:
            detail = (getattr(e, "stderr", "") or str(e)).strip().splitlines()
            return [Finding(repo.name, "error", f"the check did not finish: {detail[0] if detail else e}")]

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(one, repos))
    findings = [Finding("config", "config", p) for p in problems]
    return Report(root, owner, repos, skipped, findings + [f for fs in results for f in fs])


# ── output ──────────────────────────────────────────────────────────────────────────────────

def _summary(report):
    total = len(report.repos)
    repos = "repository" if total == 1 else "repositories"
    if not report.findings:
        return f"No findings in {total} {repos}."
    n_repos = len({f.repo for f in report.findings})
    return f"{_plural(len(report.findings), 'finding')} in {n_repos} of {total} {repos}."


def render_text(report, stamp):
    lines = [f"figtracer repos: {_short(report.root)}, owner {report.owner}, {stamp}"]
    lines += [f"{f.repo}: {f.text}" for f in report.findings]
    lines.append(_summary(report))
    if report.skipped:
        lines.append(f"Not checked (origin is not github.com/{report.owner}): {', '.join(report.skipped)}")
    return "\n".join(lines) + "\n"


def render_markdown(report, stamp):
    out = ["# Repo health", "",
           f"`figtracer repos` over `{_short(report.root)}` (owner {report.owner}), {stamp}. "
           f"{_summary(report)}", ""]
    current = None
    for f in report.findings:
        if f.repo != current:
            if current is not None:
                out.append("")
            out += [f"## {f.repo}", ""]
            current = f.repo
        out.append(f"- {f.text}")
    if report.skipped:
        out += ["", "## Not checked", "",
                f"Origin is not github.com/{report.owner}: {', '.join(report.skipped)}."]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(
        prog="figtracer repos",
        description="read-only health check of the git repositories under a folder: uncommitted "
                    "work, unpushed or local-only branches, worktrees to retire, stale worktree "
                    "records, old open PRs, a missing core.hooksPath, and the live checkout")
    p.add_argument("--root", default="~/code", help="folder whose checkouts are checked (default ~/code)")
    p.add_argument("--owner", help="GitHub owner whose repositories are checked (default: the gh login)")
    p.add_argument("--stale-days", type=int, default=14, help="an open PR older than this is reported")
    p.add_argument("--format", choices=("text", "markdown"), default="text")
    p.add_argument("--no-gh", action="store_true", help="skip the PR checks deliberately")
    p.add_argument("--timeout", type=int, default=GH_TIMEOUT, help="seconds to wait for each gh call")
    args = p.parse_args(argv)

    root = os.path.expanduser(args.root)
    if not os.path.isdir(root):
        print(f"figtracer repos: no such folder: {root}", file=sys.stderr)
        return 2
    if shutil.which("git") is None:
        print("figtracer repos: git is not on PATH", file=sys.stderr)
        return 2

    gh_problem = None
    owner = args.owner
    if not owner:
        if args.no_gh:
            print("figtracer repos: --no-gh needs --owner", file=sys.stderr)
            return 2
        try:
            owner = gh_login(args.timeout)
        except GhUnavailable as e:
            print(f"figtracer repos: cannot tell whose repositories to check ({e}); pass --owner",
                  file=sys.stderr)
            return 2

    prs_for = None
    if not args.no_gh:
        if shutil.which("gh") is None:
            gh_problem = "gh is not on PATH"
        else:
            def prs_for(slug, all_states):
                return fetch_prs(slug, all_states, args.timeout)

    report = run(root, owner, prs_for=prs_for, stale_days=args.stale_days)
    if gh_problem:
        report.findings.insert(0, Finding("gh", "gh", f"{gh_problem}; every PR check was skipped "
                                                       f"(--no-gh skips them deliberately)"))
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    render = render_markdown if args.format == "markdown" else render_text
    sys.stdout.write(render(report, stamp))
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
