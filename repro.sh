#!/usr/bin/env bash
#
# Reproduce the RustFS "overwrite leak": repeatedly overwriting (re-PUT) the
# SAME object key in an UN-versioned bucket leaves one orphaned on-disk data
# directory PER overwrite. They are never garbage-collected, so:
#
#   * the object's backing directory grows to N entries (one per PUT),
#   * a non-delimited LIST that has to readdir() that directory gets slower
#     and slower (eventually tripping RustFS's 5s walk_dir timeout -> HTTP 500),
#   * on-disk usage balloons to many times the object's logical size.
#
# This is exactly the production pattern: an agent appends to a transcript
# .jsonl through an s3fs mount; each flush re-PUTs the whole (growing) object,
# so a long session racks up thousands of stale data dirs behind one key.
#
# The script is self-contained: it downloads a pinned RustFS release + the mc
# client into ./.bin, runs a single-drive server on a scratch dir, drives the
# write pattern, and prints the on-disk + LIST-latency evidence. Nothing is
# installed system-wide; everything lives under this directory and is cleaned
# up on exit (pass --keep to inspect the data dir afterwards).
#
# Usage:   ./repro.sh [N_OVERWRITES] [--keep]
#   N_OVERWRITES  how many times to re-PUT the key (default 800)
#   --keep        don't delete the scratch data dir / leave server stopped
#
set -euo pipefail

# ----- config -------------------------------------------------------------
RUSTFS_VERSION="${RUSTFS_VERSION:-1.0.0-beta.7}"   # pin: the version we observed the bug on
OS="$(uname -s)"                                    # Linux / Darwin
ARCH="$(uname -m)"                                  # x86_64 / aarch64 / arm64
# normalize: macOS reports Apple Silicon as "arm64", Linux as "aarch64"
[ "$ARCH" = "arm64" ] && ARCH="aarch64"
PORT="${PORT:-9100}"
AK=rustfsadmin ; SK=rustfsadmin
BUCKET=repro
KEY=growing.jsonl
LINE_BYTES="${LINE_BYTES:-400}"                     # size of each appended "event" line
# RustFS (like MinIO) stores objects below a ~128 KiB threshold INLINE inside
# xl.meta -- those have no separate data dir, so overwriting them can't leak
# one. The leak only bites objects stored as a real data dir (part.1), i.e.
# above the inline threshold. Production's transcripts were multi-MB, so we
# seed the body well above the threshold before the first PUT.
SEED_BYTES="${SEED_BYTES:-524288}"                  # 512 KiB starting size (> inline threshold)

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/.bin"
DATA="$HERE/.rustfs-data"
ENDPOINT="http://127.0.0.1:$PORT"
EMPTY_SHA=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  # sha256("")

N="${1:-800}"
[ "${1:-}" = "--keep" ] && N=800
KEEP=0; for a in "$@"; do [ "$a" = "--keep" ] && KEEP=1; done

mkdir -p "$BIN"
RUSTFS="$BIN/rustfs"
MC="$BIN/mc"
SRV_PID=""

cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
  if [ "$KEEP" = "0" ]; then rm -rf "$DATA"; fi
}
trap cleanup EXIT

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

# ----- 0. fetch binaries --------------------------------------------------
if [ ! -x "$RUSTFS" ]; then
  say "downloading rustfs $RUSTFS_VERSION ($OS/$ARCH)"
  case "$OS/$ARCH" in
    Linux/x86_64)   rurl="rustfs-linux-x86_64-musl-v${RUSTFS_VERSION}.zip" ;;
    Linux/aarch64)  rurl="rustfs-linux-aarch64-musl-v${RUSTFS_VERSION}.zip" ;;
    Darwin/x86_64)  rurl="rustfs-macos-x86_64-v${RUSTFS_VERSION}.zip" ;;
    Darwin/aarch64) rurl="rustfs-macos-aarch64-v${RUSTFS_VERSION}.zip" ;;
    *) echo "unsupported platform: $OS/$ARCH" >&2; exit 1 ;;
  esac
  curl -fsSL "https://github.com/rustfs/rustfs/releases/download/${RUSTFS_VERSION}/${rurl}" -o "$BIN/rustfs.zip"
  unzip -o -q "$BIN/rustfs.zip" -d "$BIN"
  # the zip contains a `rustfs` binary (possibly nested); normalize the path
  found="$(find "$BIN" -type f -name rustfs | head -1)"
  [ "$found" != "$RUSTFS" ] && mv -f "$found" "$RUSTFS"
  chmod +x "$RUSTFS"; rm -f "$BIN/rustfs.zip"
fi
if [ ! -x "$MC" ]; then
  say "downloading mc client"
  case "$OS/$ARCH" in
    Linux/x86_64)   murl=linux-amd64 ;;
    Linux/aarch64)  murl=linux-arm64 ;;
    Darwin/x86_64)  murl=darwin-amd64 ;;
    Darwin/aarch64) murl=darwin-arm64 ;;
    *) echo "unsupported platform: $OS/$ARCH" >&2; exit 1 ;;
  esac
  curl -fsSL "https://dl.min.io/client/mc/release/${murl}/mc" -o "$MC"
  chmod +x "$MC"
