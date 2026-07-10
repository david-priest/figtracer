# Contributing to figtracer

Thanks for your interest in figtracer. It is a small toolkit for reproducible lab
record-keeping — scaffolding experiments, rendering bench protocols, keeping analysis
figures in provenance-tracked sync with plain-text lab notes, and closing the loop with a
git commit. Contributions of all kinds — bug reports, documentation, and code — are welcome.

## Ways to contribute

- **Report a bug** or **request a feature** via a GitHub issue (templates provided).
- **Improve the docs** — the `README.md`, `docs/`, and command help text.
- **Submit code** via a pull request (see below).

## Development setup

figtracer is a Python package (`>=3.11`) that ships three console scripts — `figtracer`,
`labkit`, and `figtools`.

```bash
git clone https://github.com/david-priest/figtracer
cd figtracer
pip install -e ".[dev]"     # runtime + test deps
pytest                       # run the test suite
```

The optional R helper (`r/figtracer.R`) is a standalone, base-R `f2()` shim so R users get
the same figure→manifest→note loop without any extra R package; it is sourced directly, not
installed.

## Pull requests

1. **Branch off the latest `main`:** `git fetch origin && git checkout -b fix/<slug> origin/main`.
2. **Keep it focused** — one logical change per PR. Small PRs review faster and merge cleaner.
3. **Add or update tests** for any behaviour change; keep the suite green (`pytest`).
4. **Open a PR to `main`.** CI (pytest on Python 3.11 / 3.12 / 3.13) must pass before merge.
   Do not push directly to `main`.
5. Describe the *why* as well as the *what* in the PR body.

Please don't bundle unrelated refactors into a feature PR — they make review harder.

## Style

- Match the surrounding code; keep functions small and single-purpose.
- Prefer standard-library and the existing lightweight dependencies over new ones. The
  figure front-ends deliberately duck-type plotting objects so figtracer carries no plotting
  dependency — keep that property.
- Docstrings should say what a module is *for*, not just what it does.

## Reporting security issues

Please do not open a public issue for a security-sensitive report; contact the maintainer
directly (see `CODE_OF_CONDUCT.md` for the contact address).

## Code of conduct

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
