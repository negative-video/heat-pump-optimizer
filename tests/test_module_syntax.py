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
