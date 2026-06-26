"""Unit tests for object-key generation and content-type inference (U4).

Pure, credential-free, network-free: every function under test is a deterministic
transform once ``today`` is injected.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from static_hosting_mcp.formatters import (
    DEFAULT_INLINE_CONTENT_TYPE,
    SLUG_FALLBACK,
    SLUG_MAX_LENGTH,
    SUFFIX_LENGTH,
    UNKNOWN_CONTENT_TYPE,
    extension_for,
    generate_key,
    infer_content_type,
    slugify,
)

# Key shape: YYYY/MM/DD/<slug>-<6 lowercase alnum>.<ext>
_KEY_RE = re.compile(r"^\d{4}/\d{2}/\d{2}/[a-z0-9-]+-[a-z0-9]{6}\.[a-z0-9]+$")


# --- slugify ---------------------------------------------------------------


def test_slug_spaces_and_punctuation_become_single_hyphens():
    assert slugify("Hello, World! Foo / Bar") == "hello-world-foo-bar"


def test_slug_ascii_folds_unicode():
    assert slugify("Café Münchën Notes") == "cafe-munchen-notes"


def test_slug_collapses_runs_and_trims_edges():
    assert slugify("  ***Q2___tariff***  ") == "q2-tariff"


def test_slug_overlong_title_is_truncated_without_trailing_hyphen():
    slug = slugify("word " * 40)  # far longer than the cap
    assert len(slug) <= SLUG_MAX_LENGTH
    assert not slug.startswith("-")
    assert not slug.endswith("-")


@pytest.mark.parametrize("title", ["", "   ", "!!!", "@#$%^&*()", "—"])
def test_slug_empty_or_symbol_only_falls_back(title):
    assert slugify(title) == SLUG_FALLBACK


# --- generate_key ----------------------------------------------------------


def test_generate_key_matches_acceptance_example_ae1():
    # AE1: inline HTML + title -> 2026/06/24/q2-tariff-deep-research-<suffix>.html
    key = generate_key("Q2 tariff deep research", "text/html", date(2026, 6, 24))
    assert re.fullmatch(r"2026/06/24/q2-tariff-deep-research-[a-z0-9]{6}\.html", key), key


def test_generate_key_overall_shape():
    assert _KEY_RE.match(generate_key("Some Title", "application/json", date(2026, 1, 9)))


def test_generate_key_uses_injected_date_for_the_prefix():
    assert generate_key("x", "text/html", date(2030, 12, 5)).startswith("2030/12/05/")


def test_generate_key_suffix_differs_across_calls():
    # Collision avoidance: identical inputs still yield distinct keys.
    today = date(2026, 6, 24)
    keys = {generate_key("same title", "text/html", today) for _ in range(20)}
    assert len(keys) == 20


def test_generate_key_suffix_length_and_alphabet():
    key = generate_key("Title", "text/html", date(2026, 6, 24))
    suffix = key.rsplit("-", 1)[1].split(".", 1)[0]
    assert len(suffix) == SUFFIX_LENGTH
    assert re.fullmatch(r"[a-z0-9]+", suffix)


def test_generate_key_unknown_content_type_uses_bin_extension():
    key = generate_key("data", "application/x-not-real", date(2026, 6, 24))
    assert key.endswith(".bin")


def test_generate_key_empty_slug_title_uses_fallback():
    key = generate_key("***", "text/html", date(2026, 6, 24))
    assert key == f"2026/06/24/{SLUG_FALLBACK}-{key.rsplit('-', 1)[1]}"
    assert key.startswith(f"2026/06/24/{SLUG_FALLBACK}-")


# --- extension_for ---------------------------------------------------------


@pytest.mark.parametrize(
    "content_type,expected_ext",
    [
        ("text/html", "html"),
        ("text/markdown", "md"),
        ("application/json", "json"),
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("application/pdf", "pdf"),
    ],
)
def test_extension_for_known_types(content_type, expected_ext):
    assert extension_for(content_type) == expected_ext


def test_extension_for_strips_parameters_and_case():
    assert extension_for("text/HTML; charset=utf-8") == "html"


def test_extension_for_unknown_type_is_bin():
    assert extension_for("application/x-unknown") == "bin"


# --- infer_content_type ----------------------------------------------------


def test_infer_inline_default_is_html():
    # No source path and a title without an extension -> inline default.
    assert infer_content_type(title="Q2 tariff deep research") == DEFAULT_INLINE_CONTENT_TYPE


@pytest.mark.parametrize(
    "source_path,expected",
    [
        ("notes.md", "text/markdown"),
        ("data.json", "application/json"),
        ("logo.png", "image/png"),
        ("photo.JPEG", "image/jpeg"),
        ("page.html", "text/html"),
    ],
)
def test_infer_from_source_extension(source_path, expected):
    assert infer_content_type(source_path=source_path) == expected


def test_infer_unknown_extension_falls_back_to_octet_stream():
    assert infer_content_type(source_path="archive.xyz") == UNKNOWN_CONTENT_TYPE


def test_infer_explicit_content_type_overrides_extension():
    assert infer_content_type(content_type="image/png", source_path="notes.md") == "image/png"


def test_infer_uses_title_extension_when_no_source_path():
    assert infer_content_type(title="weekly-notes.md") == "text/markdown"


def test_infer_source_path_takes_precedence_over_title():
    assert infer_content_type(source_path="data.json", title="report.md") == "application/json"


def test_infer_dotted_title_without_real_extension_is_inline_default():
    # The trailing token after the last dot is not a short extension.
    assert infer_content_type(title="v1.2 release plan") == DEFAULT_INLINE_CONTENT_TYPE


@pytest.mark.parametrize(
    "title",
    ["Roadmap v1.0", "Q2 report v2.1", "Budget 2026.06"],
)
def test_infer_inline_title_ending_in_version_token_is_inline_default(title):
    # RF3: a human title ending in a version/date-like dotted token ("Roadmap
    # v1.0" -> "0") parses as a trailing extension but is NOT a recognized type;
    # it must fall through to the inline HTML default, not octet-stream (which
    # would yield a .bin key and download instead of render the artifact).
    assert infer_content_type(content_type=None, source_path=None, title=title) == (
        DEFAULT_INLINE_CONTENT_TYPE
    )


def test_infer_source_path_unknown_extension_still_octet_stream_for_real_files():
    # RF3 must not regress source-path inference: a real file with an unrecognized
    # extension is genuinely opaque binary even if a version-like title is present.
    assert (
        infer_content_type(source_path="archive.xyz", title="Roadmap v1.0") == UNKNOWN_CONTENT_TYPE
    )
