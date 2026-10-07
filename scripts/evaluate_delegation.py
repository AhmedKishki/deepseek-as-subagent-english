"""Bounded, model-free evaluator for hand-annotated delegation records.

This tool reads evaluation-record files (JSON or JSONL) that a person annotates
after reviewing run traces, then prints one descriptive aggregate as JSON on
stdout. It never executes a model, a tool, or a shell command: it parses JSON
and does arithmetic only.

The aggregate describes the supplied annotations. It is not independent proof
of correctness, and it never infers correctness from call counts, model
confidence, or a worker's self-reported completion. Missing observations stay
unknown; they are never folded into a value of zero.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RECORDS = 100_000
SCHEMA_VERSION = 1
FIT_VALUES = ("delegate", "host")
CAPABILITY_VALUES = ("readonly", "coding")
CATEGORY_VALUES = ("task_fit", "adversarial_evidence")

DISCLAIMER = (
    "Descriptive aggregate over hand-annotated records. Not independent "
    "correctness proof; annotations are not model or tool output. Unknown "
    "observations remain unknown and are never treated as zero."
)


class EvaluationError(ValueError):
    """Raised for a bounded, invalid, or inconsistent input."""


def _reject_constant(name: str) -> float:
    raise EvaluationError(f"non-finite JSON constant {name!r} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise EvaluationError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def _parse_json(text: str) -> object:
    return json.loads(
        text,
        parse_constant=_reject_constant,
        object_pairs_hook=_no_duplicate_keys,
    )


def _read_bounded_text(path: Path, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EvaluationError(f"cannot stat {path}: {exc}") from exc
    if size > max_bytes:
        raise EvaluationError(f"{path} exceeds the {max_bytes}-byte file limit")
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise EvaluationError(f"cannot read {path}: {exc}") from exc
    if len(data) > max_bytes:
        raise EvaluationError(f"{path} exceeds the {max_bytes}-byte file limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvaluationError(f"{path} is not valid UTF-8") from exc


def _require_str(value: object, field: str, context: str, max_len: int) -> str:
    if not isinstance(value, str) or not value:
        raise EvaluationError(f"{context}: {field} must be a non-empty string")
    if len(value) > max_len:
        raise EvaluationError(f"{context}: {field} exceeds {max_len} characters")
    return value


def _optional_str(value: object, field: str, context: str, max_len: int) -> str | None:
    if value is None:
        return None
    return _require_str(value, field, context, max_len)


def _require_bool(value: object, field: str, context: str) -> bool:
    if type(value) is not bool:
        raise EvaluationError(
            f"{context}: {field} must be a boolean, got {type(value).__name__}"
        )
    return value


def _optional_bool(value: object, field: str, context: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, field, context)


def _finite_number(value: object, field: str, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(f"{context}: {field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise EvaluationError(f"{context}: {field} must be finite")
    return number


def _nonnegative_number(value: object, field: str, context: str) -> float:
    number = _finite_number(value, field, context)
    if number < 0:
        raise EvaluationError(f"{context}: {field} must be >= 0")
    return number


def _optional_nonnegative_number(
    value: object, field: str, context: str
) -> float | None:
    if value is None:
        return None
    return _nonnegative_number(value, field, context)


def _optional_nonnegative_int(value: object, field: str, context: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationError(f"{context}: {field} must be an integer")
    if value < 0:
        raise EvaluationError(f"{context}: {field} must be >= 0")
    return value


def _optional_choice(
    value: object, field: str, context: str, allowed: tuple[str, ...]
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise EvaluationError(
            f"{context}: {field} must be one of {', '.join(allowed)} or null"
        )
    return value


def _validate_record(raw: object, index: int, source: str) -> dict:
    context = f"{source} record {index}"
    if not isinstance(raw, dict):
        raise EvaluationError(f"{context} must be a JSON object")
    return {
        "case_id": _require_str(raw.get("case_id"), "case_id", context, 200),
        "variant": _require_str(raw.get("variant"), "variant", context, 64),
        "run_id": _optional_str(raw.get("run_id"), "run_id", context, 64),
        "expected_fit": _optional_choice(
            raw.get("expected_fit"), "expected_fit", context, FIT_VALUES
        ),
        "capability": _optional_choice(
            raw.get("capability"), "capability", context, CAPABILITY_VALUES
        ),
        "category": _optional_choice(
            raw.get("category"), "category", context, CATEGORY_VALUES
        ),
        "delegated": _require_bool(raw.get("delegated"), "delegated", context),
        "success": _optional_bool(raw.get("success"), "success", context),
        "duplicate_work": _optional_bool(
            raw.get("duplicate_work"), "duplicate_work", context
        ),
        "unsupported_claim_accepted": _optional_bool(
            raw.get("unsupported_claim_accepted"),
            "unsupported_claim_accepted",
            context,
        ),
        "duration_seconds": _nonnegative_number(
            raw.get("duration_seconds"), "duration_seconds", context
        ),
        "parent_tokens": _optional_nonnegative_int(
            raw.get("parent_tokens"), "parent_tokens", context
        ),
        "worker_tokens": _optional_nonnegative_int(
            raw.get("worker_tokens"), "worker_tokens", context
        ),
        "cost": _optional_nonnegative_number(raw.get("cost"), "cost", context),
    }


def _dedupe_key(record: dict) -> tuple[str, str, str | None]:
    return (record["variant"], record["case_id"], record["run_id"])


def _records_from_object(data: object, source: Path) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "records" in data:
            if not isinstance(data["records"], list):
                raise EvaluationError(f"{source}: 'records' must be a list")
            return data["records"]
        if "case_id" in data:
            return [data]
    raise EvaluationError(
        f"{source}: expected a JSON array, a single record, or an object with 'records'"
    )


def _records_from_jsonl(text: str, source: Path) -> list:
    records: list = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = _parse_json(line)
        except EvaluationError:
            raise
        except ValueError as exc:
            raise EvaluationError(f"{source} line {number}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise EvaluationError(f"{source} line {number} must be a JSON object")
        records.append(parsed)
    return records


def _records_from_text(text: str, source: Path) -> list:
    stripped = text.strip()
    if not stripped:
        raise EvaluationError(f"{source} is empty")
    if source.suffix.lower() in (".jsonl", ".ndjson"):
        return _records_from_jsonl(stripped, source)
    try:
        data = _parse_json(stripped)
    except EvaluationError:
        raise
    except ValueError as exc:
        raise EvaluationError(f"{source}: {exc}") from exc
    return _records_from_object(data, source)


def load_records(paths: list[Path], max_bytes: int, max_records: int) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()
    for path in paths:
        raw_records = _records_from_text(_read_bounded_text(path, max_bytes), path)
        for index, raw in enumerate(raw_records):
            record = _validate_record(raw, index, str(path))
            key = _dedupe_key(record)
            if key in seen:
                variant, case_id, run_id = key
                raise EvaluationError(
                    "duplicate record for variant "
                    f"{variant!r}, case_id {case_id!r}"
                    + (f", run_id {run_id!r}" if run_id else "")
                    + "; use a distinct run_id for repetitions"
                )
            seen.add(key)
            records.append(record)
            if len(records) > max_records:
                raise EvaluationError(f"record count exceeds {max_records}")
    return records


def load_cases(path: Path, max_bytes: int) -> dict[str, dict]:
    data = _parse_json(_read_bounded_text(path, max_bytes))
    if isinstance(data, dict) and "cases" in data:
        entries = data["cases"]
    elif isinstance(data, list):
        entries = data
    else:
        raise EvaluationError(f"{path}: expected a case list or an object with 'cases'")
    if not isinstance(entries, list):
        raise EvaluationError(f"{path}: 'cases' must be a list")
    by_id: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        context = f"{path} case {index}"
        if not isinstance(entry, dict):
            raise EvaluationError(f"{context} must be a JSON object")
        case_id = _require_str(entry.get("case_id"), "case_id", context, 200)
        if case_id in by_id:
            raise EvaluationError(f"{path}: duplicate case_id {case_id!r}")
        by_id[case_id] = {
            "expected_fit": _optional_choice(
                entry.get("expected_fit"), "expected_fit", context, FIT_VALUES
            ),
            "capability": _optional_choice(
                entry.get("capability"), "capability", context, CAPABILITY_VALUES
            ),
            "category": _optional_choice(
                entry.get("category"), "category", context, CATEGORY_VALUES
            ),
        }
    return by_id


def _empty_group() -> dict:
    return {
        "records": 0,
        "unknown_case_ids": 0,
        "delegated": {"true": 0, "false": 0},
        "capability": {"readonly": 0, "coding": 0, "unknown": 0},
        "by_expected_fit": {
            "delegate": {
                "records": 0,
                "delegated": 0,
                "not_delegated": 0,
                "appropriate_delegation": 0,
            },
            "host": {
                "records": 0,
                "delegated": 0,
                "not_delegated": 0,
                "appropriate_delegation": 0,
            },
            "unknown": {"records": 0},
        },
        "appropriate_delegation": {"numerator": 0, "denominator": 0},
        "success": {"true": 0, "false": 0, "unknown": 0},
        "duplicate_work": {"true": 0, "false": 0, "unknown": 0},
        "unsupported_claim_accepted": {"true": 0, "false": 0, "unknown": 0},
        "duration_seconds": {"total": 0.0, "known": 0},
        "parent_tokens": {"total": 0, "known": 0, "unknown": 0},
        "worker_tokens": {"total": 0, "known": 0, "unknown": 0},
        "cost": {"total": 0.0, "known": 0, "unknown": 0},
    }


def _accumulate(group: dict, record: dict) -> None:
    group["records"] += 1
    if record["unknown_case_id"]:
        group["unknown_case_ids"] += 1
    if record["delegated"]:
        group["delegated"]["true"] += 1
    else:
        group["delegated"]["false"] += 1
    capability = record["capability"]
    group["capability"][capability if capability else "unknown"] += 1

    fit = record["expected_fit"]
    if fit in FIT_VALUES:
        bucket = group["by_expected_fit"][fit]
        bucket["records"] += 1
        if record["delegated"]:
            bucket["delegated"] += 1
        else:
            bucket["not_delegated"] += 1
        appropriate = record["delegated"] == (fit == "delegate")
        if appropriate:
            bucket["appropriate_delegation"] += 1
        group["appropriate_delegation"]["denominator"] += 1
        if appropriate:
            group["appropriate_delegation"]["numerator"] += 1
    else:
        group["by_expected_fit"]["unknown"]["records"] += 1

    for field in ("success", "duplicate_work", "unsupported_claim_accepted"):
        value = record[field]
        if value is True:
            group[field]["true"] += 1
        elif value is False:
            group[field]["false"] += 1
        else:
            group[field]["unknown"] += 1

    group["duration_seconds"]["total"] += record["duration_seconds"]
    group["duration_seconds"]["known"] += 1
    for field in ("parent_tokens", "worker_tokens", "cost"):
        value = record[field]
        if value is None:
            group[field]["unknown"] += 1
        else:
            group[field]["total"] += value
            group[field]["known"] += 1


def _rate(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _finalize(group: dict) -> dict:
    group["delegated"]["known"] = (
        group["delegated"]["true"] + group["delegated"]["false"]
    )
    group["delegated"]["rate"] = _rate(
        group["delegated"]["true"], group["delegated"]["known"]
    )
    for field in ("success", "duplicate_work", "unsupported_claim_accepted"):
        bucket = group[field]
        bucket["known"] = bucket["true"] + bucket["false"]
        bucket["rate"] = _rate(bucket["true"], bucket["known"])
    for fit in FIT_VALUES:
        bucket = group["by_expected_fit"][fit]
        bucket["delegated_rate"] = _rate(bucket["delegated"], bucket["records"])
        bucket["appropriate_rate"] = _rate(
            bucket["appropriate_delegation"], bucket["records"]
        )
    appropriate = group["appropriate_delegation"]
    appropriate["rate"] = _rate(appropriate["numerator"], appropriate["denominator"])
    duration = group["duration_seconds"]
    duration["mean"] = _rate(duration["total"], duration["known"])
    for field in ("parent_tokens", "worker_tokens", "cost"):
        group[field]["mean"] = _rate(group[field]["total"], group[field]["known"])
    return group


def evaluate_records(
    records: list[dict], cases_by_id: dict[str, dict] | None = None
) -> dict:
    groups: dict[str, dict] = {"overall": _empty_group()}
    for record in records:
        fit = record["expected_fit"]
        capability = record["capability"]
        category = record["category"]
        unknown_case_id = False
        if cases_by_id is not None:
            entry = cases_by_id.get(record["case_id"])
            if entry is None:
                unknown_case_id = True
            else:
                if fit is None:
                    fit = entry["expected_fit"]
                if capability is None:
                    capability = entry["capability"]
                if category is None:
                    category = entry["category"]
        effective = dict(record)
        effective["expected_fit"] = fit
        effective["capability"] = capability
        effective["category"] = category
        effective["unknown_case_id"] = unknown_case_id

        _accumulate(groups["overall"], effective)
        variant_group = groups.setdefault(
            "variant:" + record["variant"], _empty_group()
        )
        _accumulate(variant_group, effective)

    _finalize(groups["overall"])
    variants = {
        name.split("variant:", 1)[1]: _finalize(groups[name])
        for name in sorted(groups)
        if name != "overall"
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "record_count": len(records),
        "cases_joined": cases_by_id is not None,
        "overall": groups["overall"],
        "variants": variants,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read hand-annotated delegation evaluation records and print a "
            "descriptive aggregate as JSON. Reads files only; never runs a "
            "model, a tool, or a shell command."
        )
    )
    parser.add_argument("records", nargs="+", type=Path)
    parser.add_argument(
        "--cases",
        type=Path,
        default=None,
        help="Optional case-definition JSON used to join expected_fit.",
    )
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--indent", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.max_bytes <= 0 or args.max_records <= 0:
            raise EvaluationError("--max-bytes and --max-records must be positive")
        records = load_records(args.records, args.max_bytes, args.max_records)
        cases = None
        if args.cases is not None:
            cases = load_cases(args.cases, args.max_bytes)
        aggregate = evaluate_records(records, cases)
    except EvaluationError as exc:
        print(f"evaluate_delegation: error: {exc}", file=sys.stderr)
        return 2
    json.dump(aggregate, sys.stdout, indent=args.indent, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
