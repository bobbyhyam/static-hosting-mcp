# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-26

### Added

- Initial release: a stdio MCP server that publishes a self-contained artifact to
  a Google Cloud Storage bucket and hands back a permanent, access-controlled URL.
- Six tools: `publish_artifact`, `grant_access`, `revoke_access`,
  `list_artifacts`, `get_artifact`, and `delete_artifact`. Each returns a curated
  `dict`; failures come back as a structured `{"isError": true, "error": ...}`
  payload rather than crashing the session.
- Per-object ACL access model — permanent authenticated URLs with read access
  granted per artifact by Google-account email (fine-grained access / UBLA off).
- `publish_artifact` `source_path` uploads are default-denied: reading a local
  file requires opting in with `ARTIFACT_SOURCE_ROOT`, and credential/secret
  shapes are always refused (see [`SECURITY.md`](SECURITY.md)).
- Packaging and contributor on-ramp: PyPI-ready project metadata, a CI workflow
  (`ruff`, `mypy`, and the credential-free test tier on Python 3.11–3.13),
  `CONTRIBUTING.md`, `SECURITY.md`, and this changelog.

[Unreleased]: https://github.com/bobbyhyam/static-hosting-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bobbyhyam/static-hosting-mcp/releases/tag/v0.1.0
