#!/usr/bin/env python3
"""Run a timed SQL benchmark directly via Python/PostgreSQL."""

from __future__ import annotations

import argparse
import curses
import logging
import os
import sys
from datetime import datetime
from time import perf_counter
from dataclasses import dataclass
from pathlib import Path


CACHE_STATS_SQL = """
SELECT
    sum(heap_blks_read) as heap_read,
    sum(heap_blks_hit)  as heap_hit,
    (sum(heap_blks_hit) / (sum(heap_blks_hit) + sum(heap_blks_read))) * 100 as hit_ratio
FROM pg_statio_user_tables;
"""

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


@dataclass
class CacheStats:
    heap_read: int
    heap_hit: int
    hit_ratio: float | None


@dataclass
class BenchmarkResult:
    execution_time_ms: float
    row_count: int
    cache_stats: CacheStats


logger = logging.getLogger(__name__)


def configure_logging(level_name: str) -> None:
    normalized = level_name.strip().upper()
    if normalized in {"NONE", "OFF", "DISABLE", "DISABLED"}:
        level = logging.CRITICAL + 1
    else:
        level = getattr(logging, normalized, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    logger.info("Logging initialized at level=%s", logging.getLevelName(level))


def run_benchmark(
    sql_text: str,
    *,
    host: str = "localhost",
    port: int = 5432,
    dbname: str = "vision_zero",
    user: str = "visionzero",
    password: str = "visionzero",
    options: str | None = None,
    connect_timeout: int | None = None,
) -> BenchmarkResult:
    logger.info(
        "Starting benchmark: host=%s port=%s dbname=%s user=%s sql_chars=%s",
        host,
        port,
        dbname,
        user,
        len(sql_text),
    )
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for direct Python DB benchmarking. "
            "Install it with: pip install psycopg[binary]"
        ) from exc

    logger.info("Opening PostgreSQL connection")
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
        logger.info("Using connect_timeout=%ss", connect_timeout)

    with psycopg.connect(
        **connect_kwargs,
    ) as conn:
        logger.info("PostgreSQL connection established")
        with conn.cursor() as cur:
            logger.info("Executing benchmark SQL")
            start = perf_counter()
            cur.execute(sql_text)
            logger.info("SQL execution finished, fetching rows")
            rows = cur.fetchall()
            end = perf_counter()
            logger.info("Fetched %s rows, collecting cache stats", len(rows))
            cur.execute(CACHE_STATS_SQL)
            cache_row = cur.fetchone()
            logger.info("Cache stats query complete")

    if cache_row is None:
        cache_stats = CacheStats(heap_read=0, heap_hit=0, hit_ratio=None)
    else:
        heap_read = int(cache_row[0] or 0)
        heap_hit = int(cache_row[1] or 0)
        hit_ratio_raw = cache_row[2]
        hit_ratio = float(hit_ratio_raw) if hit_ratio_raw is not None else None
        cache_stats = CacheStats(
            heap_read=heap_read,
            heap_hit=heap_hit,
            hit_ratio=hit_ratio,
        )

    return BenchmarkResult(
        execution_time_ms=(end - start) * 1000.0,
        row_count=len(rows),
        cache_stats=cache_stats,
    )


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    fraction = index - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def add_line(stdscr: curses.window, y: int, text: str) -> None:
    height, width = stdscr.getmaxyx()
    if 0 <= y < height:
        stdscr.addnstr(y, 0, text, max(1, width - 1))


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
            "Note: startup-only PostgreSQL tunables are configured via docker-compose "
            f"(not per benchmark session): {', '.join(ignored)}",
            file=sys.stderr,
        )


