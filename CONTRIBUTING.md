# Contributing

Thanks for your interest in improving `static-hosting-mcp`. This guide covers the
local development workflow and what CI expects from a pull request. For a tour of
the architecture and how the server is tested live, see [`CLAUDE.md`](CLAUDE.md);
for what the server *is* and how an operator deploys it, see [`README.md`](README.md).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (handles Python, the virtualenv, and deps)
- Python 3.11+ (uv will install a suitable interpreter if you don't have one)

You do **not** need a Google Cloud bucket or credentials to develop or to run the
default test suite — only the live tier touches a real bucket (see below).

## Setup

```bash
uv sync
```

This creates a virtualenv and installs the project plus the dev tools (pytest,
ruff, mypy).

## Running tests

The suite has two tiers. The default `pytest` run is **credential-free**: it is
the in-process **unit** tests plus a **stdio end-to-end** tier that spawns the
server over the real MCP transport with the GCS leaf swapped for an in-memory
fake, so it needs no bucket and no credentials.

```bash
# Unit + stdio-E2E tier — run by default in CI, no credentials required
uv run pytest
```

The **live** tests are tagged `@pytest.mark.live` and deselected by default (the
project sets `addopts = "-m 'not live'"`). They drive a real GCS bucket through
the transport — publish → get → delete, plus an anonymous-GET privacy check — and
create and clean up their own objects. To run them you need a bucket and the env
vars described in [`.env.example`](.env.example) (the suite reads them from a
`.env` file at the repo root):

```bash
# Live integration tests — require a real GCS bucket + .env (see .env.example)
uv run pytest -m live
```

The grant/revoke ACL tests additionally need real grantee accounts in
`GCS_TEST_GRANTEE` / `GCS_TEST_GRANTEES` (GCS rejects unknown principals) and skip
cleanly when those are unset.

## Lint, format, and type checks

```bash
uv run ruff check            # lint
uv run ruff format --check   # verify formatting (drop --check to apply it)
uv run mypy src              # type check
```

`ruff check` and `ruff format` are configured in `pyproject.toml` (line length is
owned by the formatter, so `E501` is intentionally ignored); please run both
before opening a PR.

## pre-commit

A [pre-commit](https://pre-commit.com/) config (`.pre-commit-config.yaml`) mirrors
the CI lint/format checks so you catch issues before pushing. Install the git hook
once:

```bash
uv run --with pre-commit pre-commit install
```

It then runs ruff (lint + format) and basic file-hygiene checks on every commit.
To run it across the whole tree manually:

```bash
uv run --with pre-commit pre-commit run --all-files
```

mypy is **not** a pre-commit hook (it is slow/noisy under pre-commit's isolated
envs); it runs in CI and you can run it locally with `uv run mypy src`.

## Changelog

Add a bullet under the `## [Unreleased]` section of
[`CHANGELOG.md`](CHANGELOG.md) describing any user-facing change in your PR.

## Pull request expectations

- CI must be green. The `CI` workflow runs `ruff check`, `ruff format --check`,
  `mypy src`, and the credential-free test tier across Python 3.11, 3.12, and 3.13.
- CI does **not** have GCS credentials, so it runs the credential-free tier only.
  If your change touches live behaviour — the GCS client, ACL handling, the
  lifespan, or anything credential-related — run `uv run pytest -m live` against a
  bucket locally and mention the result in your PR.
- Keep changes focused and add tests where it makes sense — prefer credential-free
  unit / stdio-E2E tests so coverage runs everywhere. When you add or change a
  tool, extend the stdio-E2E tier so the tool is exercised through the transport.

See [`CLAUDE.md`](CLAUDE.md) for a deeper tour of the architecture and the live
validation workflow.
