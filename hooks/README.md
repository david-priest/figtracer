# hooks/ — committed git hooks (multi-agent hygiene)

Version-controlled hooks so every clone (including fresh machines and cloud agents) can enforce
the same guards — not just whoever hand-installed a local hook.

## Activate (one-time per clone)

```bash
git config core.hooksPath hooks
```

That points git at this directory instead of `.git/hooks`, and it applies to every worktree of
the clone. (A worktree-local hook in `.git/hooks/pre-push` provides the same guard until you do
this; once `core.hooksPath` is set, the committed versions here take over.)

## What's here

- **`pre-push`** — hard-blocks direct pushes to `main`/`master` (PR-only, CI-gated). Feature-branch
  pushes pass. Real-emergency bypass: `git push --no-verify` (discouraged — you almost never want it).

## Note for maintainers

`core.hooksPath` currently needs the one-time command above. Follow-up worth doing: have
`figtracer init` set it automatically when run inside the repo, so a fresh clone is guarded with
no manual step. See the `repo-hygiene` skill and each repo's `AGENTS.md` "Git & multi-agent
hygiene" section for the rules these hooks enforce.
