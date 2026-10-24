# Contributing

## Setup

You need Python 3.11 or newer, and Docker with Compose if you want a local
OpenObserve.

```bash
git clone https://github.com/sqlalchemy-openobserve/sqlalchemy-openobserve.git
cd sqlalchemy-openobserve
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

To run code against a real server, see
[A local OpenObserve](README.md#a-local-openobserve) in the README.

## Before you push

Run the same checks CI runs:

```bash
ruff check .
ruff format --check .
mypy --strict src
pytest
```

`ruff format .` fixes formatting, and `ruff check --fix .` fixes import order
and some other lint. CI runs these on Python 3.11, 3.12 and 3.13, on every
pull request and every push to `main`.

## Tests

Name new test files after the module they test, for example
`tests/test_timerange.py` for `timerange.py`.

CI does not start an OpenObserve server, so everything under `tests/` must
pass without one. Integration tests against a live server are not set up yet.

## Branches and pull requests

`main` is protected. Nobody can push to it directly, and every change goes
through a pull request.

1. Branch off an up-to-date `main`, and push the new branch once:

   ```bash
   git fetch origin
   git switch --no-track -c my-change origin/main
   git push -u origin my-change
   ```

   After that, a plain `git push` works.
2. Keep each pull request to one change. One module plus its tests is a good
   size.
3. Before opening the pull request, and whenever `main` moves, rebase:

   ```bash
   git fetch origin
   git rebase origin/main
   git push --force-with-lease
   ```

   GitHub will not merge a branch that is behind `main`.
4. A pull request merges when:
   - one other team member has approved it, and
   - `test (3.11)`, `test (3.12)` and `test (3.13)` pass.

   Pushing new commits dismisses earlier approvals, so ask for a fresh
   review.

   Admins also see an option to merge without meeting these requirements.
   Leave it for emergencies.
5. We recommend **Squash and merge**, with a commit message that says what
   the change does. It keeps `main` to one commit per pull request.

## Changelog

If your change affects people who use the package, add a line under
`## Unreleased` in [CHANGELOG.md](CHANGELOG.md).
