#!/usr/bin/env python3
"""Pre-release validation for the Heat Pump Optimizer HACS integration.

Runs structural checks that catch the most common release-breaking issues:
version mismatches, translation desync, broken imports, syntax errors,
and missing migration handlers. Stdlib only — no HA or pip deps needed.

Exit 0 = all checks pass.  Exit 1 = at least one check failed.
"""

import ast
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = REPO_ROOT / "custom_components" / "heatpump_optimizer"

passes = 0
failures = 0


def ok(label: str, detail: str = "") -> None:
    global passes
    passes += 1
    print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))


def fail(label: str, detail: str) -> None:
    global failures
    failures += 1
    print(f"  FAIL  {label}")
    for line in detail.strip().splitlines():
        print(f"        {line}")


# ── 1. Version sync (const.py == manifest.json) ────────────────────────

def check_version_sync() -> None:
    const_path = INTEGRATION / "const.py"
    manifest_path = INTEGRATION / "manifest.json"

    const_version = None
    for line in const_path.read_text().splitlines():
        m = re.match(r'^VERSION\s*=\s*["\']([^"\']+)["\']', line)
        if m:
            const_version = m.group(1)
            break

    if const_version is None:
        fail("version-sync", "Could not parse VERSION from const.py")
        return

    manifest = json.loads(manifest_path.read_text())
    manifest_version = manifest.get("version")

    if const_version == manifest_version:
        ok("version-sync", f"v{const_version}")
    else:
        fail("version-sync",
             f"const.py has {const_version!r}, manifest.json has {manifest_version!r}")


# ── 2. JSON syntax ─────────────────────────────────────────────────────

def check_json_syntax() -> None:
    json_files = [
        INTEGRATION / "manifest.json",
        INTEGRATION / "strings.json",
        INTEGRATION / "translations" / "en.json",
    ]
    # Optional files that may or may not exist
    optional = [INTEGRATION / "hacs.json"]
    for p in optional:
        if p.exists():
            json_files.append(p)

    all_ok = True
    errors = []
    for p in json_files:
        if not p.exists():
            errors.append(f"{p.name}: file not found")
            all_ok = False
            continue
        try:
            json.loads(p.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"{p.name}: {e}")
            all_ok = False

    if all_ok:
        ok("json-syntax", f"{len(json_files)} files")
    else:
        fail("json-syntax", "\n".join(errors))


# ── 3. Translation sync (strings.json == translations/en.json) ─────────

def _deep_diff(a, b, path: str = "") -> list[str]:
    """Return list of key-path differences between two nested dicts."""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            p = f"{path}.{key}" if path else key
            if key not in a:
                diffs.append(f"  + {p} (only in translations/en.json)")
            elif key not in b:
                diffs.append(f"  - {p} (only in strings.json)")
            else:
                diffs.extend(_deep_diff(a[key], b[key], p))
    elif a != b:
        diffs.append(f"  ~ {path}: values differ")
    return diffs


def check_translation_sync() -> None:
    strings_path = INTEGRATION / "strings.json"
    en_path = INTEGRATION / "translations" / "en.json"

    if not strings_path.exists() or not en_path.exists():
        fail("translation-sync", "strings.json or translations/en.json missing")
        return

    strings = json.loads(strings_path.read_text())
    en = json.loads(en_path.read_text())

    diffs = _deep_diff(strings, en)
    if not diffs:
        ok("translation-sync")
    else:
        fail("translation-sync",
             f"{len(diffs)} difference(s):\n" + "\n".join(diffs[:20]))


# ── 4. Python syntax ──────────────────────────────────────────────────

def check_python_syntax() -> None:
    errors = []
    count = 0
    for py in INTEGRATION.rglob("*.py"):
        count += 1
        try:
            ast.parse(py.read_text(), filename=str(py))
        except SyntaxError as e:
            errors.append(f"{py.relative_to(REPO_ROOT)}:{e.lineno}: {e.msg}")

    if not errors:
        ok("python-syntax", f"{count} files")
    else:
        fail("python-syntax", "\n".join(errors))


