#!/usr/bin/env python3
"""Validate cost-router data pipeline files against schema contracts.

Dev-safe: exits 0 with a "no data files" notice when DATA_DIR is absent or empty.
Production: exits 1 on any schema violation, 0 on clean pass.

Usage:
    python validate_data_integrity.py                        # validate all files in DATA_DIR
    python validate_data_integrity.py --file usage.jsonl     # one file only
    python validate_data_integrity.py --dir /path/to/data/   # explicit data directory
    python validate_data_integrity.py --json                  # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR  # noqa: E402
from data_schemas import (  # noqa: E402
    SCHEMAS,
    SESSION_LOG_COMMON,
    SESSION_LOG_EVENT_SCHEMA,
    SESSION_LOG_TURN_SCHEMA,
    SESSION_SIGNALS_SCHEMA,
    USAGE_COMMON,
    USAGE_EVENT_SCHEMAS,
)


@dataclass
class Violation:
    file: str
    line: int
    field: str | None
    message: str
    severity: str = "error"  # "error" | "warning"

    def __str__(self) -> str:
        loc = f"line {self.line}" + (f", field '{self.field}'" if self.field else "")
        return f"[{self.severity.upper()}] {self.file}:{loc}: {self.message}"


def validate_dict_record(
    record: dict,
    schema: dict,
    file_name: str,
    line_num: int,
) -> list[Violation]:
    violations: list[Violation] = []
    required = schema.get("required", {})
    invariants = schema.get("invariants", [])

    for field_name, expected_types in required.items():
        if field_name not in record:
            violations.append(Violation(
                file=file_name, line=line_num, field=field_name,
                message=f"required field missing",
            ))
            continue
        val = record[field_name]
        # bool is subclass of int — check bool first to avoid false positives
        if expected_types is int and isinstance(val, bool):
            violations.append(Violation(
                file=file_name, line=line_num, field=field_name,
                message=f"expected int, got bool",
            ))
            continue
        if not isinstance(val, expected_types):
            got = type(val).__name__
            exp = (
                expected_types.__name__
                if isinstance(expected_types, type)
                else " | ".join(t.__name__ for t in expected_types if t is not type(None))
                + (" | None" if type(None) in expected_types else "")
            )
            violations.append(Violation(
                file=file_name, line=line_num, field=field_name,
                message=f"expected {exp}, got {got}",
            ))

    for field_name, predicate, description in invariants:
        if field_name not in record:
            continue
        try:
            if not predicate(record[field_name]):
                violations.append(Violation(
                    file=file_name, line=line_num, field=field_name,
                    message=f"invariant failed: {description}",
                ))
        except Exception as exc:
            violations.append(Violation(
                file=file_name, line=line_num, field=field_name,
                message=f"invariant check error: {exc}", severity="warning",
            ))

    return violations


def validate_signals_record(
    record: object,
    file_name: str,
    line_num: int,
) -> list[Violation]:
    violations: list[Violation] = []
    schema = SESSION_SIGNALS_SCHEMA

    if not isinstance(record, list):
        return [Violation(
            file=file_name, line=line_num, field=None,
            message=f"expected JSON array, got {type(record).__name__}",
        )]

    if len(record) != schema["length"]:
        violations.append(Violation(
            file=file_name, line=line_num, field=None,
            message=f"expected {schema['length']} elements, got {len(record)}",
        ))
        return violations

    for idx, (expected_types, description) in schema["indices"].items():
        val = record[idx]
        if not isinstance(val, expected_types):
            got = type(val).__name__
            violations.append(Violation(
                file=file_name, line=line_num, field=f"[{idx}]",
                message=f"{description} — got {got}",
            ))

    # Value-level checks when types are correct
    if isinstance(record[1], str) and record[1] not in schema["valid_outcomes"]:
        violations.append(Violation(
            file=file_name, line=line_num, field="[1] outcome",
            message=f"unknown outcome '{record[1]}', expected one of {sorted(schema['valid_outcomes'])}",
            severity="warning",
        ))
    if isinstance(record[2], str) and record[2] not in schema["valid_confidences"]:
        violations.append(Violation(
            file=file_name, line=line_num, field="[2] confidence",
            message=f"unknown confidence '{record[2]}', expected one of {sorted(schema['valid_confidences'])}",
            severity="warning",
        ))

    return violations


def validate_session_log_record(
    record: dict,
    file_name: str,
    line_num: int,
) -> list[Violation]:
    """Type-dispatched validation for session_log.jsonl records."""
    violations = validate_dict_record(record, SESSION_LOG_COMMON, file_name, line_num)
    if "turn_id" in record:
        violations.extend(
            validate_dict_record(record, SESSION_LOG_TURN_SCHEMA, file_name, line_num)
        )
    elif "event_type" in record:
        violations.extend(
            validate_dict_record(record, SESSION_LOG_EVENT_SCHEMA, file_name, line_num)
        )
    return violations


def validate_usage_record(
    record: dict,
    file_name: str,
    line_num: int,
) -> list[Violation]:
    """Type-dispatched validation for usage.jsonl records."""
    violations = validate_dict_record(record, USAGE_COMMON, file_name, line_num)
    record_type = record.get("type")
    if record_type and record_type in USAGE_EVENT_SCHEMAS:
        violations.extend(
            validate_dict_record(record, USAGE_EVENT_SCHEMAS[record_type], file_name, line_num)
        )
    return violations


def validate_file(path: Path, schema: dict | None = None) -> list[Violation]:
    """Validate a single JSONL file. Returns [] if file doesn't exist (dev-safe)."""
    if not path.exists():
        return []

    file_name = path.name
    is_signals = file_name == "session_signals.jsonl"
    is_usage = file_name == "usage.jsonl"
    is_session_log = file_name == "session_log.jsonl"
    violations: list[Violation] = []

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [Violation(file=file_name, line=0, field=None, message=f"read error: {exc}")]

    for line_num, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            violations.append(Violation(
                file=file_name, line=line_num, field=None,
                message=f"invalid JSON: {exc}",
            ))
            continue

        if is_signals:
            violations.extend(validate_signals_record(record, file_name, line_num))
        elif is_session_log:
            if not isinstance(record, dict):
                violations.append(Violation(
                    file=file_name, line=line_num, field=None,
                    message=f"expected JSON object, got {type(record).__name__}",
                ))
                continue
            violations.extend(validate_session_log_record(record, file_name, line_num))
        elif is_usage:
            if not isinstance(record, dict):
                violations.append(Violation(
                    file=file_name, line=line_num, field=None,
                    message=f"expected JSON object, got {type(record).__name__}",
                ))
                continue
            violations.extend(validate_usage_record(record, file_name, line_num))
        elif schema is not None:
            if not isinstance(record, dict):
                violations.append(Violation(
                    file=file_name, line=line_num, field=None,
                    message=f"expected JSON object, got {type(record).__name__}",
                ))
                continue
            violations.extend(validate_dict_record(record, schema, file_name, line_num))

    return violations


