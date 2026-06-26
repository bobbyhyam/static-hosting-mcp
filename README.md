# static-hosting-mcp

[![CI](https://github.com/bobbyhyam/static-hosting-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/bobbyhyam/static-hosting-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/static-hosting-mcp)](https://pypi.org/project/static-hosting-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/bobbyhyam/static-hosting-mcp/blob/main/LICENSE)

A Python **stdio MCP server** that publishes a one-off artifact — a research
report, a plan, some rendered HTML — to a Google Cloud Storage bucket and hands
back a **permanent, access-controlled URL**, plus tools to grant and revoke read
access by Google-account email. Bucket and service-account credentials are loaded
from the environment and are never exposed to the LLM.

The returned URL is a **permanent authenticated URL** of the form
`https://storage.cloud.google.com/<bucket>/<object_key>`. It opens for any Google
account you grant read access to (plus anyone with inherited bucket/project read,
including the server's own service account) and is denied to everyone else. It
does not expire.

## Access model

- **Authenticated URLs + per-object ACL grants.** Each artifact gets a permanent
  authenticated URL; access is granted per object by adding the recipient's email
  as an ACL *reader*. This is the one combination that is **permanent _and_
  email-restricted** — unlike public URLs (no access control), signed URLs
  (expire, 7-day cap), or bucket-wide IAM (can't scope a grant to one file).
- **Fine-grained access (UBLA off).** Per-object ACLs require the bucket's Uniform
  Bucket-Level Access to be **disabled**. The server never changes this setting.
  On startup it validates only that the credentials load and the bucket is
  reachable; it does **not** read the UBLA setting then. If UBLA is on, the first
  per-object grant (on publish or `grant_access`) fails and the server returns an
  actionable error naming the bucket and the command to disable it.
- **Single role.** The service account needs exactly one role on the bucket:
  `roles/storage.objectAdmin` (object read/write/delete + per-object ACL
  management). No bucket-metadata (`storage.buckets.get`) permission is required.

## Operator setup

The server reads its configuration from the environment. The **correct place** to
put it is a `.env` file at the project root (already gitignored), with the key file
under `secrets/` (also gitignored). `.env.example` documents the contract.

### Environment contract

| Var | Required | Purpose |
| --- | --- | --- |
| `GCS_BUCKET` | yes | Bucket name only — **no** `gs://` prefix (e.g. `my-artifacts-bucket`). Not a secret; it appears as the bucket segment of returned URLs. |
| `GOOGLE_APPLICATION_CREDENTIALS` | yes | **Absolute** path to the service-account JSON key (recommended: `secrets/gcs-sa-key.json`). |
| `GCS_PROJECT_ID` | optional | Overrides the project id; if unset it is derived from the key/ADC. |
| `ARTIFACT_SOURCE_ROOT` | optional | **Absolute** directory that `publish_artifact`'s `source_path` reads are confined to. **Unset ⇒ `source_path` is disabled** (publish inline `content` only). Files that resolve outside it — and credential/secret shapes (the SA key, `~/.ssh`, `~/.config/gcloud`, `*.pem`/`*.key`, anything under a `secrets/` directory) — are refused with no upload. |
| `ARTIFACT_MAX_BYTES` | optional | Maximum uploaded artifact size in bytes, inline or `source_path` (default `104857600` = 100 MiB). An oversized file is refused from a stat, before it is read into memory. |

> **Why `source_path` is locked down.** A published artifact can be shared with
> external Google accounts, so an unconfined local-file read would be a
> read-any-file → publish → share-to-attacker channel whose highest-value target is
> the service-account key this server otherwise keeps hidden. `source_path` is
> therefore default-denied: opt in per deployment with `ARTIFACT_SOURCE_ROOT`, and
> keep the key file (and any other secret) outside that directory. Inline `content`
> is unaffected.

### 1. Provision the bucket, service account, and key

Run once; substitute `YOUR_PROJECT_ID` / `YOUR_BUCKET_NAME`:

```bash
# 1. Create a bucket with fine-grained access (UBLA OFF) and public access prevented.
#    --no-uniform-bucket-level-access and --public-access-prevention are boolean flags
#    (no value). Per-recipient ACL grants are unaffected by public-access-prevention;
#    it only blocks public (allUsers / allAuthenticatedUsers) grants.
gcloud storage buckets create gs://YOUR_BUCKET_NAME \
  --project=YOUR_PROJECT_ID \
  --location=US \
  --no-uniform-bucket-level-access \
  --public-access-prevention

# If the bucket already exists with UBLA on, disable it instead:
# gcloud storage buckets update gs://YOUR_BUCKET_NAME --no-uniform-bucket-level-access

# 2. Create the server's service account.
gcloud iam service-accounts create static-hosting-mcp \
  --project=YOUR_PROJECT_ID \
  --display-name="static-hosting-mcp server"

# 3. Grant the single required role ON THE BUCKET:
#    objectAdmin = object read/write/delete + per-object ACL management.
SA="static-hosting-mcp@YOUR_PROJECT_ID.iam.gserviceaccount.com"
gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET_NAME \
  --member="serviceAccount:${SA}" --role="roles/storage.objectAdmin"

# 4. Create and download a JSON key into secrets/ (gitignored).
gcloud iam service-accounts keys create secrets/gcs-sa-key.json --iam-account="${SA}"
```

`roles/storage.objectAdmin` on the bucket is the only role required. If a
permission ever proves insufficient against the live API, `roles/storage.admin`
on the bucket is the broad fallback.

### 2. Key-creation org-policy caveat & ADC fallback

If the project is under a Cloud organization that enforces
`constraints/iam.disableServiceAccountKeyCreation` (the default for orgs created
on/after 2024-05-03), **step 4 will fail**. For a consumer/Gmail account with no
organization it works as written. Otherwise, grant a policy exception or switch to
keyless **Application Default Credentials (ADC)**:

```bash
# Keyless ADC — leave GOOGLE_APPLICATION_CREDENTIALS unset and authenticate locally:
gcloud auth application-default login
# (or use service-account impersonation)
```

With ADC the server builds its storage client from the ambient credentials
instead of a key file; set `GCS_PROJECT_ID` if the project can't be inferred.

### 3. Configure the environment

```bash
cp .env.example .env
# then edit .env and fill in GCS_BUCKET and GOOGLE_APPLICATION_CREDENTIALS
```

## Running locally

This is a local `uv` project. From the project root:

```bash
uv sync                      # resolve and install dependencies
uv run static-hosting-mcp    # start the stdio MCP server (reads .env)
```

On startup the server validates that the credentials load and the bucket is
reachable, and aborts with an actionable message if either fails. If the required
environment variables are missing it prints `Missing required environment
variables: ...` to stderr and exits non-zero.

## Claude Desktop / Claude Code config

Add the server to your MCP client config (Claude Desktop:
`claude_desktop_config.json`; Claude Code: `.mcp.json` or `~/.claude.json`). Use
an **absolute** path to this checkout and to the key file:

```json
{
  "mcpServers": {
    "static-hosting": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/static-hosting-mcp",
        "static-hosting-mcp"
      ],
      "env": {
        "GCS_BUCKET": "your-bucket-name",
        "GOOGLE_APPLICATION_CREDENTIALS": "/absolute/path/to/static-hosting-mcp/secrets/gcs-sa-key.json"
      }
    }
  }
}
```

The `env` block here is equivalent to the `.env` file; rely on either. Add
`GCS_PROJECT_ID` only if it can't be derived from the key.

## Tools

Six workflow-oriented tools. Each returns a curated `dict`; failures come back as a
structured error dict (`{"isError": true, "error": ...}`) with a next step — tools
never crash the session.

**Publish & share (writes)**

- `publish_artifact` — Publish one self-contained artifact (inline `content`
  **or** a local `source_path` — exactly one) under a `title`, and optionally
  share it in the same call via `grant_emails`. Returns the permanent URL, object
  key, content-type, size, and per-email grant results. `source_path` requires
  `ARTIFACT_SOURCE_ROOT` to be set and the file to resolve inside it (else it is
  disabled); if the upload succeeds but a grant fails, the result still carries the
  recoverable `key`/`url` plus a `warning` (retry with `grant_access`, don't
  re-publish).
- `grant_access` — Share an artifact that **already exists**: add per-object read
  access for one or more `emails` (by object key **or** full URL). Idempotent;
  returns per-email results; the URL is unchanged.
- `revoke_access` — Remove read access for one or more `emails` from an existing
  artifact. Idempotent and **destructive**; returns per-email results.

**Inspect & manage**

- `list_artifacts` — List published artifacts as curated summaries (key, URL,
  created date, size, grantee count), optionally filtered by a `date_prefix`
  (`YYYY`, `YYYY/MM`, or `YYYY/MM/DD`); default `limit` 50, with a truncation
  signal and a narrow-the-prefix / raise-the-limit hint. **Read-only.** Use this
  to find *many*.
- `get_artifact` — Fetch **one** artifact's metadata (URL, content-type, size,
  created date, current grantee emails) by object key **or** full URL.
  **Read-only.**
- `delete_artifact` — Delete one artifact (by object key **or** full URL);
  afterward its URL stops resolving. **Destructive.** v1 has no overwrite-in-place:
  to revise an artifact, delete and re-publish (this yields a new URL and requires
  re-granting).

### Object reference

`grant_access`, `revoke_access`, `get_artifact`, and `delete_artifact` all accept
the same **object reference** — either the bare object key
(`2026/06/24/q2-tariff-deep-research-7f3a9c.html`) or the full returned URL.

## Manual acceptance check

Restricted read is asserted automatically at the ACL layer and by confirming an
unauthenticated fetch is denied (see the live test suite). The final
**human-in-the-loop** check is manual, because the authenticated URL requires an
interactive Google sign-in:

> A granted recipient opens the `https://storage.cloud.google.com/<bucket>/<key>`
> URL **while signed in to a granted Google account** and sees the artifact; a
> person **without** a grant (or signed into a non-granted account) is denied.

Per-object ACLs cap at ~100 entries, so an artifact can be shared with up to ~100
accounts — ample for one-off sharing.

## Development

```bash
uv run pytest                # fast unit tier (credential-free, faked GCS client)
uv run pytest -m live        # live integration against the configured bucket (skips if .env is incomplete)
uv run ruff check            # lint
uv run mypy src              # type-check
```
