"""figtracer repos: the read-only health check of a folder of git checkouts.

Every repository here is synthetic: bare remotes in a temp folder stand in for GitHub, reached
through a `url.<base>.insteadOf` rule in a temp HOME, so `remote.origin.url` still reads as a
github.com URL while fetch, push and ls-remote stay on disk. PR state is a fake `prs_for`. The
labkit user config is a file in the temp HOME, absent unless a test writes it.
"""
import datetime as dt
import os
import subprocess

import pytest

from figtracer import cli, repos
from labkit import config as lkconfig

OWNER = "example-lab"
NOW = dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class Lab:
    def __init__(self, tmp):
        self.tmp, self.code, self.remotes = tmp, tmp / "code", tmp / "remotes"
        self.code.mkdir()
        self.remotes.mkdir()

    def repo(self, name="repo-a", owner=OWNER):
        bare = self.remotes / owner / f"{name}.git"
        bare.parent.mkdir(exist_ok=True)
        git(self.tmp, "init", "--bare", str(bare))
        git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
        work = self.code / name
        git(self.tmp, "init", str(work))
        git(work, "symbolic-ref", "HEAD", "refs/heads/main")
        self.commit(work, "README.md", "readme\n")
        git(work, "remote", "add", "origin", f"https://github.com/{owner}/{name}.git")
        git(work, "push", "-u", "origin", "main")
        return work

    def commit(self, work, fname="a.txt", text="x\n"):
        path = work / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(work, "add", fname)
        git(work, "commit", "-m", f"add {fname}")
        return git(work, "rev-parse", "HEAD")

    def worktree(self, work, branch, start="origin/main"):
        path = self.code / f"{work.name}-{branch.split('/')[-1]}"
        git(work, "worktree", "add", "-b", branch, str(path), start)
        return path

    def clone(self, name, owner=OWNER):
        other = self.tmp / f"other-{name}"
        git(self.tmp, "clone", f"https://github.com/{owner}/{name}.git", str(other))
        return other

    def config(self, text):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(text)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, val in {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                     "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, val)
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        f'[url "{tmp_path / "remotes"}/"]\n\tinsteadOf = https://github.com/\n')
    lab = Lab(tmp_path)
    # labkit fixed the config path at import, so the temp HOME alone does not move it
    lab.config_path = home / ".config" / "labkit" / "config.yaml"
    monkeypatch.setattr(lkconfig, "_USER_CONFIG", str(lab.config_path))
    return lab


def pr(number, branch, state="OPEN", created="2026-10-15T00:00:00Z", merged=None, closed=None,
       oid=None, title="Synthetic change", draft=False):
    return {"number": number, "headRefName": branch, "state": state, "createdAt": created,
            "mergedAt": merged, "closedAt": closed, "headRefOid": oid, "title": title,
            "isDraft": draft}


def check(lab, prs=(), live=None, stale_days=14):
    return repos.run(str(lab.code), OWNER, prs_for=lambda slug, all_states: list(prs), now=NOW,
                     stale_days=stale_days, live=live or {})


def kinds(report):
    return sorted((f.repo, f.kind) for f in report.findings)


def texts(report, kind):
    return [f.text for f in report.findings if f.kind == kind]


def test_a_clean_repository_reports_nothing(lab):
    lab.repo()
    report = check(lab)
    assert [r.name for r in report.repos] == ["repo-a"]
    assert report.findings == []
    assert repos.render_text(report, "STAMP").splitlines()[-1] == "No findings in 1 repository."


def test_the_check_changes_no_repository_state(lab):
    work = lab.repo()
    wt = lab.worktree(work, "feat/x")
    (work / "README.md").write_text("edited\n")
    (wt / "new.txt").write_text("new\n")

    def state():
        return (git(work, "for-each-ref"), git(work, "worktree", "list", "--porcelain"),
                os.stat(work / ".git" / "index").st_mtime_ns,
                sorted(os.listdir(work / ".git")), git(work, "config", "--list", "--local"))

    before = state()
    check(lab, [pr(1, "feat/x", "MERGED", merged="2026-10-16T00:00:00Z")])
    assert state() == before