def run_loop_ui(args: argparse.Namespace, sql_statements: list[str]) -> None:
    interval_seconds = args.interval_seconds
    total_queries = len(sql_statements)

    def _loop(stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

        entries: list[str] = []
        timings_ms: list[float] = []
        failures = 0
        run_count = 0
        scroll_offset = 0
        follow_tail = True
        latest_cache_stats = CacheStats(heap_read=0, heap_hit=0, hit_ratio=None)
        last_query_index: int | None = None
        started = perf_counter()
        next_run_at = perf_counter()

        while True:
            now = perf_counter()
            if now >= next_run_at:
                run_count += 1
                query_index = (run_count - 1) % total_queries
                sql_text = sql_statements[query_index]
                timestamp = datetime.now().strftime("%H:%M:%S")
                logger.info(
                    "Loop iteration start: run=%s query=%s/%s",
                    run_count,
                    query_index + 1,
                    total_queries,
                )
                try:
                    result = run_benchmark(
                        sql_text,
                        host=args.host,
                        port=args.port,
                        dbname=args.dbname,
                        user=args.user,
                        password=args.password,
                        options=args.pg_options,
                        connect_timeout=args.connect_timeout,
                    )
                    timings_ms.append(result.execution_time_ms)
                    latest_cache_stats = result.cache_stats
                    last_query_index = query_index
                    logger.info(
                        "Loop iteration success: run=%s duration_ms=%.3f rows=%s",
                        run_count,
                        result.execution_time_ms,
                        result.row_count,
                    )
                    entries.append(
                        f"{run_count:05d} {timestamp}  {result.execution_time_ms:10.3f} ms  "
                        f"rows={result.row_count}  query={query_index + 1}/{total_queries}"
                    )
                except Exception as exc:  # noqa: BLE001 - keep loop alive while benchmarking
                    failures += 1
                    last_query_index = query_index
                    logger.exception("Loop iteration failed: run=%s", run_count)
                    entries.append(
                        f"{run_count:05d} {timestamp}  ERROR {exc.__class__.__name__}: {exc}  "
                        f"query={query_index + 1}/{total_queries}"
                    )
                next_run_at = now + interval_seconds

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            stats_height = 10
            history_top = 1
            history_height = max(3, height - stats_height - history_top)
            max_scroll = max(0, len(entries) - history_height)

            if follow_tail:
                scroll_offset = max_scroll
            else:
                scroll_offset = min(scroll_offset, max_scroll)

            add_line(
                stdscr,
                0,
                "DB Benchmark Loop  |  q quit  up/down scroll  PgUp/PgDn page  Home/End jump",
            )
            for row in range(history_height):
                entry_index = scroll_offset + row
                if entry_index >= len(entries):
                    break
                add_line(stdscr, history_top + row, entries[entry_index])

            divider_y = history_top + history_height
            add_line(stdscr, divider_y, "-" * max(1, width - 1))

            elapsed_seconds = max(0.0, perf_counter() - started)
            next_in = max(0.0, next_run_at - perf_counter())
            success_count = len(timings_ms)
            mean_ms = (sum(timings_ms) / success_count) if success_count else 0.0
            min_ms = min(timings_ms) if success_count else 0.0
            max_ms = max(timings_ms) if success_count else 0.0
            p50_ms = percentile(timings_ms, 0.50)
            p95_ms = percentile(timings_ms, 0.95)
            cache_hit_ratio = (
                f"{latest_cache_stats.hit_ratio:.2f}%"
                if latest_cache_stats.hit_ratio is not None
                else "n/a"
            )

            stats_lines = [
                f"Runs: {run_count}  Success: {success_count}  Failures: {failures}  Interval: {interval_seconds:.0f}s  Next: {next_in:.1f}s",
                f"Queries loaded: {total_queries}  Last query: {(last_query_index + 1) if last_query_index is not None else 'n/a'}",
                f"Mean: {mean_ms:.3f} ms  Min: {min_ms:.3f} ms  Max: {max_ms:.3f} ms",
                f"P50: {p50_ms:.3f} ms  P95: {p95_ms:.3f} ms",
                f"Cache: heap_read={latest_cache_stats.heap_read}  heap_hit={latest_cache_stats.heap_hit}  hit_ratio={cache_hit_ratio}",
                f"Elapsed: {elapsed_seconds:.1f}s  Showing {scroll_offset + 1 if entries else 0}-{min(len(entries), scroll_offset + history_height)} of {len(entries)}",
            ]

            for i, line in enumerate(stats_lines, start=divider_y + 1):
                add_line(stdscr, i, line)

            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                break
            if key == curses.KEY_UP:
                follow_tail = False
                scroll_offset = max(0, scroll_offset - 1)
            elif key == curses.KEY_DOWN:
                scroll_offset = min(max_scroll, scroll_offset + 1)
                follow_tail = scroll_offset >= max_scroll
            elif key == curses.KEY_PPAGE:
                follow_tail = False
                scroll_offset = max(0, scroll_offset - history_height)
            elif key == curses.KEY_NPAGE:
                scroll_offset = min(max_scroll, scroll_offset + history_height)
                follow_tail = scroll_offset >= max_scroll
            elif key == curses.KEY_HOME:
                follow_tail = False
                scroll_offset = 0
            elif key == curses.KEY_END:
                follow_tail = True
                scroll_offset = max_scroll

    curses.wrapper(_loop)


def main() -> None:
    # This script is intended to run from `toolbox/db_benchmark/`.
    default_sql_file = Path("timing_queries.sql")

    parser = argparse.ArgumentParser(
        description="Benchmark SQL execution time using a direct Python DB connection."
    )
    parser.add_argument(
        "--sql-file",
        type=Path,
        default=default_sql_file,
        help="Path to SQL file to run (default: timing_queries.sql).",
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
            "Connection options passed through to PostgreSQL "
            "(default: PGOPTIONS, or auto-built from PG_RANDOM_PAGE_COST, "
            "PG_EFFECTIVE_IO_CONCURRENCY, PG_DEFAULT_STATISTICS_TARGET, "
            "PG_JIT)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("BENCHMARK_LOG_LEVEL", "NONE"),
        help="Python logging level (default: BENCHMARK_LOG_LEVEL or NONE).",
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
            "PostgreSQL connection timeout in seconds "
            "(default: PGCONNECT_TIMEOUT if set; otherwise no explicit timeout)."
        ),
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="Delay between loop iterations in seconds (default: 1.0).",
    )
    args = parser.parse_args()
    configure_logging(args.log_level)
    logger.info("Benchmark script started")
    logger.info("Using SQL file: %s", args.sql_file)
    args.pg_options = build_pgoptions_from_env(args.pg_options)
    logger.info("Effective pg_options: %s", args.pg_options if args.pg_options else "<none>")
    warn_for_startup_only_env_tunables()

    if args.interval_seconds <= 0:
        print("--interval-seconds must be greater than 0.", file=sys.stderr)
        raise SystemExit(1)

    logger.info("Reading SQL text from file")
    sql_text = args.sql_file.read_text()
    sql_statements = parse_sql_statements(sql_text)
    logger.info("Loaded %s SQL statement(s)", len(sql_statements))
    if not sql_statements:
        print(f"No SQL statements found in {args.sql_file}.", file=sys.stderr)
        raise SystemExit(1)

    logger.info("Entering loop mode")
    if not sys.stdout.isatty():
        print("This benchmark requires an interactive TTY terminal.", file=sys.stderr)
        raise SystemExit(1)
    run_loop_ui(args, sql_statements)
    logger.info("Loop mode exited")


if __name__ == "__main__":
    main()
