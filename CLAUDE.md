# CLAUDE.md — working in static-hosting-mcp

Guidance for any Claude Code session working in this repo. For what the server
*is* and how an operator deploys it, see [`README.md`](README.md); for the
contributor workflow — setup, the test tiers, lint/type checks, and PR
expectations — see [`CONTRIBUTING.md`](CONTRIBUTING.md); for the test code, see
[`tests/`](tests).

This is a stdio MCP server with six tools (`publish_artifact`, `grant_access`,
`revoke_access`, `list_artifacts`, `get_artifact`, `delete_artifact`). Because the
tools touch a real GCS bucket and share artifacts with external Google accounts,
**every change to tool behavior must be validated against the live server, not
just in unit tests, before it is merged.** This file documents how.

## Testing framework — the standard practice

When you change anything that affects MCP tool behavior (a tool signature, a
returned shape, an error path, the lifespan, the GCS client), validate in this
order before merging:

1. **Run the automated suites** (fast, credential-free by default):
   ```bash
   uv run pytest            # unit tier + stdio E2E tier (no credentials needed)
   uv run ruff check        # lint (the project enforces this)
   uv run mypy src          # type-check
   ```
2. **Drive the live server interactively** through the **tmux loop** below — the
   manual, exploratory form of the check. Use it while iterating to see real tool
   output without reloading your own session.
3. **Live integration tier** — runs against the **dedicated dev bucket** in `.env`
   (publish → get → delete through the real transport, plus an anonymous-GET
   privacy check; it creates and cleans up its own objects):
   ```bash
   uv run pytest -m live    # skips cleanly if .env is not populated
   ```
   The grant/revoke ACL tests additionally need real grantee accounts in
   `GCS_TEST_GRANTEE` / `GCS_TEST_GRANTEES` (GCS rejects unknown principals) and
   skip when those are unset.

The automated **stdio E2E tier** ([`tests/test_server_stdio.py`](tests/test_server_stdio.py))
is the durable, CI-able form of the tmux loop: it spawns the server over stdio
exactly the way `.mcp.json` does and exercises all six tools through the real MCP
transport, but with the GCS leaf swapped for an in-memory fake
([`tests/stdio_fake_server.py`](tests/stdio_fake_server.py),
[`tests/fakes.py`](tests/fakes.py)) so it needs no bucket and no credentials.
Add to it whenever you add or change a tool.

## The `.mcp.json` dev server

> **The dev environment targets a dedicated dev bucket — its data is disposable.**
> The `.env` in this checkout points `GCS_BUCKET` at a bucket that exists solely
> for development and testing; it holds **no production data**. Publish, grant,
> revoke, get, list, and delete against it freely — both the live test tier and
> the tmux loop create and delete objects there as they run, and nothing in it is
> precious. (Never point `.env` at a production bucket.)

A **gitignored** `.mcp.json` at the repo root registers this server with any
Claude Code session opened in this directory, so the session can call the six
tools directly. It is gitignored (see `.gitignore`) because it carries an
absolute path to *this* checkout; never commit it, `.env`, or anything under
`secrets/`.

It registers one stdio server, `static-hosting-dev`, launched as
`uv run --env-file .env --directory <repo> static-hosting-mcp` — i.e. the real
server, reading the real bucket + credentials from `.env`. Recreate it with this
template (use **absolute** paths — a stdio server is launched from an
unpredictable working directory):

```json
{
  "mcpServers": {
    "static-hosting-dev": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--env-file",
        "/absolute/path/to/static-hosting-mcp/.env",
        "--directory",
        "/absolute/path/to/static-hosting-mcp",
        "static-hosting-mcp"
      ],
      "env": {}
    }
  }
}
```

After writing `.mcp.json`, a **newly launched** Claude Code session in this repo
picks up the `static-hosting-dev` tools (an already-running session must be
restarted to load it — which is exactly why the tmux loop below spawns a *second*
instance instead of reloading yours).

## The tmux live-test loop

To validate live tool behavior **without reloading your own session** (which would
discard your context), launch a *second* Claude Code instance inside a detached
`tmux` session, drive it with `tmux send-keys`, and read its output with
`tmux capture-pane -p`. That second instance loads `.mcp.json` and therefore has
the `static-hosting-dev` tools wired to the live bucket. `tmux` 3.6 is at
`/usr/bin/tmux`.

```bash
# 1. Create a detached tmux session (wide pane so captured output is not wrapped).
tmux new-session -d -s mcptest -x 220 -y 50

# 2. Launch a second Claude Code instance in this repo. --dangerously-skip-permissions
#    lets it run the MCP tool calls without an interactive approval prompt you cannot
#    see/answer from outside; this disposable instance is the only thing it affects.
tmux send-keys -t mcptest \
  'cd /absolute/path/to/static-hosting-mcp && claude --dangerously-skip-permissions' Enter
sleep 6   # give it a moment to boot and connect to the MCP server

# 3. Drive it: send a prompt that exercises the tool you changed.
tmux send-keys -t mcptest \
  'Use the static-hosting-dev MCP server: publish_artifact with title "tmux smoke" and content "<h1>hi</h1>", then print the returned url and key verbatim.' Enter

# 4. Read the result (poll until the tool call has completed).
sleep 20
tmux capture-pane -t mcptest -p | tail -40
#   Use `-S -` to include scrollback if the answer scrolled off:
#   tmux capture-pane -t mcptest -p -S - | tail -80

# 5. Iterate — send more prompts (grant_access, get_artifact, delete_artifact, ...)
#    and re-capture. Tear the session down when finished.
tmux kill-session -t mcptest
```

Tips:
- Send **one focused instruction per turn** and capture after each; the second
  instance is a full agent, so be explicit about which tool and arguments to use.
- It hits the **dedicated dev bucket** (via `.env`), which is safe to mutate:
  `publish_artifact`, `grant_access`/`revoke_access`, and `delete_artifact` freely
  — nothing there is production data. Clean up test objects when convenient to
  avoid clutter, but you don't need to treat them as precious.
- If `capture-pane` shows the instance still working, `sleep` and capture again
  rather than sending the next prompt.

## When to use which

- **Editing tool logic / shapes / errors** → unit tests + the stdio E2E tier
  (`uv run pytest`), then the tmux loop to eyeball real output.
- **Touching the GCS client or anything ACL/credential-related** → also run
  `uv run pytest -m live` against the dev bucket (or the tmux loop, which uses it).
- **Adding a new tool** → add an in-process unit test
  ([`tests/test_tools_unit.py`](tests/test_tools_unit.py)) *and* extend the stdio
  E2E tier so the tool is exercised through the transport.
