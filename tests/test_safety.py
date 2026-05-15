"""Tests for the safety layer: html unescape (OTDB only) + display scrub."""
from __future__ import annotations

import pytest

from plugin_main import otdb_unescape, scrub_for_display


# ── html unescape: regression corpus ────────────────────────────────────────
# Real OTDB encodings seen in production trivia data. The bug we're guarding
# against: rendering "&quot;" / "&#039;" / "&amp;" / "&eacute;" verbatim
# instead of decoding them. This is the #1 naive-implementation bug.
OTDB_CORPUS = [
    ("&quot;Hello&quot;", '"Hello"'),
    ("Don&#039;t panic", "Don't panic"),
    ("Tom &amp; Jerry", "Tom & Jerry"),
    ("caf&eacute;", "café"),
    ("&Aacute;rmin", "Ármin"),
    ("&pi;&radic;2", "π√2"),
    ("&lt;br&gt;tag&lt;/br&gt;", "<br>tag</br>"),
    ("&#34;mixed&#34; &amp; &quot;quotes&quot;", '"mixed" & "quotes"'),
    ("", ""),
]


@pytest.mark.parametrize("encoded,decoded", OTDB_CORPUS)
def test_otdb_unescape_decodes_known_encodings(encoded, decoded):
    assert otdb_unescape(encoded) == decoded


def test_otdb_unescape_idempotent_on_plain_unicode():
    # Already-decoded strings must pass through unchanged.
    assert otdb_unescape("café") == "café"
    assert otdb_unescape("Plain English") == "Plain English"
    assert otdb_unescape("π√2") == "π√2"


def test_otdb_unescape_none_returns_empty():
    assert otdb_unescape("") == ""


# ── scrub_for_display ───────────────────────────────────────────────────────

def test_scrub_neutralizes_at_everyone():
    out = scrub_for_display("Ping @everyone now")
    assert "@everyone" not in out
    # Zero-width space is between @ and e
    assert "@​everyone" in out


def test_scrub_neutralizes_at_here():
    out = scrub_for_display("Hey @here, look")
    assert "@here" not in out
    assert "@​here" in out


def test_scrub_escapes_backticks():
    out = scrub_for_display("Use `code` here")
    # Backticks get a zero-width-space prefix so they don't open code blocks
    assert "`code`" not in out
    assert "​`" in out


def test_scrub_neutralizes_masked_link_http():
    out = scrub_for_display("Click [here](https://evil.example.com) now")
    # The closing paren of [text]( must have a zero-width space before http
    assert "](http" not in out
    assert "](​http" in out


def test_scrub_neutralizes_masked_link_www():
    out = scrub_for_display("[link](www.evil.example.com)")
    assert "](www" not in out
    assert "](​www" in out


def test_scrub_strips_bidi_controls():
    # RTL override (U+202E) used to flip visible text order
    spoofed = "Answer: A‮noitseuQ"
    out = scrub_for_display(spoofed)
    assert "‮" not in out


def test_scrub_strips_all_bidi_codepoints():
    bad_codepoints = ["‪", "‫", "‬", "‭", "‮",
                      "⁦", "⁧", "⁨", "⁩"]
    for cp in bad_codepoints:
        out = scrub_for_display(f"safe{cp}content")
        assert cp not in out, f"bidi codepoint {cp!r} survived scrub"


def test_scrub_preserves_legitimate_content():
    plain = "Who composed Symphony No. 5? It's Beethoven."
    assert scrub_for_display(plain) == plain


def test_scrub_handles_empty_and_none_safely():
    assert scrub_for_display("") == ""