def run_validation(data_dir: Path, target_file: str | None = None) -> tuple[list[Violation], int]:
    """Validate files in data_dir. Returns (violations, files_checked)."""
    all_violations: list[Violation] = []
    files_checked = 0

    files_to_check: list[tuple[str, dict | None]] = []
    # Files using type-dispatch (schema=None signals validate_file to use their own path)
    _type_dispatch_files = {"usage.jsonl", "session_log.jsonl"}
    if target_file:
        schema = None if target_file in _type_dispatch_files else SCHEMAS.get(target_file)
        files_to_check = [(target_file, schema)]
    else:
        files_to_check.append(("usage.jsonl", None))
        files_to_check.append(("session_log.jsonl", None))
        for name, schema in SCHEMAS.items():
            files_to_check.append((name, schema))
        files_to_check.append(("session_signals.jsonl", None))

    for file_name, schema in files_to_check:
        path = data_dir / file_name
        if not path.exists():
            continue
        files_checked += 1
        violations = validate_file(path, schema)
        all_violations.extend(violations)

    return all_violations, files_checked


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate cost-router data pipeline files")
    parser.add_argument("--file", help="validate a single named file (e.g. usage.jsonl)")
    parser.add_argument("--dir", help="data directory path (default: DATA_DIR from _common)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON report")
    args = parser.parse_args()

    data_dir = Path(args.dir) if args.dir else DATA_DIR

    if not data_dir.exists():
        if args.json:
            print(json.dumps({"status": "skipped", "reason": "data dir absent", "violations": []}))
        else:
            print(f"[skip] data directory not found: {data_dir}", file=sys.stderr)
        return 0

    violations, files_checked = run_validation(data_dir, target_file=args.file)

    if files_checked == 0:
        if args.json:
            print(json.dumps({"status": "skipped", "reason": "no data files present", "violations": []}))
        else:
            print("[skip] no data files present — nothing to validate")
        return 0

    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    if args.json:
        print(json.dumps({
            "status": "fail" if errors else "pass",
            "files_checked": files_checked,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "violations": [
                {"file": v.file, "line": v.line, "field": v.field,
                 "message": v.message, "severity": v.severity}
                for v in violations
            ],
        }, indent=2))
    else:
        for v in violations:
            print(str(v))
        status = "PASS" if not errors else "FAIL"
        print(f"\n{status}: {files_checked} file(s) checked, "
              f"{len(errors)} error(s), {len(warnings)} warning(s)")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
