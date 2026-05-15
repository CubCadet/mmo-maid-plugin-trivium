"""Cross-file invariants. Catches mistakes that the rest of the suite
doesn't — like "forgot to bump both manifest.json and __version__"."""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main


_REPO = Path(__file__).resolve().parent.parent


def test_version_constant_matches_manifest():
    """manifest.json#version and plugin_main.__version__ must stay aligned.

    A drift here ships a plugin whose on_ready log says one version but
    whose manifest declares another — confusing for support and breaks the
    release.yml tag-vs-manifest parity check at the release-build step.
    """
    manifest = json.loads((_REPO / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == plugin_main.__version__, (
        f"version drift: manifest.json says {manifest['version']!r}, "
        f"__version__ says {plugin_main.__version__!r}. "
        "Bump both before release."
    )


def test_changelog_has_entry_for_current_version():
    """The CHANGELOG must have a section header for the current version.
    A new release without a changelog entry is a smell."""
    changelog = (_REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    expected_header = f"## [{plugin_main.__version__}]"
    assert expected_header in changelog, (
        f"CHANGELOG.md missing section {expected_header!r}. "
        "Add the section before tagging the release."
    )
