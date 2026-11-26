"""The sidebar panel is one JavaScript file that no Python test executes.

Two things have gone wrong in it without anything noticing:

* 0.3.0 shipped a syntax error (a dangling ``else`` left by a refactor). The
  browser refused the module and the panel was a blank page.
* The cards looked entities up by the integration's internal key, while
  entity IDs come from display names. Seventeen lookups never matched, so the
  panel was permanently in its "learning" layout and the savings card never
  rendered.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess

import pytest

from tests.conftest import CC

PANEL = os.path.join(CC, "frontend", "panel.js")
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"

# Lookups the panel makes for things that are not entities. Each renders
# nothing when absent; listed so a new unmatched lookup has to be justified.
NOT_ENTITIES = {
    "tactical_correction",  # an attribute of current_phase, not an entity
    "source_health",        # never implemented as an entity
}


def _source() -> str:
    return io.open(PANEL, encoding="utf-8").read()


def _slug(name: str) -> str:
    """Home Assistant's entity-ID slug of a display name, near enough."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _sensor_keys_and_names() -> dict[str, str]:
    """Integration key -> default display name, for every sensor."""
    src = io.open(os.path.join(CC, "sensor.py"), encoding="utf-8").read()
    pairs = re.findall(
        r'super\(\).__init__\(\s*coordinator,\s*entry,\s*"([a-z0-9_]+)",\s*"([^"]+)"', src
    )
    return dict(pairs)


def _syntax_error() -> tuple[str | None, str | None]:
    node = shutil.which("node")
    if node:
        try:
            run = subprocess.run([node, "--check", PANEL], capture_output=True, text=True, timeout=60)
            if run.returncode == 0:
                return "node", None
            if "SyntaxError" in run.stderr:
                return "node", run.stderr.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if os.path.exists(JSC):
        script = f"try {{ checkSyntax({PANEL!r}); print('OK') }} catch (e) {{ print('ERR ' + e) }}"
        try:
            run = subprocess.run([JSC, "-e", script], capture_output=True, text=True, timeout=60)
            if run.stdout.strip() == "OK":
                return "jsc", None
            if run.stdout.startswith("ERR "):
                return "jsc", run.stdout.strip()[4:]
        except (OSError, subprocess.SubprocessError):
            pass
    return None, None


def test_panel_parses():
    engine, error = _syntax_error()
    if engine is None:
        pytest.skip("no JavaScript engine available (node or JavaScriptCore)")
    assert error is None, f"panel.js does not parse ({engine}): {error}"


def test_panel_has_no_dangling_else():
    """Cheap guard for the exact 0.3.0 failure, for machines with no JS engine."""
    for number, line in enumerate(_source().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("else ") or stripped == "else":
            previous = _source().splitlines()[number - 2].strip()
            assert not previous.startswith(("let ", "const ", "var ")) or previous.endswith(
                ("{", ",", "(", "?", ":", "&&", "||")
            ), f"panel.js:{number}: 'else' directly after a declaration"


def test_every_panel_lookup_names_a_real_entity():
    """Each findEntity key is a sensor key, the switch, or a declared exception."""
    keys = set(_sensor_keys_and_names())
    lookups = set(re.findall(r'findEntity\(\w+,\s*"([a-z0-9_]+)"\)', _source()))
    aliases = dict(re.findall(r"^\s+([a-z0-9_]+): \"([a-z0-9_]+)\",$", _source(), re.M))

    unknown = sorted(
        key for key in lookups
        if key not in keys
        and key not in NOT_ENTITIES
        and key != "enabled"
        and key not in aliases
        and key not in aliases.values()  # a direct lookup of a legacy ID suffix
    )
    assert not unknown, f"panel looks up entities that do not exist: {unknown}"


# ID suffixes that only exist on installs created before an entity was
# renamed. Entity IDs are fixed at first registration, so these stay valid
# there; a fresh install is resolved through the registry key instead.
LEGACY_ID_SUFFIXES = {
    "energy_saved_today",  # savings_kwh_today, before "Modeled Savings Today"
}


def test_aliases_point_at_the_ids_existing_installs_have():
    """An alias maps a key to the ID suffix its default name produces."""
    names = _sensor_keys_and_names()
    aliases = dict(re.findall(r"^\s+([a-z0-9_]+): \"([a-z0-9_]+)\",$", _source(), re.M))
    assert aliases, "ENTITY_ALIASES not found in panel.js"
    wrong = {
        key: (suffix, _slug(names[key]))
        for key, suffix in aliases.items()
        if key in names and _slug(names[key]) != suffix and suffix not in LEGACY_ID_SUFFIXES
    }
    assert not wrong, f"alias does not match the entity's default name slug: {wrong}"


def test_panel_resolves_entities_through_the_registry():
    """Key-based resolution is what keeps a rename or translation from breaking it."""
    src = _source()
    assert "function indexEntities(hass)" in src
    assert 'entry.platform === "heatpump_optimizer" && entry.translation_key' in src
    assert "indexEntities(hass);" in src
