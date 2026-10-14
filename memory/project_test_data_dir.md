---
name: project_test_data_dir
description: Test data files for cost-router go in /cost-router/data/, not tmp or project root
metadata:
  type: project
---

All test-related data files (fixtures, outputs, logs generated during test runs) live in `~/cost-router/data/`.

**Why:** Keeps test artifacts out of project root and tmp; consistent with the dev/prod split where data files have a dedicated location.

**How to apply:** When writing test fixtures, generating test output, or referencing data paths in tests, use `data/` relative to the project root — never `/tmp` or project root directly.
