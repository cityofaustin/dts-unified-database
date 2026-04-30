# DB Benchmark Helper

This tool runs `benchmark_db.py` in Docker and continuously executes the SQL statements in `timing_queries.sql` in a loop.

## Quick start

From `toolbox/db_benchmark/`:

```shell
docker compose run --rm benchmark-db
```

The benchmark runs in an interactive terminal UI and cycles through all loaded SQL statements. Press `q` to quit.

## Targeting a specific Postgres instance

The benchmark container reads standard `PG*` environment variables. You can override them at runtime via the environment or via argument flags. Via the environment, a custom invocation might look like this:

```shell
PGHOST=host.docker.internal PGPORT=5431 PGDATABASE=vision_zero PGUSER=visionzero PGPASSWORD=visionzero docker compose run --rm benchmark-db
```

## Tuning Postgres for local benchmarking

The main local Postgres container (`docker-compose.yml`) supports these tunables through `.env`:

- `PG_MAINTENANCE_WORK_MEM`
- `PG_MAX_WAL_SIZE`
- `PG_SHARED_BUFFERS`
- `PG_WORK_MEM`
- `PG_EFFECTIVE_CACHE_SIZE`
- `PG_CHECKPOINT_COMPLETION_TARGET`
- `PG_RANDOM_PAGE_COST`
- `PG_EFFECTIVE_IO_CONCURRENCY`
- `PG_MAX_CONNECTIONS`
- `PG_DEFAULT_STATISTICS_TARGET`
- `PG_JIT`
- `PG_WAL_COMPRESSION`

### What each parameter controls

#### Note: All defaults are calculated for an RDS machine with 32 GB of RAM.

- `PG_MAINTENANCE_WORK_MEM` sets how much memory Postgres can use for maintenance operations like `VACUUM`, `CREATE INDEX`, and `ALTER TABLE` tasks (default: `GREATEST({DBInstanceClassMemory*1024/63963136},65536)` or in our case `537 MB`). Higher values can speed up those operations, especially index creation on large tables. If set too high on a busy system, concurrent maintenance work can consume too much RAM.
- `PG_MAX_WAL_SIZE` sets the soft upper bound on how much WAL data can accumulate before checkpoints are forced (default: `2GB`). A larger value usually means fewer checkpoints and less checkpoint-related write pressure during heavy write workloads. The tradeoff is more WAL disk usage and potentially longer crash recovery.
- `PG_SHARED_BUFFERS` sets the size of Postgres' primary in-memory cache for table and index pages (default: `64MB`). Increasing it can reduce disk reads when the working set fits in memory. It must fit available memory and shared memory limits, or startup/performance issues can occur.
- `PG_WORK_MEM` sets memory available per sort, hash, and similar query operation before Postgres spills to disk (no RDS default value). Higher values can make complex queries faster by avoiding temporary disk files. Because this applies per operation and per connection, overly large values can cause memory pressure under concurrency.
- `PG_EFFECTIVE_CACHE_SIZE` is a planner hint estimating how much data is likely cached by Postgres plus the OS page cache (default: `2GB`). It does not allocate memory directly and only influences query planning decisions. Setting it too low can bias plans toward sequential scans, while setting it too high can over-favor index usage.
- `PG_CHECKPOINT_COMPLETION_TARGET` controls how aggressively checkpoint writes are spread across each checkpoint interval (default: `0.9`). Higher values smooth writes over more time, reducing I/O spikes and latency jitter. If set too high or too low for the workload, write performance can still become uneven.
- `PG_RANDOM_PAGE_COST` tells the planner how expensive random I/O is relative to sequential I/O (no RDS default value). Lower values make index scans more attractive, which can help on SSDs and well-cached datasets. If set unrealistically low, the planner may choose indexes when sequential scans would be faster.
- `PG_EFFECTIVE_IO_CONCURRENCY` tells Postgres how many concurrent disk I/O requests it can expect, mainly affecting bitmap heap scan prefetch behavior (no RDS default value). Higher values can improve read throughput on storage that handles parallel I/O well. On slower or constrained storage, very high values may provide little benefit.
- `PG_MAX_CONNECTIONS` sets the maximum number of simultaneous client sessions allowed (default: `3604`). Higher limits accommodate more direct connections but increase memory overhead and scheduling contention. For high concurrency, connection pooling is often more efficient than continually raising this setting.
- `PG_DEFAULT_STATISTICS_TARGET` sets the default amount of column statistics collected by `ANALYZE` for planner estimates (no RDS default value). Higher targets improve cardinality estimation accuracy for complex predicates and skewed data. The tradeoff is longer analyze time and larger statistics storage.
- `PG_JIT` enables or disables PostgreSQL's just-in-time compilation for parts of query execution (default: `off`). JIT can speed up some long-running CPU-heavy queries after compilation overhead is paid. For short queries or OLTP-style traffic, disabling JIT can reduce per-query overhead and improve latency consistency.
- `PG_WAL_COMPRESSION` enables compression of full-page images written to WAL (default: `on` using `zstd`). This can reduce WAL volume and write amplification, which may help write-heavy workloads or slower disks. It uses additional CPU, so net benefit depends on workload and hardware balance.

After editing `.env`, restart the DB:

For Vision Zero:

```shell
./vision-zero db-down
./vision-zero db-up
```

Then run the benchmark against it.

To verify the effective settings in Postgres:

```sql
SHOW maintenance_work_mem;
SHOW max_wal_size;
SHOW shared_buffers;
SHOW work_mem;
SHOW effective_cache_size;
SHOW checkpoint_completion_target;
SHOW random_page_cost;
SHOW effective_io_concurrency;
SHOW max_connections;
SHOW default_statistics_target;
SHOW jit;
SHOW wal_compression;
```