def test_uncommitted_changes_in_the_main_checkout_and_a_worktree(lab):
    work = lab.repo()
    wt = lab.worktree(work, "feat/x")
    (work / "README.md").write_text("edited\n")
    (wt / "scratch.txt").write_text("new\n")
    found = texts(check(lab, [pr(1, "feat/x")]), "uncommitted")
    assert found == ["main checkout (main): uncommitted: 1 modified (M README.md)",
                     f"worktree {wt} (feat/x): uncommitted: 1 untracked (?? scratch.txt)"]


def test_a_commit_not_pushed_is_reported_against_origin_branch(lab):
    work = lab.repo()
    lab.commit(work)
    assert texts(check(lab), "unpushed") == [
        "main checkout (main): 1 commit ahead of origin/main, not pushed"]

    wt = lab.worktree(work, "feat/y")
    lab.commit(wt, "b.txt")
    git(wt, "push", "-u", "origin", "feat/y")
    lab.commit(wt, "c.txt")
    assert f"worktree {wt} (feat/y): 1 commit ahead of origin/feat/y, not pushed" in texts(
        check(lab, [pr(2, "feat/y")]), "unpushed")


def test_a_branch_never_pushed_is_local_only_whatever_it_tracks(lab):
    work = lab.repo()
    wt = lab.worktree(work, "feat/x")           # may track origin/main, which proves nothing
    lab.worktree(work, "feat/empty")             # no commits of its own: nothing at risk
    lab.commit(wt, "b.txt")
    lab.commit(wt, "c.txt")
    assert texts(check(lab), "local-only") == [
        f"worktree {wt} (feat/x): no copy on GitHub, 2 commits only on this disk"]


def test_a_pr_head_counts_as_the_github_copy(lab):
    """A squash-merged PR whose branch was deleted still holds its commits in the PR head."""
    work = lab.repo()
    wt = lab.worktree(work, "feat/z")
    head = lab.commit(wt, "b.txt")
    report = check(lab, [pr(3, "feat/z", "MERGED", merged="2026-10-18T00:00:00Z", oid=head)])
    assert texts(report, "local-only") == []
    assert texts(report, "retire-worktree") == [
        f"worktree {wt} (feat/z): PR #3 merged 2026-10-18; retire the worktree"]


def test_detached_commits_on_no_branch_are_local_only(lab):
    work = lab.repo()
    path = lab.code / "repo-a-detached"
    git(work, "worktree", "add", "--detach", str(path), "origin/main")
    head = lab.commit(path, "b.txt")
    assert texts(check(lab), "local-only") == [
        f"worktree {path} (detached HEAD) at {head[:7]}: 1 commit on no branch and not on GitHub"]


def test_worktrees_whose_newest_pr_is_merged_or_closed_are_retired(lab):
    work = lab.repo()
    paths = {b: lab.worktree(work, b) for b in ("feat/m", "feat/c", "feat/o", "feat/r", "feat/n")}
    report = check(lab, [
        pr(1, "feat/m", "MERGED", merged="2026-10-10T00:00:00Z"),
        pr(2, "feat/c", "CLOSED", closed="2026-10-11T00:00:00Z"),
        pr(3, "feat/o", "OPEN"),
        pr(4, "feat/r", "CLOSED", closed="2026-10-12T00:00:00Z"),
        pr(5, "feat/r", "OPEN"),                 # reopened as a new PR: still in use
    ])
    assert texts(report, "retire-worktree") == [
        f"worktree {paths['feat/c']} (feat/c): PR #2 closed unmerged 2026-10-11; retire the worktree",
        f"worktree {paths['feat/m']} (feat/m): PR #1 merged 2026-10-10; retire the worktree",
    ]


