#!/usr/bin/env python3
"""
Reproduce the RustFS "overwrite leak": repeatedly overwriting (re-PUT) the
SAME object key in an UN-versioned bucket leaves one orphaned on-disk data
directory PER overwrite. They are never garbage-collected, so:

  * the object's backing directory grows to N entries (one per PUT),
  * a non-delimited LIST that has to readdir() that directory gets slower
    and slower (eventually tripping RustFS's 5s walk_dir timeout -> HTTP 500),
  * on-disk usage balloons to many times the object's logical size.

This is exactly the production pattern: an agent appends to a transcript
.jsonl through an s3fs mount; each flush re-PUTs the whole (growing) object,
so a long session racks up thousands of stale data dirs behind one key.

The script is self-contained: it downloads a pinned RustFS release + the mc
client into ./.bin, runs a single-drive server on a scratch dir, drives the
write pattern, and prints the on-disk + LIST-latency evidence. Nothing is
installed system-wide; everything lives under this directory and is cleaned
up on exit (pass --keep to inspect the data dir afterwards).

Usage:   ./repro.py [N_OVERWRITES] [--keep]
  N_OVERWRITES  how many times to re-PUT the key (default 800)
  --keep        don't delete the scratch data dir / leave server stopped
"""

import atexit
import datetime
import hashlib
import hmac
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# ----- config -------------------------------------------------------------
RUSTFS_VERSION = os.environ.get("RUSTFS_VERSION", "1.0.0-beta.7")  # pin: the version we observed the bug on
PORT = int(os.environ.get("PORT", "9100"))
AK = SK = "rustfsadmin"
BUCKET = "repro"
KEY = "growing.jsonl"
LINE_BYTES = int(os.environ.get("LINE_BYTES", "400"))             # size of each appended "event" line
# RustFS (like MinIO) stores objects below a ~128 KiB threshold INLINE inside
# xl.meta -- those have no separate data dir, so overwriting them can't leak
# one. The leak only bites objects stored as a real data dir (part.1), i.e.
# above the inline threshold. Production's transcripts were multi-MB, so we
# seed the body well above the threshold before the first PUT.
SEED_BYTES = int(os.environ.get("SEED_BYTES", "524288"))          # 512 KiB starting size (> inline threshold)

OS_NAME = platform.system()                                       # Linux / Darwin
ARCH = platform.machine()                                         # x86_64 / aarch64 / arm64
if ARCH == "arm64":                                               # macOS reports Apple Silicon as arm64
    ARCH = "aarch64"

HERE = Path(__file__).resolve().parent
BIN = HERE / ".bin"
DATA = HERE / ".rustfs-data"
ENDPOINT = f"http://127.0.0.1:{PORT}"
REGION, SERVICE = "us-east-1", "s3"
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # sha256("")

RUSTFS = BIN / "rustfs"
MC = BIN / "mc"
OBJDIR = DATA / BUCKET / KEY

# ----- args ---------------------------------------------------------------
KEEP = "--keep" in sys.argv[1:]
N = 800
for a in sys.argv[1:]:
    if a != "--keep":
        N = int(a)
        break

srv = None  # the rustfs server Popen handle


def say(msg):
    print(f"\n\033[1;36m== {msg}\033[0m")


def cleanup():
    if srv is not None:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
    if not KEEP and DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)


atexit.register(cleanup)


