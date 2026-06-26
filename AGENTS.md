# AGENTS.md

This project keeps a single source of truth for AI-agent and contributor guidance
in **[CLAUDE.md](CLAUDE.md)** — architecture, testing framework, the `.mcp.json`
dev server, and the live-validation workflow all live there. For the contributor
workflow (setup, test tiers, lint/type checks, and PR expectations), see
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

If your agent tooling reads `AGENTS.md`, treat `CLAUDE.md` as the canonical
instructions and follow it.

(We use a thin pointer here instead of a symlink so the file works cleanly on
Windows checkouts and across tools that don't resolve symlinks.)