def test_worktree_records_whose_folder_lost_its_git_file(lab):
    work = lab.repo()
    lost = lab.worktree(work, "feat/g")
    gone = lab.worktree(work, "feat/h")
    os.remove(lost / ".git")
    os.rename(gone, lab.tmp / "moved-away")
    report = check(lab)
    assert texts(report, "stale-record") == [
        f"worktree record {lost}: the folder has lost its .git file",
        f"worktree record {gone}: the folder is gone (git worktree prune drops the record)",
    ]
    assert kinds(report) == [("repo-a", "stale-record")] * 2


def test_open_prs_older_than_the_limit_with_their_age(lab):
    lab.repo()
    report = check(lab, [
        pr(7, "feat/old", created="2026-10-01T00:00:00Z", title="An old change", draft=True),
        pr(8, "feat/new", created="2026-10-11T00:00:00Z"),
        pr(9, "feat/done", "MERGED", created="2026-09-01T00:00:00Z", merged="2026-09-02T00:00:00Z"),
    ])
    assert texts(report, "old-pr") == ["PR #7 open 20 days (feat/old, draft): An old change"]


def test_a_hooks_path_that_does_not_exist(lab):
    work = lab.repo()
    git(work, "config", "core.hooksPath", "tools/hooks")
    assert texts(check(lab), "hooks-path") == [
        'core.hooksPath is "tools/hooks", which does not exist in the main checkout: '
        "git runs no hook there, including any pre-push guard in .git/hooks"]

    (work / "tools" / "hooks").mkdir(parents=True)     # present here, on no branch
    wt = lab.worktree(work, "feat/x")
    assert texts(check(lab, [pr(1, "feat/x")]), "hooks-path") == [
        f'core.hooksPath is "tools/hooks", which does not exist in 1 worktree ({wt}): '
        "git runs no hook there, including any pre-push guard in .git/hooks"]


LIVE = {"repo-live": repos.LiveSpec("live/checkout", ("R", "NAMESPACE"))}


def _live_repo(lab):
    work = lab.repo("repo-live")
    lab.commit(work, "R/f.R", "f <- 1\n")
    lab.commit(work, "NAMESPACE", "export(f)\n")
    git(work, "push", "origin", "main")
    git(work, "checkout", "-b", "fix/live")
    lab.commit(work, "dev/notes.txt")                   # a folder main does not carry
    git(work, "push", "origin", "HEAD:refs/heads/live/checkout")
    return work


def test_a_live_checkout_in_step_with_main_and_its_backup(lab):
    _live_repo(lab)
    assert check(lab, live=LIVE).findings == []


def _drift(lab, work):
    """Main moves on, the live branch gains a commit its backup lacks, and R/f.R is edited."""
    other = lab.clone("repo-live")
    lab.commit(other, "R/g.R", "g <- 2\n")
    git(other, "push", "origin", "main")
    git(work, "fetch", "origin")
    lab.commit(work, "dev/more.txt")
    (work / "R" / "f.R").write_text("f <- 3\n")


def test_a_live_checkout_behind_main_ahead_of_its_backup_and_differing(lab):
    work = _live_repo(lab)
    _drift(lab, work)
    report = check(lab, live=LIVE)
    assert kinds(report) == [("repo-live", "live-behind"), ("repo-live", "live-differs"),
                             ("repo-live", "live-unpushed"), ("repo-live", "uncommitted")]
    assert texts(report, "live-unpushed") == [
        f"live branch fix/live is 1 commit ahead of origin/live/checkout "
        f"(git -C {work} push origin HEAD:refs/heads/live/checkout)"]
    assert texts(report, "live-behind") == [
        f"origin/main has 1 commit not merged into the live branch (git -C {work} merge origin/main)"]
    assert texts(report, "live-differs") == [
        "R/, NAMESPACE differ from origin/main in 2 files: R/f.R, R/g.R"]