# ── 5. Import resolution (relative imports point to real files) ─────────

def check_import_resolution() -> None:
    errors = []
    count = 0

    for py in INTEGRATION.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError:
            continue  # already reported by check_python_syntax

        pkg_dir = py.parent

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 0:
                continue  # absolute import — skip

            # Resolve the relative import target directory
            target_dir = pkg_dir
            for _ in range(node.level - 1):
                target_dir = target_dir.parent

            if node.module:
                parts = node.module.split(".")
                resolved = target_dir / "/".join(parts)

                # Could be a package (dir/__init__.py) or module (file.py)
                if not (resolved.with_suffix(".py").exists() or
                        (resolved.is_dir() and (resolved / "__init__.py").exists())):
                    count += 1
                    rel = py.relative_to(REPO_ROOT)
                    errors.append(f"{rel}:{node.lineno}: from {'.'*node.level}{node.module} — target not found")

    if not errors:
        ok("import-resolution", "all relative imports resolve")
    else:
        fail("import-resolution", f"{count} broken import(s):\n" + "\n".join(errors[:20]))


# ── 6. Manifest structure ──────────────────────────────────────────────

def check_manifest_structure() -> None:
    manifest_path = INTEGRATION / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        fail("manifest-structure", "Cannot read manifest.json")
        return

    required_fields = ["domain", "name", "version", "config_flow", "documentation", "iot_class"]
    missing = [f for f in required_fields if f not in manifest]

    errors = []
    if missing:
        errors.append(f"Missing required fields: {', '.join(missing)}")

    if manifest.get("domain") != "heatpump_optimizer":
        errors.append(f"domain is {manifest.get('domain')!r}, expected 'heatpump_optimizer'")

    version = manifest.get("version", "")
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        errors.append(f"version {version!r} does not match X.Y.Z pattern")

    if not errors:
        ok("manifest-structure")
    else:
        fail("manifest-structure", "\n".join(errors))


# ── 7. Migration coverage ──────────────────────────────────────────────

def check_migration_coverage() -> None:
    # Parse config_flow.py for VERSION = N inside the ConfigFlow class
    cf_path = INTEGRATION / "config_flow.py"
    cf_text = cf_path.read_text()

    cf_version = None
    for m in re.finditer(r"^\s+VERSION\s*=\s*(\d+)", cf_text, re.MULTILINE):
        cf_version = int(m.group(1))

    if cf_version is None:
        fail("migration-coverage", "Could not find VERSION in config_flow.py")
        return

    if cf_version <= 1:
        ok("migration-coverage", "VERSION=1, no migrations needed")
        return

    # Scan __init__.py for migration handlers
    init_path = INTEGRATION / "__init__.py"
    init_text = init_path.read_text()

    missing = []
    for v in range(2, cf_version + 1):
        # Look for patterns like: config_entry.version < 2  or  version < 2
        pattern = rf"version\s*<\s*{v}"
        if not re.search(pattern, init_text):
            missing.append(f"v{v-1}→v{v}")

    if not missing:
        ok("migration-coverage", f"VERSION={cf_version}, all migrations present")
    else:
        fail("migration-coverage",
             f"config_flow VERSION={cf_version}, missing migration(s): {', '.join(missing)}")


# ── Main ───────────────────────────────────────────────────────────────

def main() -> int:
    print("Heat Pump Optimizer — Pre-release Validation")
    print("=" * 50)

    check_version_sync()
    check_json_syntax()
    check_translation_sync()
    check_python_syntax()
    check_import_resolution()
    check_manifest_structure()
    check_migration_coverage()

    print("=" * 50)
    total = passes + failures
    print(f"{passes}/{total} checks passed", end="")
    if failures:
        print(f", {failures} FAILED")
        return 1
    else:
        print(" — all clear")
        return 0


if __name__ == "__main__":
    sys.exit(main())
