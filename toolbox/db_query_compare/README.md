# db_query_compare

Compares the final result sets produced by two SQL files against the same PostgreSQL database.

The tool runs statements in each file in order, and compares only the **final statement** result set:
- column names (in order)
- row count
- row values (in order)

Exit codes:
- `0`: results are identical
- `1`: results differ or execution failed

## 🤖 LLM usage

This application was created using Cursor's Composer model. It was created as a helper tool to assist in
the recent re-writing of a view's query code in another City of Austin, DTS project.

## Files

- `compare_queries.py` - main script
- `query_one.txt` - default SQL input for query one
- `query_two.txt` - default SQL input for query two
- `requirements.txt` - Python dependency (`psycopg[binary]`)

## Setup

From `toolbox/db_query_compare`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Basic usage

Run from this directory:

```bash
python3 compare_queries.py
```

By default, it reads:
- `query_one.txt`
- `query_two.txt`

## Use custom SQL files

```bash
python3 compare_queries.py --query-one /path/to/query_a.sql --query-two /path/to/query_b.sql
```

## Connection settings

You can configure DB connection via CLI args or PostgreSQL env vars:

- `--host` / `PGHOST` (default `localhost`)
- `--port` / `PGPORT` (default `5432`)
- `--dbname` / `PGDATABASE` (default `vision_zero`)
- `--user` / `PGUSER` (default `visionzero`)
- `--password` / `PGPASSWORD` (default `visionzero`)
- `--pg-options` / `PGOPTIONS`
- `--connect-timeout` / `PGCONNECT_TIMEOUT`

Example:

```bash
PGHOST=localhost PGPORT=5432 PGDATABASE=my_db PGUSER=my_user PGPASSWORD=my_pass \
python3 compare_queries.py
```

## Comparison output controls

- `--max-diff-rows N` - max differing row indexes to print in detail (default `20`)
- `--list-matching-fields` - for differing rows, print all fields (not just mismatches)

Example:

```bash
python3 compare_queries.py --max-diff-rows 100 --list-matching-fields
```

## SQL file expectations

- Each file may contain multiple SQL statements separated by `;`.
- Non-final statements are executed for setup.
- The final statement in each file must return a rowset (for example `SELECT ...`).
- If final statements return the same data but in different order, rows will be reported as different.