def test_a_live_checkout_compared_against_a_stale_fetch_says_so(lab):
    work = _live_repo(lab)
    other = lab.clone("repo-live")
    gh_sha = lab.commit(other, "R/g.R", "g <- 2\n")
    git(other, "push", "origin", "main")
    found = texts(check(lab, live=LIVE), "live-fetch")
    assert len(found) == 1
    assert found[0].startswith("origin/main here is ") and f"GitHub has {gh_sha[:7]}" in found[0]
    assert f"git -C {work} fetch origin" in found[0]


def from_config(lab, prs=()):
    """The check with the live checkouts read from the user config, as the command reads them."""
    return repos.run(str(lab.code), OWNER, prs_for=lambda slug, all_states: list(prs), now=NOW)


LIVE_CONFIG = """\
repos:
  live_checkouts:
    - repo: repo-live
      backup: live/checkout
      base: main
      paths: [R, NAMESPACE]
"""


def test_no_live_checkout_is_checked_without_a_config_entry(lab):
    work = _live_repo(lab)
    _drift(lab, work)
    for text in (None, "vault_root: /vault\n", "repos:\n", "repos:\n  live_checkouts: []\n"):
        if text is not None:
            lab.config(text)
        assert repos.live_checkouts() == ({}, [])
        # the main checkout is checked as any other: its edit, and its commit on no origin ref
        assert kinds(from_config(lab)) == [("repo-live", "local-only"), ("repo-live", "uncommitted")]


def test_a_configured_live_checkout_reports_as_before(lab):
    work = _live_repo(lab)
    lab.config(LIVE_CONFIG)
    assert repos.live_checkouts() == (LIVE, [])
    assert from_config(lab).findings == []

    _drift(lab, work)
    report = from_config(lab)
    assert report.findings == check(lab, live=LIVE).findings
    assert kinds(report) == [("repo-live", "live-behind"), ("repo-live", "live-differs"),
                             ("repo-live", "live-unpushed"), ("repo-live", "uncommitted")]


def test_a_live_checkout_entry_that_cannot_be_used_is_a_config_finding(lab, capsys):
    _live_repo(lab)
    lab.config("""\
repos:
  live_checkouts:
    - repo-live
    - {repo: repo-live, backup: live/checkout}
    - {repo: repo-live, backup: live/checkout, paths: [R], bsae: dev}
    - {repo: example-lab/repo-live, backup: live/checkout, paths: [R]}
    - {repo: repo-live, paths: [R]}
    - {repo: repo-live, backup: live/checkout, paths: [R, NAMESPACE]}
    - {repo: repo-live, backup: other, paths: [R]}
""")
    where = "`repos.live_checkouts` entry {} in ~/.config/labkit/config.yaml"
    specs, problems = repos.live_checkouts()
    assert specs == LIVE                                # entry 6, with base defaulting to main
    assert problems == [
        f"{where.format(1)} is not a mapping; it is not checked",
        f"{where.format(2)} needs `paths`, a list of the paths compared with the base branch; "
        "it is not checked",
        f"{where.format(3)} has unknown keys bsae (known: repo, backup, base, paths); "
        "it is not checked",
        f"{where.format(4)} has `repo: example-lab/repo-live`; give the repository's name "
        "without its owner; it is not checked",
        f"{where.format(5)} needs `backup`, the branch on origin that backs up the live "
        "checkout; it is not checked",
        f"{where.format(7)} names repo-live again; only the first entry for it is checked",
    ]
    report = from_config(lab)
    assert texts(report, "config") == problems
    assert kinds(report) == [("config", "config")] * 6

    for text, problem in [
            ("repos: [repo-live]\n", "`repos` in ~/.config/labkit/config.yaml is not a mapping"),
            ("repos:\n  live_checkouts: {repo: repo-live}\n",
             "`repos.live_checkouts` in ~/.config/labkit/config.yaml is not a list"),
            ("repos:\n  live_checkouts: [unclosed\n",
             "~/.config/labkit/config.yaml could not be read (expected ',' or ']', but got "
             "'<stream end>' at line 3)")]:
        lab.config(text)
        assert texts(from_config(lab), "config") == [f"{problem}; no live checkout is checked"]

    assert repos.main(["--root", str(lab.code), "--owner", OWNER, "--no-gh"]) == 1
    assert "config: ~/.config/labkit/config.yaml could not be read" in capsys.readouterr().out


