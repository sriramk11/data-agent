"""Dataset ingestion and schema introspection.

This runs in the trusted backend process, NOT the sandbox -- reading a
user's own uploaded file to list its columns is an ordinary file operation,
not model-generated code, so it doesn't need sandboxing. Only code the LLM
writes goes through the sandbox (see app/sandbox/).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from app.models import SessionSchemaColumn


def ingest_csv(src_path: Path, data_dir: Path) -> tuple[str, list[SessionSchemaColumn], list[dict[str, Any]], int]:
    """Copy an uploaded CSV into the session's sandbox-mounted data dir and
    sniff a lightweight schema (column names, inferred dtype, a few sample
    rows) so the system prompt can describe the dataset without the model
    having to blind-guess column names on turn 1.
    """
    dest = data_dir / src_path.name
    dest.write_bytes(src_path.read_bytes())

    with dest.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = []
        row_count = 0
        for row in reader:
            row_count += 1
            if len(rows) < 5:
                rows.append(row)

    columns = [SessionSchemaColumn(name=name, dtype=_sniff_dtype(rows, name)) for name in fieldnames]
    return dest.name, columns, rows, row_count


def _sniff_dtype(sample_rows: list[dict[str, Any]], col: str) -> str:
    values = [r.get(col, "") for r in sample_rows if r.get(col, "") not in ("", None)]
    if not values:
        return "string"
    if all(_is_int(v) for v in values):
        return "int"
    if all(_is_float(v) for v in values):
        return "float"
    if all(_is_date(v) for v in values):
        return "date"
    return "string"


def _is_int(v: str) -> bool:
    try:
        int(v)
        return True
    except ValueError:
        return False


def _is_float(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def _is_date(v: str) -> bool:
    import re
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}", v))


def schema_prompt_block(data_path: str, columns: list[SessionSchemaColumn], sample_rows: list[dict[str, Any]], row_count: int) -> str:
    cols = "\n".join(f"  - {c.name} ({c.dtype})" for c in columns)
    sample = "\n".join(str(r) for r in sample_rows[:3])
    return (
        f"Dataset file: {data_path}\n"
        f"Row count: ~{row_count}\n"
        f"Columns:\n{cols}\n"
        f"Sample rows:\n{sample}\n"
    )
