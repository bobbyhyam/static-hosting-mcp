"""Unit tests for curated response shapes and structured errors (U4).

Each shaper is asserted to carry *exactly* its documented keys, and the error
helpers are checked for the application-level ``isError`` marker and actionable
message text. A final test enforces the U4 contract that ``formatters`` is a
pure, import-only leaf with no edge to ``gcs_client`` or any network library.
"""

from __future__ import annotations

import ast
from pathlib import Path

from static_hosting_mcp import formatters
from static_hosting_mcp.formatters import (
    artifact_detail,
    artifact_summary,
    error,
    grant_results,
    list_result,
    not_found_message,
    publish_result,
    ubla_disabled_message,
)

# --- publish_result --------------------------------------------------------


def test_publish_result_has_exactly_the_documented_keys():
    result = publish_result(
        key="2026/06/24/x-abc123.html",
        url="https://storage.cloud.google.com/bkt/2026/06/24/x-abc123.html",
        content_type="text/html",
        size=42,
        grants=[{"email": "a@example.com", "ok": True}],
    )
    assert set(result) == {"key", "url", "content_type", "size", "grants"}
    assert result["grants"] == [{"email": "a@example.com", "ok": True}]


def test_publish_result_defaults_grants_to_empty_list():
    result = publish_result(key="k", url="u", content_type="text/html", size=0)
    assert result["grants"] == []


def test_publish_result_omits_warning_key_when_none():
    # RF5: the warning key is additive — a clean publish has no `warning` and stays
    # the exact documented shape.
    result = publish_result(key="k", url="u", content_type="text/html", size=0)
    assert "warning" not in result


def test_publish_result_carries_warning_and_key_url_on_partial_success():
    # RF5: a post-upload grant failure is a success-with-warning that STILL carries
    # the recoverable key/url (not a key-less error), plus the per-email failure.
    result = publish_result(
        key="2026/06/24/x-abc123.html",
        url="https://storage.cloud.google.com/bkt/2026/06/24/x-abc123.html",
        content_type="text/html",
        size=42,
        grants=[{"email": "a@example.com", "ok": False, "error": "grant failed"}],
        warning="published but grant failed; retry with grant_access",
    )
    assert "isError" not in result  # the object exists; this is not an error result
    assert result["key"] == "2026/06/24/x-abc123.html"
    assert result["url"].endswith(result["key"])
    assert result["warning"]
    assert result["grants"][0]["ok"] is False


# --- artifact_summary ------------------------------------------------------


def test_artifact_summary_reports_grantee_count_not_the_acl():
    summary = artifact_summary(
        key="2026/06/24/x-abc123.html",
        url="https://storage.cloud.google.com/bkt/2026/06/24/x-abc123.html",
        created="2026-06-24T10:00:00Z",
        size=128,
        grantee_count=3,
    )
    assert set(summary) == {"key", "url", "created", "size", "grantee_count"}
    assert summary["grantee_count"] == 3
    # The full ACL must never leak into a listing summary.
    assert "grantees" not in summary


# --- artifact_detail -------------------------------------------------------


def test_artifact_detail_has_exactly_the_documented_keys():
    detail = artifact_detail(
        url="https://storage.cloud.google.com/bkt/2026/06/24/x-abc123.html",
        content_type="text/html",
        size=128,
        created="2026-06-24T10:00:00Z",
        grantees=["a@example.com", "b@example.com"],
    )
    assert set(detail) == {"url", "content_type", "size", "created", "grantees"}
    assert detail["grantees"] == ["a@example.com", "b@example.com"]


def test_artifact_detail_copies_the_grantees_list():
    source = ["a@example.com"]
    detail = artifact_detail(
        url="u", content_type="text/html", size=1, created=None, grantees=source
    )
    source.append("b@example.com")
    assert detail["grantees"] == ["a@example.com"]  # not aliased to the caller's list


# --- grant_results ---------------------------------------------------------


def test_grant_results_shapes_ok_and_error_entries():
    shaped = grant_results(
        [
            ("alice@example.com", True, None),
            ("bob@example.com", False, "unknown principal"),
        ]
    )
    assert shaped == [
        {"email": "alice@example.com", "ok": True},
        {"email": "bob@example.com", "ok": False, "error": "unknown principal"},
    ]


def test_grant_results_ok_entry_has_no_error_key():
    [entry] = grant_results([("alice@example.com", True, None)])
    assert "error" not in entry


def test_grant_results_empty_input_is_empty_list():
    assert grant_results([]) == []


# --- list_result -----------------------------------------------------------


def test_list_result_envelope_keys_and_total_equals_page_length():
    items = [{"key": "a"}, {"key": "b"}, {"key": "c"}]
    envelope = list_result(items, truncated=True, hint="Narrow the date prefix.")
    assert set(envelope) == {"items", "total", "truncated", "hint"}
    assert envelope["items"] is items
    assert envelope["total"] == 3  # len(items), not a full-bucket count
    assert envelope["truncated"] is True
    assert envelope["hint"] == "Narrow the date prefix."


def test_list_result_total_tracks_items_even_when_not_truncated():
    envelope = list_result([{"key": "only"}], truncated=False, hint="")
    assert envelope["total"] == 1
    assert envelope["truncated"] is False


# --- error + message templates ---------------------------------------------


def test_error_carries_application_marker_and_hint():
    result = error("Both content and source_path were given.", hint="Pass exactly one.")
    assert result == {
        "isError": True,
        "error": "Both content and source_path were given.",
        "hint": "Pass exactly one.",
    }


def test_error_without_hint_omits_the_hint_key():
    result = error("Something failed.")
    assert result == {"isError": True, "error": "Something failed."}
    assert "hint" not in result


def test_ubla_message_names_bucket_and_disable_command():
    message = ubla_disabled_message("my-artifacts-bucket")
    assert "my-artifacts-bucket" in message
    assert "uniform-bucket-level-access" in message
    assert "gs://my-artifacts-bucket" in message


def test_not_found_message_names_reference_and_next_step():
    reference = "2026/06/24/missing-abc123.html"
    message = not_found_message(reference)
    assert reference in message
    assert "list_artifacts" in message


# --- purity gate (U4 contract: pure, network-free leaf) --------------------


def test_formatters_is_an_import_pure_leaf():
    """``formatters`` must import only the standard library and nothing from
    ``gcs_client`` -- the property that keeps it a network-free leaf (U4)."""
    source = Path(formatters.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Reject any relative import (level > 0), e.g. `from .gcs_client import ...`.
            assert node.level == 0, "formatters must not use intra-package imports"
            if node.module:
                imported_roots.add(node.module.split(".")[0])

    allowed_stdlib = {"__future__", "re", "secrets", "unicodedata", "collections", "datetime"}
    assert imported_roots <= allowed_stdlib, (
        f"formatters imports outside the allowed stdlib set: {imported_roots - allowed_stdlib}"
    )
    # The leaf must not import the network client (only ever mention it in prose).
    assert "gcs_client" not in imported_roots
