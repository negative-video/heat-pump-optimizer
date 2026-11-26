"""Every integration module must at least parse.

The suite loads modules individually through conftest's loader, so a module
no test imports can carry a syntax error and still show green. That happened
during the EKF removal: deleting a call left an empty `try:` in coordinator.py
and 1198 tests passed anyway. tools/validate.py caught it, but validate only
runs at commit time via the hook, which is later than it should be.

This is deliberately a parse check, not an import check. Importing the
package pulls in Home Assistant, which is not installed in this environment.
"""

import ast
import io
import os

from tests.conftest import CC


def _module_paths():
    for root, _dirs, files in os.walk(CC):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _undefined_names(path):
    """Names a module loads but never binds anywhere in the file.

    Coarse on purpose: one bound-set per file, so it cannot tell scopes apart,
    but it also cannot raise a false alarm on a name that is defined somewhere
    in the module. Names used only inside annotations are skipped because
    every module here uses ``from __future__ import annotations``.
    """
    import builtins

    tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}
    annotation_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            bound.add(node.name)
        for field in ("annotation", "returns"):
            sub = getattr(node, field, None)
            if sub is not None:
                annotation_nodes.update(id(n) for n in ast.walk(sub))
    return sorted({
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id not in bound
        and id(node) not in annotation_nodes
    })


def test_every_module_parses():
    failures = []
    for path in _module_paths():
        try:
            ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
        except SyntaxError as err:
            failures.append(f"{os.path.relpath(path, CC)}:{err.lineno}: {err.msg}")
    assert not failures, "modules with syntax errors:\n  " + "\n  ".join(failures)


def test_no_module_references_the_removed_estimator():
    """Audit item 8 deleted the EKF. Nothing may reach for it again."""
    banned = ("ThermalEstimator", "AdaptivePerformanceModel", "thermal_estimator",
              "adaptive_performance_model")
    offenders = []
    for path in _module_paths():
        text = io.open(path, encoding="utf-8").read()
        hits = [b for b in banned if b in text]
        if hits:
            offenders.append(f"{os.path.relpath(path, CC)}: {', '.join(hits)}")
    assert not offenders, (
        "references to the removed thermal estimator:\n  " + "\n  ".join(offenders)
    )


def test_no_module_uses_an_undefined_name():
    """A missing import must fail the suite, not the first Home Assistant start.

    ``coordinator_helpers.py`` once decorated a method with ``@callback``
    without importing it. The module raised NameError on import, so the whole
    integration failed to load, yet 1213 tests and tools/validate.py passed:
    nothing imports the coordinator here because Home Assistant is not
    installed. A parse check cannot see this; a name scan can.
    """
    offenders = []
    for path in _module_paths():
        for lineno, name in _undefined_names(path):
            offenders.append(f"{os.path.relpath(path, CC)}:{lineno}: {name}")
    assert not offenders, "undefined names:\n  " + "\n  ".join(offenders)
