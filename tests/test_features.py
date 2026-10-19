import argparse
import re
from pathlib import Path

import pytest

from mnemoloci import cli, features

NAV = Path(__file__).resolve().parent.parent / "app" / "index.html"


def _commands() -> set[str]:
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(action.choices)


def test_every_command_belongs_to_a_feature_or_is_listed_as_internal():
    claimed = {c for f in features.catalogue()["features"] for c in f["commands"]}
    unknown = _commands() - claimed - set(features.INTERNAL)
    assert not unknown, f"add these commands to mnemoloci/features.py: {sorted(unknown)}"


def test_the_catalogue_names_no_command_that_does_not_exist():
    claimed = {c for f in features.catalogue()["features"] for c in f["commands"]}
    assert not (claimed | set(features.INTERNAL)) - _commands()


def test_a_command_is_claimed_by_one_feature_only():
    seen: dict[str, str] = {}
    for f in features.catalogue()["features"]:
        for c in f["commands"]:
            assert c not in seen, f"{c} is in both {seen[c]} and {f['id']}"
            seen[c] = f["id"]


def test_each_feature_is_well_formed():
    cat = features.catalogue()
    ids = [f["id"] for f in cat["features"]]
    assert len(ids) == len(set(ids))
    categories = {c["id"] for c in cat["categories"]}
    for f in cat["features"]:
        assert f["category"] in categories and f["level"] in features.LEVELS
        assert f["title"].strip() and len(f["description"]) > 20 and f["commands"]


@pytest.mark.skipif(not NAV.is_file(), reason="web app sources not present")
def test_a_feature_points_at_a_view_that_exists_and_only_when_the_app_has_it():
    views = set(re.findall(r'data-view="([a-z]+)"', NAV.read_text("utf-8")))
    for f in features.catalogue()["features"]:
        if f["level"] in ("full", "partial"):
            assert f["view"] in views, f"{f['id']} claims the view {f['view']!r}, which is not in the menu"
        else:
            assert f["view"] is None, f"{f['id']} is {f['level']} but names a view"


def test_the_summary_counts_what_the_page_will_say():
    s = features.catalogue()["summary"]
    assert s["total"] == s["full"] + s["partial"] + s["none"] > 30


I18N = Path(__file__).resolve().parent.parent / "app" / "src" / "i18n-it.ts"


@pytest.mark.skipif(not I18N.is_file(), reason="web app sources not present")
def test_every_catalogue_text_has_an_italian_translation():
    import json

    known = I18N.read_text("utf-8")
    cat = features.catalogue()
    texts = [c["title"] for c in cat["categories"]] + [x for f in cat["features"] for x in (f["title"], f["description"])]
    missing = [t for t in texts if json.dumps(t, ensure_ascii=False) not in known]
    assert not missing, f"add these to app/src/i18n-it.ts: {missing}"