def test_gh_unavailable_for_a_repository_is_a_finding(lab):
    work = lab.repo()
    lab.worktree(work, "feat/x")

    def broken(slug, all_states):
        raise repos.GhUnavailable("gh did not answer within 60 s")

    report = repos.run(str(lab.code), OWNER, prs_for=broken, now=NOW, live={})
    assert texts(report, "gh") == [
        f"PR state for {OWNER}/repo-a unavailable (gh did not answer within 60 s); "
        "worktree and open-PR checks skipped"]


def test_discovery_keeps_one_entry_per_repository_and_only_the_owner(lab):
    work = lab.repo("repo-a")
    lab.repo("repo-b", owner="other-lab")
    lab.worktree(work, "feat/x")                        # repo-a-x under the root
    os.symlink(work, lab.code / "repo-a-link")
    (lab.code / "plain-folder").mkdir()
    broken = lab.code / "broken-wt"
    broken.mkdir()
    (broken / ".git").write_text(f"gitdir: {lab.tmp / 'nowhere'}\n")
    found, skipped = repos.discover(str(lab.code), OWNER)
    assert [(r.name, r.slug) for r in found] == [("repo-a", f"{OWNER}/repo-a")]
    assert len(found[0].worktrees) == 2
    assert skipped == ["broken-wt (its .git does not lead to a repository)", "repo-b"]


def test_github_slug_reads_https_and_ssh_urls():
    assert repos.github_slug("https://github.com/example-lab/repo-a.git") == "example-lab/repo-a"
    assert repos.github_slug("git@github.com:example-lab/repo-a.git") == "example-lab/repo-a"
    assert repos.github_slug("ssh://git@github.com/example-lab/repo-a") == "example-lab/repo-a"
    assert repos.github_slug("/srv/git/repo-a.git") is None


@pytest.fixture
def fake_gh(monkeypatch):
    calls = []

    def fetch(slug, all_states, timeout=60):
        calls.append((slug, all_states))
        return [pr(1, "feat/x", "MERGED", merged="2026-10-16T00:00:00Z")]

    monkeypatch.setattr(repos, "fetch_prs", fetch)
    monkeypatch.setattr(repos.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_cli_exit_codes_and_markdown(lab, fake_gh, capsys):
    work = lab.repo()
    args = ["--root", str(lab.code), "--owner", OWNER]
    assert repos.main(args) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "No findings in 1 repository."
    assert fake_gh == [(f"{OWNER}/repo-a", False)]       # no worktree: open PRs only

    wt = lab.worktree(work, "feat/x")
    assert repos.main(args + ["--format", "markdown"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("# Repo health\n")
    assert "1 finding in 1 of 1 repository." in out
    assert f"## repo-a\n\n- worktree {wt} (feat/x): PR #1 merged 2026-10-16; retire the worktree\n" in out
    assert fake_gh[-1] == (f"{OWNER}/repo-a", True)


def test_cli_without_gh_says_so_and_fails(lab, monkeypatch, capsys):
    lab.repo()
    monkeypatch.setattr(repos.shutil, "which", lambda name: None if name == "gh" else "/usr/bin/git")
    assert repos.main(["--root", str(lab.code), "--owner", OWNER]) == 1
    assert "gh: gh is not on PATH; every PR check was skipped" in capsys.readouterr().out
    assert repos.main(["--root", str(lab.code), "--owner", OWNER, "--no-gh"]) == 0
    assert repos.main(["--root", str(lab.code), "--no-gh"]) == 2


def test_figtracer_dispatches_repos(lab):
    lab.repo()
    assert cli.main(["repos", "--root", str(lab.code), "--owner", OWNER, "--no-gh"]) == 0