fi
"$RUSTFS" --version 2>/dev/null | head -1 || true

# ----- 1. start a fresh single-drive server -------------------------------
say "starting rustfs on $ENDPOINT (data dir: $DATA)"
rm -rf "$DATA"; mkdir -p "$DATA"
RUSTFS_ACCESS_KEY="$AK" RUSTFS_SECRET_KEY="$SK" \
  "$RUSTFS" server "$DATA" --address ":$PORT" >"$HERE/.rustfs.log" 2>&1 &
SRV_PID=$!

# wait for the S3 port (any HTTP response, incl. 403, means it's up)
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$ENDPOINT/" 2>/dev/null || true)"
  [ "$code" != "000" ] && [ -n "$code" ] && break
  kill -0 "$SRV_PID" 2>/dev/null || { echo "rustfs died on startup; see .rustfs.log" >&2; tail -20 "$HERE/.rustfs.log" >&2; exit 1; }
  sleep 0.5
done
"$MC" alias set repro "$ENDPOINT" "$AK" "$SK" >/dev/null
"$MC" mb -p repro/$BUCKET >/dev/null
"$MC" version info repro/$BUCKET 2>/dev/null | sed 's/^/  /' || true   # show it's UN-versioned

OBJDIR="$DATA/$BUCKET/$KEY"

# helper: time a non-delimited LIST of the bucket (the call s3fs makes at mount)
list_ms() {
  curl -s -o /dev/null \
    --aws-sigv4 "aws:amz:us-east-1:s3" --user "$AK:$SK" \
    -H "x-amz-content-sha256: $EMPTY_SHA" \
    -w '%{http_code} %{time_total}' \
    "$ENDPOINT/$BUCKET/?list-type=2"
}

# ----- 2. drive the overwrite pattern -------------------------------------
say "re-PUTting one key '$KEY' $N times (append-then-overwrite, like s3fs flushes)"
body="$HERE/.body.tmp"; : > "$body"
line="$(head -c "$LINE_BYTES" < /dev/zero | tr '\0' 'x')"
# seed the file above the inline threshold so every PUT lands as a real data dir
seed_lines=$(( SEED_BYTES / (LINE_BYTES + 24) + 1 ))
for s in $(seq 1 "$seed_lines"); do printf '{"seed":%d,"d":"%s"}\n' "$s" "$line" >> "$body"; done
printf '  seeded body to %s before first PUT\n' "$(du -h "$body" | awk '{print $1}')"
printf '  progress: '
for i in $(seq 1 "$N"); do
  printf '{"i":%d,"event":"%s"}\n' "$i" "$line" >> "$body"   # the file GROWS each turn
  "$MC" cp -q "$body" "repro/$BUCKET/$KEY" >/dev/null         # ...and we overwrite the same key
  if [ $((i % 100)) -eq 0 ]; then
    read -r code t <<<"$(list_ms)"
    printf '%d(list=%ss) ' "$i" "$t"
  fi
done
printf '\n'
rm -f "$body"

# ----- 3. evidence --------------------------------------------------------
say "RESULT"
logical_sz="$("$MC" ls repro/$BUCKET/$KEY 2>/dev/null | awk '{print $(NF-1), $(NF-2)}')"
ondisk_entries="$(ls -1 "$OBJDIR" 2>/dev/null | wc -l)"
data_dirs="$(find "$OBJDIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)"
ondisk_du="$(du -sh "$OBJDIR" 2>/dev/null | awk '{print $1}')"
read -r fcode ft <<<"$(list_ms)"

printf '  overwrites issued ............. %s\n' "$N"
printf '  logical object size ........... %s (one current object)\n' "${logical_sz:-?}"
printf '  on-disk entries in object dir . %s\n' "$ondisk_entries"
printf '  └─ orphaned data directories .. %s   <- should be 1 if GC worked\n' "$data_dirs"
printf '  on-disk footprint ............. %s   <- vs the logical size above\n' "$ondisk_du"
printf '  non-delimited LIST ............ HTTP %s in %ss\n' "$fcode" "$ft"
echo
echo "  one object dir, abbreviated:"
ls -1 "$OBJDIR" 2>/dev/null | head -4 | sed 's/^/    /'
echo "    ... ($ondisk_entries entries total)"
echo "  a sample data dir holds a full copy of that PUT's body:"
sampled="$(find "$OBJDIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -n "$sampled" ] && ls -la "$sampled" | sed 's/^/    /'

echo
if [ "$data_dirs" -gt 1 ]; then
  printf '\033[1;31m  BUG REPRODUCED:\033[0m %s overwrites of one key left %s data dirs (expected 1).\n' "$N" "$data_dirs"
  printf '  Each overwrite leaked its predecessor; LIST cost and disk use scale with overwrite count.\n'
else
  printf '\033[1;32m  No leak observed:\033[0m object dir has a single data dir (GC working in this build).\n'
fi
[ "$KEEP" = "1" ] && echo && echo "  (--keep) data dir left at: $DATA"