def make_executable(p: Path):
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def mc(*args, capture=False):
    """Run the bundled mc client."""
    return subprocess.run(
        [str(MC), *args],
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


# AWS SigV4-signed GET, returning (http_status, seconds). Mirrors the curl
# --aws-sigv4 call the bash version used to time a non-delimited LIST.
def signed_get(path, query):
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    host = f"127.0.0.1:{PORT}"

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{EMPTY_SHA}\n"
        f"x-amz-date:{amzdate}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        ["GET", path, query, canonical_headers, signed_headers, EMPTY_SHA]
    )

    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amzdate,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + SK).encode(), datestamp)
    k_region = _hmac(k_date, REGION)
    k_service = _hmac(k_region, SERVICE)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={AK}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    req = urllib.request.Request(
        f"{ENDPOINT}{path}?{query}",
        headers={
            "x-amz-date": amzdate,
            "x-amz-content-sha256": EMPTY_SHA,
            "Authorization": authorization,
        },
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        e.read()
        code = e.code
    return code, time.perf_counter() - start


def list_bucket():
    return signed_get(f"/{BUCKET}/", "list-type=2")


# ----- 0. fetch binaries --------------------------------------------------
def fetch_binaries():
    BIN.mkdir(parents=True, exist_ok=True)

    if not RUSTFS.exists():
        say(f"downloading rustfs {RUSTFS_VERSION} ({OS_NAME}/{ARCH})")
        rustfs_assets = {
            ("Linux", "x86_64"): f"rustfs-linux-x86_64-musl-v{RUSTFS_VERSION}.zip",
            ("Linux", "aarch64"): f"rustfs-linux-aarch64-musl-v{RUSTFS_VERSION}.zip",
            ("Darwin", "x86_64"): f"rustfs-macos-x86_64-v{RUSTFS_VERSION}.zip",
            ("Darwin", "aarch64"): f"rustfs-macos-aarch64-v{RUSTFS_VERSION}.zip",
        }
        asset = rustfs_assets.get((OS_NAME, ARCH))
        if asset is None:
            sys.exit(f"unsupported platform: {OS_NAME}/{ARCH}")
        url = f"https://github.com/rustfs/rustfs/releases/download/{RUSTFS_VERSION}/{asset}"
        zip_path = BIN / "rustfs.zip"
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(BIN)
        zip_path.unlink()
        # the zip may contain a (possibly nested) `rustfs` binary; normalize the path
        found = next(p for p in BIN.rglob("rustfs") if p.is_file())
        if found != RUSTFS:
            found.replace(RUSTFS)
        make_executable(RUSTFS)

    if not MC.exists():
        say("downloading mc client")
        mc_platform = {
            ("Linux", "x86_64"): "linux-amd64",
            ("Linux", "aarch64"): "linux-arm64",
            ("Darwin", "x86_64"): "darwin-amd64",
            ("Darwin", "aarch64"): "darwin-arm64",
        }.get((OS_NAME, ARCH))
        if mc_platform is None:
            sys.exit(f"unsupported platform: {OS_NAME}/{ARCH}")
        urllib.request.urlretrieve(
            f"https://dl.min.io/client/mc/release/{mc_platform}/mc", MC
        )
        make_executable(MC)

    ver = subprocess.run(
        [str(RUSTFS), "--version"], capture_output=True, text=True
    ).stdout.splitlines()
    if ver:
        print(ver[0])


# ----- 1. start a fresh single-drive server -------------------------------
def start_server():
    global srv
    say(f"starting rustfs on {ENDPOINT} (data dir: {DATA})")
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)

    env = {**os.environ, "RUSTFS_ACCESS_KEY": AK, "RUSTFS_SECRET_KEY": SK}
    log = open(HERE / ".rustfs.log", "w")
    srv = subprocess.Popen(
        [str(RUSTFS), "server", str(DATA), "--address", f":{PORT}"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    # wait for the S3 port (any HTTP response, incl. 403, means it's up)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{ENDPOINT}/", timeout=2).read()
            break
        except urllib.error.HTTPError:
            break  # got an HTTP status -> server is up
        except urllib.error.URLError:
            pass   # connection refused -> not up yet
        if srv.poll() is not None:
            tail = (HERE / ".rustfs.log").read_text().splitlines()[-20:]
            sys.exit("rustfs died on startup; see .rustfs.log\n" + "\n".join(tail))
        time.sleep(0.5)

    mc("alias", "set", "repro", ENDPOINT, AK, SK)
    mc("mb", "-p", f"repro/{BUCKET}")
    info = mc("version", "info", f"repro/{BUCKET}", capture=True).stdout.strip()
    if info:
        print("\n".join("  " + ln for ln in info.splitlines()))  # show it's UN-versioned


# ----- 2. drive the overwrite pattern -------------------------------------
def drive():
    say(f"re-PUTting one key '{KEY}' {N} times (append-then-overwrite, like s3fs flushes)")
    body = HERE / ".body.tmp"
    line = "x" * LINE_BYTES

    with open(body, "w") as f:
        # seed the file above the inline threshold so every PUT lands as a real data dir
        seed_lines = SEED_BYTES // (LINE_BYTES + 24) + 1
        for s in range(1, seed_lines + 1):
            f.write(f'{{"seed":{s},"d":"{line}"}}\n')
    seeded = subprocess.run(["du", "-h", str(body)], capture_output=True, text=True).stdout.split()[0]
    print(f"  seeded body to {seeded} before first PUT")

    print("  progress: ", end="", flush=True)
    with open(body, "a") as f:
        for i in range(1, N + 1):
            f.write(f'{{"i":{i},"event":"{line}"}}\n')  # the file GROWS each turn
            f.flush()
            mc("cp", "-q", str(body), f"repro/{BUCKET}/{KEY}")  # ...and we overwrite the same key
            if i % 100 == 0:
                code, t = list_bucket()
                print(f"{i}(list={t:.6f}s) ", end="", flush=True)
    print()
    body.unlink(missing_ok=True)


# ----- 3. evidence --------------------------------------------------------
def du_sh(path):
    out = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True).stdout
    return out.split()[0] if out.strip() else "?"


def report():
    say("RESULT")
    ls = mc("ls", f"repro/{BUCKET}/{KEY}", capture=True).stdout.split()
    logical_sz = " ".join(ls[-3:-1]) if len(ls) >= 3 else "?"

    entries = list(OBJDIR.iterdir()) if OBJDIR.exists() else []
    data_dirs = [p for p in entries if p.is_dir()]
    fcode, ft = list_bucket()

    print(f"  overwrites issued ............. {N}")
    print(f"  logical object size ........... {logical_sz} (one current object)")
    print(f"  on-disk entries in object dir . {len(entries)}")
    print(f"  └─ orphaned data directories .. {len(data_dirs)}   <- should be 1 if GC worked")
    print(f"  on-disk footprint ............. {du_sh(OBJDIR)}   <- vs the logical size above")
    print(f"  non-delimited LIST ............ HTTP {fcode} in {ft:.6f}s")
    print()
    print("  one object dir, abbreviated:")
    for p in sorted(e.name for e in entries)[:4]:
        print(f"    {p}")
    print(f"    ... ({len(entries)} entries total)")
    print("  a sample data dir holds a full copy of that PUT's body:")
    if data_dirs:
        listing = subprocess.run(["ls", "-la", str(data_dirs[0])], capture_output=True, text=True).stdout
        print("\n".join("    " + ln for ln in listing.splitlines()))

    print()
    if len(data_dirs) > 1:
        print(f"\033[1;31m  BUG REPRODUCED:\033[0m {N} overwrites of one key left {len(data_dirs)} data dirs (expected 1).")
        print("  Each overwrite leaked its predecessor; LIST cost and disk use scale with overwrite count.")
    else:
        print("\033[1;32m  No leak observed:\033[0m object dir has a single data dir (GC working in this build).")
    if KEEP:
        print(f"\n  (--keep) data dir left at: {DATA}")


def main():
    fetch_binaries()
    start_server()
    drive()
    report()


if __name__ == "__main__":
    main()
