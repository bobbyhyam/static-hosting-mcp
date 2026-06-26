# Security Policy

`static-hosting-mcp` loads a Google Cloud service-account credential and grants
external Google accounts read access to published objects, so we take reports
seriously. This document explains how to report a vulnerability and the security
posture you should understand when running the server.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for a security vulnerability.**

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/bobbyhyam/static-hosting-mcp/security/advisories/new)
("Report a vulnerability" under the repository's **Security** tab). If that is
unavailable, email the maintainer at **bobby@bobbyhyam.com**.

Please include:

- a description of the issue and its impact,
- the version or commit affected,
- steps to reproduce (a minimal proof of concept helps), and
- any suggested remediation.

We aim to acknowledge a report within a few business days and to agree on a
disclosure timeline once the issue is confirmed. Please give us a reasonable
window to ship a fix before any public disclosure.

## Supported Versions

This project is pre-1.0; security fixes target the latest released `0.x` version.
Upgrade to the most recent release before reporting, and pin to a released
version rather than an arbitrary commit.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Security Model and Threat Surface

Understanding these boundaries helps you deploy the server safely and helps us
triage reports.

- **Service-account key.** The server reads its GCS credentials from the
  environment (`GOOGLE_APPLICATION_CREDENTIALS`, or keyless Application Default
  Credentials) and **never exposes them to the LLM** — the configured key path is
  marked `repr=False` so it cannot leak through a stray log or error. Keep the key
  file outside version control: the recommended location, `secrets/`, is
  gitignored, and `.env` must never be committed. Where your organization allows
  it, prefer keyless ADC (`gcloud auth application-default login` or
  impersonation) over a downloaded key.

- **External read grants.** Publishing shares an artifact by adding a recipient's
  email as a per-object ACL *reader*, which grants a real external Google account
  read access to that object's permanent URL. Treat a published URL as shared with
  every grantee; remove access with `revoke_access` and delete the object with
  `delete_artifact` when it should no longer resolve. The server never makes an
  object public (no `allUsers` / `allAuthenticatedUsers` grants).

- **`source_path` is default-denied.** Because an uploaded artifact can be shared
  with external accounts, `publish_artifact` reading a local file would otherwise
  be a read-any-file → publish → share-to-attacker channel whose highest-value
  target is the very service-account key the server keeps hidden. So `source_path`
  is **disabled unless** the operator sets `ARTIFACT_SOURCE_ROOT` to an absolute
  directory, and even then only files that resolve **inside** that directory are
  read. Credential and secret shapes are **always** refused — the service-account
  key, anything under a `secrets/` directory, `~/.ssh`, `~/.config/gcloud`,
  `*.pem` / `*.key` files, and symlinks that escape the configured root — with no
  upload. Inline `content` is unaffected. Keep the key file (and any other secret)
  outside `ARTIFACT_SOURCE_ROOT`.

For the deployment-side details of these controls, see [`README.md`](README.md).
