"""
The Mini App keeps its own copy of the logo maps in web/js/ui.js —
TEAM_LOGO_MAP (club → PNG) and TEAM_LOGO_ALIASES (alias → canonical club) —
because it serves /assets/logos/… without touching Pillow. Nothing ties that
copy to the backend at runtime, so a roster or alias change used to drift
silently: the table shows an empty badge, or worse, another club's crest.

These tests parse the two object literals out of ui.js and hold them against
services/graphics/table_generator.TEAM_LOGO_MAP, config.DIVISION_CLUBS and
club_registry.TEAM_ALIASES. Keys are compared after normalize_team_name, which
folds the same characters as normalizeLogoKey in ui.js.
"""

import os
import re

import config
from club_registry import TEAM_ALIASES, normalize_team_name
from services.graphics.table_generator import TEAM_LOGO_MAP

UI_JS = os.path.join(os.path.dirname(__file__), "..", "web", "js", "ui.js")

_PAIR = re.compile(r"'([^'\\]*)'\s*:\s*'([^'\\]*)'")


def _js_object(name: str) -> dict[str, str]:
    """Return the 'key': 'value' pairs of `export const NAME = { … };` in ui.js."""
    with open(UI_JS, encoding="utf-8") as f:
        source = f.read()
    match = re.search(rf"export const {name} = \{{(.*?)\n\}};", source, re.S)
    assert match, f"{name} not found in web/js/ui.js"
    body = re.sub(r"//[^\n]*", "", match.group(1))
    pairs = _PAIR.findall(body)
    keys = [k for k, _ in pairs]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicates, f"{name}: duplicate keys {duplicates}"
    # Every entry must be a plain 'key': 'value' pair, or the parser skipped one.
    assert len(pairs) == body.count(":"), f"{name}: an entry is not a quoted pair"
    return dict(pairs)


JS_LOGO_MAP = _js_object("TEAM_LOGO_MAP")
JS_ALIASES = _js_object("TEAM_LOGO_ALIASES")

ROSTER = [club for clubs in config.DIVISION_CLUBS.values() for club in clubs]
CANON = {normalize_team_name(club): club for club in ROSTER}


def _norm_map(mapping: dict[str, str]) -> dict[str, str]:
    return {normalize_team_name(k): v for k, v in mapping.items()}


def test_js_logo_map_matches_the_backend_map():
    """Same clubs, same filenames — a renamed PNG must be renamed in both."""
    js = _norm_map(JS_LOGO_MAP)
    py = _norm_map(TEAM_LOGO_MAP)
    assert len(js) == len(JS_LOGO_MAP), "ui.js: two keys collapse into one after normalization"
    only_js = sorted(set(js) - set(py))
    only_py = sorted(set(py) - set(js))
    assert not only_js, f"In ui.js but not in table_generator.py: {only_js}"
    assert not only_py, f"In table_generator.py but not in ui.js: {only_py}"
    wrong = {k: (js[k], py[k]) for k in js if js[k] != py[k]}
    assert not wrong, f"Different filenames (ui.js, python): {wrong}"


def test_js_logo_map_covers_the_roster():
    js = _norm_map(JS_LOGO_MAP)
    missing = [club for club in ROSTER if normalize_team_name(club) not in js]
    assert not missing, f"Clubs without a logo in ui.js: {missing}"


def test_js_aliases_mirror_club_registry():
    """Key for key, to the same club. Aliases equal to a canonical name are
    skipped on both sides: the EXACT tier already knows them, and ui.js drops
    them from ALIAS_INDEX anyway. A python alias may also be missing from ui.js
    when it is the latin form ui.js derives from the club's filename
    ('los angeles' ← los_angeles.png)."""
    js = {
        normalize_team_name(k): v
        for k, v in JS_ALIASES.items()
        if normalize_team_name(k) not in CANON
    }
    py = {k: v for k, v in _norm_map(TEAM_ALIASES).items() if k not in CANON}
    py_files = _norm_map(TEAM_LOGO_MAP)
    from_filenames = {
        normalize_team_name(file.removesuffix(".png").replace("_", " ")): file
        for file in set(TEAM_LOGO_MAP.values())
    }
    only_js = sorted(set(js) - set(py))
    only_py = sorted(
        k for k in set(py) - set(js)
        if from_filenames.get(k) != py_files.get(normalize_team_name(py[k]))
    )
    assert not only_js, f"Aliases in ui.js but not in club_registry.py: {only_js}"
    assert not only_py, f"Aliases in club_registry.py but not in ui.js: {only_py}"
    wrong = {k: (js[k], py[k]) for k in js if js[k] != py[k]}
    assert not wrong, f"Alias points to a different club (ui.js, python): {wrong}"


def test_every_js_alias_reaches_a_logo():
    """ui.js silently skips an alias whose target is not in TEAM_LOGO_MAP."""
    js = _norm_map(JS_LOGO_MAP)
    dangling = {a: c for a, c in JS_ALIASES.items() if normalize_team_name(c) not in js}
    assert not dangling, f"Alias targets with no logo: {dangling}"


def test_latin_forms_from_filenames_do_not_shadow_another_club():
    """ui.js adds each filename without .png and with spaces as a key. That key
    wins over an alias, so it must never be another club's alias."""
    js = _norm_map(JS_LOGO_MAP)
    clashes = {}
    for file in set(JS_LOGO_MAP.values()):
        latin = normalize_team_name(file.removesuffix(".png").replace("_", " "))
        for alias, club in JS_ALIASES.items():
            if normalize_team_name(alias) == latin and js.get(normalize_team_name(club)) != file:
                clashes[latin] = (file, club)
    assert not clashes, f"Filename form shadows another club's alias: {clashes}"
