# RustFS overwrite leak — reproduction

Repeatedly overwriting (re-`PUT`ting) the **same object key** in an
**un-versioned** bucket leaves **one orphaned on-disk data directory per
overwrite**. They are never garbage-collected, so over time:

- the object's backing directory accumulates **N entries** (one per `PUT`),
- a **non-delimited `LIST`** — which must `readdir()` that directory — gets
  slower and slower, eventually exceeding RustFS's 5 s `walk_dir` timeout and
  returning **`500 InternalError: Io error: timeout`**,
- on-disk usage balloons to **many times** the object's logical size.

Reproduced on **`rustfs 1.0.0-beta.7`** (single-drive erasure backend).

## TL;DR run

```bash
./repro.sh            # default: 800 overwrites
./repro.sh 5000       # more overwrites -> slower LIST, bigger bloat
./repro.sh 800 --keep # leave the data dir mounted for inspection
```

The script is self-contained and installs nothing system-wide: it downloads a
pinned `rustfs` release and the `mc` client into `./.bin`, runs a scratch
server on `127.0.0.1:9100`, drives the write pattern, prints the evidence, and
cleans up on exit.

Requirements: `curl`, `unzip`, `tar`, ~a few hundred MB free under this dir,
and outbound access to `github.com` + `dl.min.io` (first run only).

## What you'll see

```
== RESULT
  overwrites issued ............. 300
  logical object size ........... 631KiB (one current object)
  on-disk entries in object dir . 301
  └─ orphaned data directories .. 300   <- should be 1 if GC worked
  on-disk footprint ............. 169M   <- vs the logical size above
  non-delimited LIST ............ HTTP 200 in 0.034285s
```

Each UUID-named data directory holds a full `part.1` copy of that `PUT`'s body:

```
<data>/repro/growing.jsonl/
├── xl.meta                       # current metadata (tiny)
├── 0007182f-…/part.1             # an OLD overwrite, never deleted
├── 009c9d4b-…/part.1
└── … one per overwrite …
```

## The catch that makes it subtle

Objects **below ~128 KiB are stored inline inside `xl.meta`** (no separate data
dir), so overwriting a *small* object does **not** leak — there's nothing to
orphan. The leak only appears once objects exceed the inline threshold and are
stored as real data directories. The repro therefore **seeds the body to 512 KiB**
(`SEED_BYTES`) before the first `PUT`. Run with a small `SEED_BYTES` (e.g.
`SEED_BYTES=4096 ./repro.sh 300`) and you'll see **no leak** — a useful control.

## Why `GET`/`HEAD` stay fast but `LIST` rots

`GET` and `HEAD` read the tiny `xl.meta` (and, for `GET`, the one current
`part.1`) — O(1). `LIST` `readdir()`s the whole object directory, which now has
one entry per historical overwrite — O(overwrites). That's why a bucket whose
objects download at full speed can still time out a bucket listing.

## How this bit us in production

An agent appended events to a transcript `events.jsonl` through an **s3fs**
mount. s3fs has no append, so every flush re-`PUT`s the whole (growing) object.
A long session flushed thousands of times, leaving **8,415 orphaned data dirs**
behind one key; that single "7.5 MB" object occupied **34 GB** on disk, and the
`transcripts/` prefix totalled **47 GB** for ~30 MB of live data. A non-delimited
`LIST` of the bucket (exactly what **s3fs issues at mount time** to validate the
bucket) exceeded the 5 s `walk_dir` timeout → `500`, so the mount never came up.

## Suggested fixes

- **RustFS:** reclaim the previous object's data directory on overwrite in an
  un-versioned bucket (or have the scanner GC orphaned data dirs). This is a
  disk-exhaustion risk independent of the listing symptom.
- **Workaround for callers:** don't repeatedly overwrite one growing object
  (e.g. via s3fs append). Write immutable, separate objects, or keep such
  buckets out of the s3fs mount path.

## Files

- `repro.sh` — the whole reproduction (download → serve → write pattern → report).
- `.bin/`, `.rustfs-data/`, `.rustfs.log` — generated; git-ignored.
