---
name: release
description: Full pre-release validation, version bump, tag, and push
disable-model-invocation: true
argument-hint: [patch|minor|major|x.y.z]
---

# Release Workflow for Heat Pump Optimizer

Execute the following release workflow. The user may provide a version bump type
(patch/minor/major) or an explicit version string like "0.2.0". Default to patch
if no argument is given.

IMPORTANT: Use `python3` for all commands, never bare `python`.

## Step 1: Pre-flight Validation
Run `python3 tools/validate.py` and show the output. If any check FAILS, STOP
and report the issue. Do not proceed until all checks pass.

## Step 2: Full Test Suite
Run `python3 -m pytest tests/ -x -q --tb=short`. If any test fails, STOP and
report the failure with details. Do not proceed.

## Step 3: Check Working Tree
Run `git status`. If there are uncommitted changes, STOP and ask the user
whether to include them in the release commit or abort.

## Step 4: Determine New Version
Read the current version from `custom_components/heatpump_optimizer/const.py`.
Calculate the new version based on the user's argument ($ARGUMENTS):
- **patch**: increment third number (0.1.29 -> 0.1.30)
- **minor**: increment second number, reset third (0.1.29 -> 0.2.0)
- **major**: increment first number, reset others (0.1.29 -> 1.0.0)
- **explicit**: use the version string as provided (e.g., "0.2.0")

Show the user: "Current version: X.Y.Z -> New version: A.B.C" and ask for
confirmation before proceeding.

## Step 5: Update Version Files
Update BOTH files:
1. `custom_components/heatpump_optimizer/const.py` — change `VERSION = "old"` to `VERSION = "new"`
2. `custom_components/heatpump_optimizer/manifest.json` — change `"version": "old"` to `"version": "new"`

## Step 6: Re-validate
Run `python3 tools/validate.py` again to confirm the version bump was applied
correctly to both files. If it fails, STOP.

## Step 7: Ask User to Confirm Commit
Show the user a summary of what will be committed and tagged. Ask for explicit
confirmation before proceeding. Do NOT commit or push without their approval.

## Step 8: Commit and Tag
```
git add custom_components/heatpump_optimizer/const.py custom_components/heatpump_optimizer/manifest.json
git commit -m "Bump version to A.B.C"
git tag vA.B.C
```

## Step 9: Ask User to Confirm Push
Tell the user: "Ready to push commit + tag vA.B.C to origin/main. Proceed?"
Do NOT push without their explicit approval.

## Step 10: Push
```
git push origin main --tags
```

## Step 11: Verify
Run `git log --oneline -3` and `git tag -l 'v*' | tail -5` to confirm.
Report: "Released vA.B.C successfully."
