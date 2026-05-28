#!/usr/bin/env python3
"""Compare result sets from two SQL files against the same database.

Reads query_one.txt and query_two.txt (or paths from flags), executes each in
order within a single connection (non-final statements consume no comparison;
the final statement must return a rowset).

Connection defaults mirror toolbox/db_benchmark/benchmark_db.py (PGHOST,
PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGOPTIONS, session GUC env vars).

Exit code 0 when results match, 1 when they differ or on error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


SESSION_GUC_ENV_VARS = (
    ("PG_RANDOM_PAGE_COST", "random_page_cost"),
    ("PG_EFFECTIVE_IO_CONCURRENCY", "effective_io_concurrency"),
    ("PG_DEFAULT_STATISTICS_TARGET", "default_statistics_target"),
    ("PG_JIT", "jit"),
)
STARTUP_GUC_ENV_VARS = (
    ("PG_MAX_CONNECTIONS", "max_connections"),
    ("PG_WAL_COMPRESSION", "wal_compression"),
)


@dataclass(frozen=True)
class QueryOutcome:
    column_names: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


def parse_sql_statements(sql_text: str) -> list[str]:
    statements = [chunk.strip() for chunk in sql_text.split(";")]
    return [f"{statement};" for statement in statements if statement]


def build_pgoptions_from_env(raw_pgoptions: str | None) -> str | None:
    if raw_pgoptions and raw_pgoptions.strip():
        return raw_pgoptions

    options_parts: list[str] = []
    for env_var, guc_name in SESSION_GUC_ENV_VARS:
        value = os.getenv(env_var)
        if value and value.strip():
            options_parts.extend(["-c", f"{guc_name}={value.strip()}"])

    if not options_parts:
        return None
    return " ".join(options_parts)


def warn_for_startup_only_env_tunables() -> None:
    ignored = []
    for env_var, guc_name in STARTUP_GUC_ENV_VARS:
        value = os.getenv(env_var)
        if value and value.strip():
            ignored.append(f"{env_var} ({guc_name})")
    if ignored:
        print(
            "Note: startup-only PostgreSQL tunables are configured at server start "
            f"(ignored here): {', '.join(ignored)}",
            file=sys.stderr,
        )


def run_final_result_set(
    sql_text: str,
    *,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    options: str | None,
    connect_timeout: int | None,
    label: str,
) -> QueryOutcome:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required. Install with: pip install -r requirements.txt"
        ) from exc

    statements = parse_sql_statements(sql_text)
    if not statements:
        raise ValueError(f"{label}: no SQL statements found.")

    connect_kwargs = {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "options": options,
        "autocommit": True,
    }
    if connect_timeout is not None:
        connect_kwargs["connect_timeout"] = connect_timeout

    with psycopg.connect(**connect_kwargs) as conn:
        with conn.cursor() as cur:
            for stmt in statements[:-1]:
                cur.execute(stmt)
                if cur.description is not None:
                    cur.fetchall()
            cur.execute(statements[-1])
            if cur.description is None:
                raise ValueError(
                    f"{label}: final statement does not return a result set "
                    "(expected SELECT or similar)."
                )
            column_names = tuple(d.name for d in cur.description)
            rows_tuple = tuple(cur.fetchall())

    return QueryOutcome(column_names=column_names, rows=rows_tuple)


def _format_cell(value: object) -> str:
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    return repr(value)


def _format_row(row: tuple[object, ...]) -> str:
    return "(" + ", ".join(_format_cell(v) for v in row) + ")"


def _format_row_index_samples(indices: list[int], *, limit: int = 48) -> str:
    ordered = sorted(set(indices))
    if not ordered:
        return ""
    if len(ordered) <= limit:
        return ", ".join(str(n) for n in ordered)
    half = limit // 2
    head = ordered[:half]
    tail = ordered[-half:]
    omit = len(ordered) - len(head) - len(tail)
    return ", ".join(str(n) for n in head) + f" … (+{omit} more) … " + ", ".join(
        str(n) for n in tail
    )


def _normalize_identifier(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _find_project_id_column_index(column_names: tuple[str, ...]) -> int | None:
    targets = {"projectid"}
    for idx, name in enumerate(column_names):
        if _normalize_identifier(name) in targets:
            return idx
    return None


def _print_project_id_context(
    column_names: tuple[str, ...],
    r1: tuple[object, ...] | None,
    r2: tuple[object, ...] | None,
) -> None:
    project_idx = _find_project_id_column_index(column_names)
    if project_idx is None:
        return

    has_r1 = r1 is not None and project_idx < len(r1)
    has_r2 = r2 is not None and project_idx < len(r2)
    if not has_r1 and not has_r2:
        return

    label = column_names[project_idx]
    v1 = r1[project_idx] if has_r1 else "<missing>"
    v2 = r2[project_idx] if has_r2 else "<missing>"

    if has_r1 and has_r2 and v1 == v2:
        print(f"  {label}: {_format_cell(v1)}")
    else:
        print(f"  {label} (query_one): {_format_cell(v1)}")
        print(f"  {label} (query_two): {_format_cell(v2)}")


def _collect_aligned_value_mismatch_fields(
    column_names: tuple[str, ...],
    rows_one: tuple[tuple[object, ...], ...],
    rows_two: tuple[tuple[object, ...], ...],
    mismatch_indexes: list[int],
) -> tuple[
    dict[tuple[int, str], list[int]],
    list[int],
    list[int],
]:
    """Across all mismatched row indexes: which columns disagreed anywhere.

    Returns (aligned_field -> sorted row indexes, missing_row_indexes,
    arity_mismatch_indexes).
    """
    buckets: defaultdict[tuple[int, str], list[int]] = defaultdict(list)
    missing_row: list[int] = []
    arity_bad: list[int] = []

    for i in mismatch_indexes:
        r1 = rows_one[i] if i < len(rows_one) else None
        r2 = rows_two[i] if i < len(rows_two) else None
        if r1 is None or r2 is None:
            missing_row.append(i)
            continue
        if len(r1) != len(r2) or len(r1) != len(column_names):
            arity_bad.append(i)
            continue
        for j, name in enumerate(column_names):
            if r1[j] != r2[j]:
                buckets[(j, name)].append(i)

    compact = {key: sorted(set(lst)) for key, lst in buckets.items()}
    return compact, sorted(set(missing_row)), sorted(set(arity_bad))


def _print_mismatch_summary(
    *,
    schema_mode: bool,
    names_one: tuple[str, ...],
    names_two: tuple[str, ...],
    field_row_map: dict[tuple[int, str], list[int]] | None,
    missing_row_indexes: list[int] | None,
    arity_mismatch_indexes: list[int] | None,
) -> None:
    print("\n=== Summary ===")
    if schema_mode:
        width = max(len(names_one), len(names_two))
        differing_positions = [
            idx
            for idx in range(width)
            if (names_one[idx] if idx < len(names_one) else None)
            != (names_two[idx] if idx < len(names_two) else None)
        ]
        if differing_positions:
            sample = _format_row_index_samples(differing_positions)
            print(
                f"Schemas differ by position ({len(differing_positions)} of {width} slots): "
                f"{sample}"
            )
        else:
            print("Schemas differ (column name lists mismatch in a way outside slot compare).")
        for idx in differing_positions[:200]:
            c1 = names_one[idx] if idx < len(names_one) else "<missing>"
            c2 = names_two[idx] if idx < len(names_two) else "<missing>"
            print(f"  [{idx}] query_one column: {c1!r}")
            print(f"                query_two: {c2!r}")
        if len(differing_positions) > 200:
            print(f"  … ({len(differing_positions) - 200} more positions not listed)")
        return

    assert field_row_map is not None
    assert missing_row_indexes is not None
    assert arity_mismatch_indexes is not None

    if field_row_map:
        print(
            "Fields whose values differ on at least one row "
            "(all differing rows were scanned — not only printed detail):"
        )
        keys = sorted(field_row_map.keys(), key=lambda t: (t[0], t[1]))
        for j, name in keys:
            rows_where = field_row_map[(j, name)]
            sample = _format_row_index_samples(rows_where)
            print(
                f"  [{j}] {name!r} — mismatched at {len(rows_where)} row(s); "
                f"indexes {sample}"
            )
    else:
        print(
            "No aligned per-column mismatches accumulated "
            "(differences were only row presence or row shape)."
        )

    extras: list[str] = []
    if missing_row_indexes:
        extras.append(
            "row indexes with a missing counterpart row: "
            f"{_format_row_index_samples(missing_row_indexes)}"
        )
    if arity_mismatch_indexes:
        extras.append(
            "row indexes where the two tuples could not align to columns "
            "(length mismatch): "
            f"{_format_row_index_samples(arity_mismatch_indexes)}"
        )
    for line in extras:
        print(line)


def _report_schema_field_by_field(
    names_one: tuple[str, ...],
    names_two: tuple[str, ...],
) -> None:
    width = max(len(names_one), len(names_two))
    print("  Position | query_one column name    | query_two column name")
    for i in range(width):
        c1 = names_one[i] if i < len(names_one) else "<missing column>"
        c2 = names_two[i] if i < len(names_two) else "<missing column>"
        same = "same" if c1 == c2 else "DIFF"
        print(f"  [{i:>5}] | {c1!r:>24} | {c2!r:>24}  ({same})")


def _report_row_field_by_field(
    column_names: tuple[str, ...],
    r1: tuple[object, ...] | None,
    r2: tuple[object, ...] | None,
    *,
    list_matching_fields: bool,
) -> None:
    """Print column-by-column detail for one row pair (or one-sided row)."""
    if r1 is None and r2 is None:
        return

    _print_project_id_context(column_names, r1, r2)

    if r1 is None:
        print("  query_one: <no row at this index>")
        print("  query_two (field-by-field):")
        if r2 is None:
            return
        for j in range(min(len(column_names), len(r2))):
            name = column_names[j]
            print(f"    [{j}] {name}: {_format_cell(r2[j])}")
        return

    if r2 is None:
        print("  query_one (field-by-field):")
        for j in range(min(len(column_names), len(r1))):
            name = column_names[j]
            print(f"    [{j}] {name}: {_format_cell(r1[j])}")
        print("  query_two: <no row at this index>")
        return

    if len(r1) != len(r2) or len(r1) != len(column_names):
        print(
            "  Row arity mismatch (cannot align fields): "
            f"query_one len={len(r1)}  query_two len={len(r2)}  "
            f"column_names len={len(column_names)}"
        )
        print(f"  query_one raw: {_format_row(r1)}")
        print(f"  query_two raw: {_format_row(r2)}")
        return

    matching = 0
    for j, name in enumerate(column_names):
        v1, v2 = r1[j], r2[j]
        if v1 == v2:
            matching += 1
            if list_matching_fields:
                print(f"  [{j}] {name}: match  {_format_cell(v1)}")
            continue
        print(f"  [{j}] {name}: DIFFERS")
        print(f"       query_one: {_format_cell(v1)}")
        print(f"       query_two: {_format_cell(v2)}")
    if matching and not list_matching_fields:
        print(f"  ({matching} column(s) match at this row; only differences shown.)")


def compare_and_report(
    one: QueryOutcome,
    two: QueryOutcome,
    *,
    max_diff_rows: int,
    list_matching_fields: bool,
) -> bool:
    if one.column_names != two.column_names:
        print("Results differ: column metadata does not match.")
        print("Field-by-field (by result column position):")
        _report_schema_field_by_field(one.column_names, two.column_names)
        _print_mismatch_summary(
            schema_mode=True,
            names_one=one.column_names,
            names_two=two.column_names,
            field_row_map=None,
            missing_row_indexes=None,
            arity_mismatch_indexes=None,
        )
        return False

    if len(one.rows) != len(two.rows):
        print(
            "Results differ: row counts do not match "
            f"(query_one={len(one.rows)}, query_two={len(two.rows)})."
        )
    else:
        print(f"Row count: {len(one.rows)} (both queries)")

    mismatch_indexes: list[int] = []
    pair_count = max(len(one.rows), len(two.rows))
    for i in range(pair_count):
        r1 = one.rows[i] if i < len(one.rows) else None
        r2 = two.rows[i] if i < len(two.rows) else None
        if r1 != r2:
            mismatch_indexes.append(i)

    if not mismatch_indexes:
        print("\nResults are identical (column names and rows match in order).")
        return True

    field_rows, missing_row_ix, arity_bad_ix = _collect_aligned_value_mismatch_fields(
        one.column_names,
        rows_one=one.rows,
        rows_two=two.rows,
        mismatch_indexes=mismatch_indexes,
    )

    printed = 0
    for i in mismatch_indexes:
        if printed >= max_diff_rows:
            suppressed = len(mismatch_indexes) - printed
            print(
                f"\n... suppressed {suppressed} more differing row index(es); "
                "use --max-diff-rows to show more."
            )
            break
        r1 = one.rows[i] if i < len(one.rows) else None
        r2 = two.rows[i] if i < len(two.rows) else None
        print(f"\nDifference at row index {i} (0-based):")
        _report_row_field_by_field(
            one.column_names,
            r1,
            r2,
            list_matching_fields=list_matching_fields,
        )
        printed += 1

    print(f"\nDiffering row indexes: {len(mismatch_indexes)} total.")
    _print_mismatch_summary(
        schema_mode=False,
        names_one=(),
        names_two=(),
        field_row_map=field_rows,
        missing_row_indexes=missing_row_ix,
        arity_mismatch_indexes=arity_bad_ix,
    )
    print("\nResults are not identical.")
    return False


def main() -> None:
    here = Path(__file__).resolve().parent
    default_one = here / "query_one.txt"
    default_two = here / "query_two.txt"

    parser = argparse.ArgumentParser(
        description="Run two SQL files and compare result sets.",
    )
    parser.add_argument(
        "--query-one",
        type=Path,
        default=default_one,
        help=f"SQL file for first query (default: {default_one.name}).",
    )
    parser.add_argument(
        "--query-two",
        type=Path,
        default=default_two,
        help=f"SQL file for second query (default: {default_two.name}).",
    )
    parser.add_argument(
        "--max-diff-rows",
        type=int,
        default=20,
        help="Max number of differing rows to print in detail (default: 20).",
    )
    parser.add_argument(
        "--list-matching-fields",
        action="store_true",
        help=(
            "For each differing row, print every column (including matching ones); "
            "default is to print only columns that differ, with a match count summary."
        ),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("PGHOST", "localhost"),
        help="PostgreSQL host (default: localhost or PGHOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PGPORT", "5432")),
        help="PostgreSQL port (default: 5432 or PGPORT).",
    )
    parser.add_argument(
        "--dbname",
        default=os.getenv("PGDATABASE", "vision_zero"),
        help="Database name (default: vision_zero or PGDATABASE).",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("PGUSER", "visionzero"),
        help="Database user (default: visionzero or PGUSER).",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("PGPASSWORD", "visionzero"),
        help="Database password (default: visionzero or PGPASSWORD).",
    )
    parser.add_argument(
        "--pg-options",
        default=os.getenv("PGOPTIONS", ""),
        help=(
            "Connection options for PostgreSQL "
            "(default: PGOPTIONS, or auto-built from PG_RANDOM_PAGE_COST, "
            "PG_EFFECTIVE_IO_CONCURRENCY, PG_DEFAULT_STATISTICS_TARGET, PG_JIT)."
        ),
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=(
            int(os.getenv("PGCONNECT_TIMEOUT", "10"))
            if os.getenv("PGCONNECT_TIMEOUT")
            else None
        ),
        help=(
            "Connection timeout in seconds "
            "(default: PGCONNECT_TIMEOUT if set; otherwise unset)."
        ),
    )
    args = parser.parse_args()

    pg_options = build_pgoptions_from_env(args.pg_options)
    warn_for_startup_only_env_tunables()

    sql_one = args.query_one.read_text()
    sql_two = args.query_two.read_text()

    try:
        one = run_final_result_set(
            sql_one,
            host=args.host,
            port=args.port,
            dbname=args.dbname,
            user=args.user,
            password=args.password,
            options=pg_options,
            connect_timeout=args.connect_timeout,
            label=str(args.query_one),
        )
        two = run_final_result_set(
            sql_two,
            host=args.host,
            port=args.port,
            dbname=args.dbname,
            user=args.user,
            password=args.password,
            options=pg_options,
            connect_timeout=args.connect_timeout,
            label=str(args.query_two),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    identical = compare_and_report(
        one,
        two,
        max_diff_rows=args.max_diff_rows,
        list_matching_fields=args.list_matching_fields,
    )
    raise SystemExit(0 if identical else 1)


if __name__ == "__main__":
    main()
