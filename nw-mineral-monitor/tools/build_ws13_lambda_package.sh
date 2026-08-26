#!/usr/bin/env bash
# Build the WS13 retrieval Lambda deployment zip (infra/ws13_query_lambda.py
# plus psycopg 3 with its compiled libpq).
#
# The whole point of this script is the platform pin. `pip install psycopg
# [binary]` on the build host resolves a wheel for the BUILD host - on this
# project that is a macOS arm64 wheel - and Lambda then fails at import with
# "No module named 'psycopg_binary'" or an ELF class error, at runtime, inside
# a VPC where there is no egress to debug from. So the install is pinned to
# manylinux x86_64 and the result is VERIFIED afterwards by reading the ELF
# header of every shared object actually placed in the package. Anything else
# fails the build here rather than in production.
#
# infra/ws13_retrieval.yaml declares Architectures: [x86_64] explicitly for the
# same reason: a later arm64 switch must break the build, not the function.
#
# Usage:
#   ./build_ws13_lambda_package.sh                       # build to var/
#   ./build_ws13_lambda_package.sh --output /tmp/fn.zip
#   ./build_ws13_lambda_package.sh --upload s3://bucket/ws13/lambda/query.zip
#   ./build_ws13_lambda_package.sh --upload s3://bucket/ws13/lambda/
#
# A trailing slash means a CONTENT-ADDRESSED key: the object is named for the
# sha256 this script already computes. That matters because CloudFormation
# compares only the literal S3Bucket/S3Key/S3ObjectVersion property values, so
# re-uploading a new zip over a fixed key is not a property change - the stack
# update succeeds and the function keeps running the previous code. A key that
# changes with the bytes (or the S3ObjectVersion printed after an upload to a
# versioned bucket) is what makes a deploy actually deploy.
set -euo pipefail

PLATFORM_TAGS=(manylinux2014_x86_64 manylinux_2_17_x86_64)
PYTHON_VERSION="3.12"
# psycopg 3 only; psycopg2 is a different package with a different API and
# would silently break the `conn.execute(sql, params)` idiom used throughout.
PSYCOPG_SPEC="${WS13_PSYCOPG_SPEC:-psycopg[binary]>=3.2,<4}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SOURCE="$ROOT/infra/ws13_query_lambda.py"
OUTPUT="$ROOT/var/ws13-query-lambda.zip"
UPLOAD=""

need() { command -v "$1" >/dev/null || { echo "ERROR: '$1' not found"; exit 1; }; }

while [ $# -gt 0 ]; do
  case "$1" in
    --output) OUTPUT="$2"; shift 2 ;;
    --upload) UPLOAD="$2"; shift 2 ;;
    --python) PYTHON_VERSION="$2"; shift 2 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown argument '$1'"; exit 1 ;;
  esac
done

need python3; need zip
# Resolve the output to an absolute path: the zip step runs from the build
# directory, so a relative --output would land in the temp dir and vanish.
OUTPUT_DIR="$(cd "$(dirname "$OUTPUT")" 2>/dev/null && pwd || true)"
if [ -z "$OUTPUT_DIR" ]; then
  mkdir -p "$(dirname "$OUTPUT")"
  OUTPUT_DIR="$(cd "$(dirname "$OUTPUT")" && pwd)"
fi
OUTPUT="$OUTPUT_DIR/$(basename "$OUTPUT")"
[ -f "$SOURCE" ] || { echo "ERROR: missing $SOURCE"; exit 1; }
if [ -n "$UPLOAD" ]; then
  need aws
  case "$UPLOAD" in
    s3://*/*) ;;
    *) echo "ERROR: --upload needs s3://bucket/key, got '$UPLOAD'"; exit 1 ;;
  esac
fi

BUILD="$(mktemp -d "${TMPDIR:-/tmp}/nwmm-ws13-lambda.XXXXXX")"
cleanup() { rm -rf "$BUILD"; }
trap cleanup EXIT

echo "==> [1/5] Installing $PSYCOPG_SPEC for linux/x86_64 (cpython $PYTHON_VERSION)"
PLATFORM_ARGS=()
for tag in "${PLATFORM_TAGS[@]}"; do PLATFORM_ARGS+=(--platform "$tag"); done
# --only-binary=:all: is mandatory with --platform: without it pip would build
# a source distribution against the BUILD host's libpq and headers.
python3 -m pip install --quiet --no-cache-dir \
  "${PLATFORM_ARGS[@]}" \
  --only-binary=:all: \
  --implementation cp \
  --python-version "$PYTHON_VERSION" \
  --target "$BUILD" \
  "$PSYCOPG_SPEC"

echo "==> [2/5] Adding the handler as index.py"
cp "$SOURCE" "$BUILD/index.py"

echo "==> [3/5] Verifying the package is manylinux x86_64"
python3 - "$BUILD" <<'PY'
import pathlib
import sys

build = pathlib.Path(sys.argv[1])
failures = []

if not (build / "psycopg").is_dir():
    failures.append("psycopg package is missing from the build directory")
if not (build / "psycopg_binary").is_dir():
    failures.append("psycopg_binary is missing: the pure-python psycopg alone "
                    "needs a system libpq that the Lambda runtime does not have")

# The recorded wheel tag is the first check: pip writes the tag of the wheel it
# actually chose into <dist-info>/WHEEL, so this catches a build host that
# resolved a macOS or aarch64 wheel despite the --platform pins.
tags = []
for wheel_file in build.glob("psycopg_binary-*.dist-info/WHEEL"):
    for line in wheel_file.read_text().splitlines():
        if line.startswith("Tag:"):
            tags.append(line.split(":", 1)[1].strip())
if not tags:
    failures.append("no psycopg_binary-*.dist-info/WHEEL tag found to verify")
for tag in tags:
    if "x86_64" not in tag or "manylinux" not in tag:
        failures.append(f"psycopg_binary wheel tag is not manylinux x86_64: {tag}")

# The authoritative check: read the ELF header of every shared object that
# ended up in the package. e_ident[4]==2 is ELF64 and e_machine==0x3E is
# x86-64 (0xB7 would be aarch64, and a Mach-O dylib has no ELF magic at all).
objects = sorted(build.rglob("*.so")) + sorted(build.rglob("*.so.*"))
if not objects:
    failures.append("package contains no compiled shared object; psycopg would "
                    "fall back to a system libpq that Lambda does not ship")
for obj in objects:
    header = obj.read_bytes()[:20]
    relative = obj.relative_to(build)
    if header[:4] != b"\x7fELF":
        failures.append(f"{relative} is not an ELF object (wrong platform wheel)")
        continue
    if header[4] != 2:
        failures.append(f"{relative} is not 64-bit ELF")
    machine = int.from_bytes(header[18:20], "little")
    if machine != 0x3E:
        failures.append(f"{relative} targets ELF machine 0x{machine:02X}, "
                        f"not x86-64 (0x3E)")

if failures:
    print("PACKAGE VERIFICATION FAILED:", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    raise SystemExit(1)

print(f"    wheel tags: {', '.join(sorted(set(tags)))}")
print(f"    verified {len(objects)} ELF x86-64 shared object(s)")
PY

echo "==> [4/5] Verifying the handler imports with no psycopg, no AWS, no DB"
# The retrieval module imports psycopg lazily on purpose so its SQL builders,
# fusion and citation resolver stay unit-testable. Assert that here, and assert
# the canonical halfvec strings the other modules import survived packaging.
( cd "$BUILD" && PYTHONPATH= python3 -B -c "
import index
assert index.INDEX_NAME == 'ws13_chunks_titan_hnsw', index.INDEX_NAME
assert index.HALFVEC_EXPR == 'titan_embedding::halfvec(1024)', index.HALFVEC_EXPR
assert index.HALFVEC_EXPR in index.CREATE_INDEX_SQL
assert index.HALFVEC_EXPR in index.ORDER_BY_SQL
assert index.ORDER_BY_SQL.endswith('<=> %s::halfvec(1024)')
# The gate probe must stay a PLAIN EXPLAIN: EXPLAIN (ANALYZE) executes the
# statement, so a guard against a 852,027-row sequential scan would run that
# scan to discover it. Timings live in the separate --measure constant.
assert index.EXPLAIN_SQL.startswith('EXPLAIN SELECT'), index.EXPLAIN_SQL
assert 'ANALYZE' not in index.EXPLAIN_SQL.upper(), index.EXPLAIN_SQL
assert index.EXPLAIN_ANALYZE_SQL.startswith('EXPLAIN (ANALYZE, BUFFERS)')
assert index.ORDER_BY_SQL in index.EXPLAIN_SQL
# The verifier has to be able to EXPLAIN the FILTERED shape a real request
# runs, not only the bare probe: the semi-join is what loses the index.
filtered, params = index.explain_ann_sql({'state': 'ID'}, '[0.0]', 200)
assert filtered.startswith('EXPLAIN SELECT'), filtered
assert 'EXISTS (SELECT 1 FROM ws13_documents d' in filtered, filtered
assert index.ORDER_BY_SQL in filtered, filtered
assert params[0] == 'ID' and params[-1] == 200, params
assert callable(index.handler)
print('    index.handler importable; halfvec constants intact')
" )

echo "==> [5/5] Writing $OUTPUT"
# Strip bytecode last, not earlier: it is build-host .pyc compiled by whatever
# python3 this machine has, and a magic number from the wrong minor version is
# dead weight in a package the runtime would recompile anyway.
find "$BUILD" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -name '*.pyc' -delete 2>/dev/null || true
mkdir -p "$(dirname "$OUTPUT")"
rm -f "$OUTPUT"
# -X drops the build host's uid/gid and extra attributes, so two builds of the
# same inputs differ only where the wheels differ.
( cd "$BUILD" && zip -qrX "$OUTPUT" . )
# The digest is written out, not just printed: the content-addressed upload key
# below is built from it, and re-deriving it in shell would be a second
# implementation of the same hash.
python3 - "$OUTPUT" "$BUILD/zip.sha256" "${UPLOAD:-}" <<'PY'
import hashlib
import pathlib
import sys
import zipfile

path = pathlib.Path(sys.argv[1])
digest_path = pathlib.Path(sys.argv[2])
upload = sys.argv[3] if len(sys.argv) > 3 else ""
with zipfile.ZipFile(path) as archive:
    names = archive.namelist()
missing = [n for n in ("index.py",) if n not in names]
if missing:
    raise SystemExit(f"zip is missing {missing}")
digest = hashlib.sha256(path.read_bytes()).hexdigest()
digest_path.write_text(digest)
size_mb = path.stat().st_size / 1_048_576
print(f"    {len(names)} entries, {size_mb:.1f} MiB")
print(f"    sha256 {digest}")
# 50 MiB bounds a DIRECT upload only, so it is fatal only when there is no
# --upload. Aborting regardless is what made the remedy in the message
# unreachable: --upload was already passed, set -e killed the script before the
# S3 copy, and the operator was told to use the flag they had just used.
if path.stat().st_size > 50 * 1_048_576:
    if not upload:
        raise SystemExit("zip exceeds the 50 MiB direct-upload limit; publish "
                         "it through S3 with --upload s3://bucket/prefix/")
    print("    over the 50 MiB direct-upload limit, which is why this goes "
          "through S3: publish with --s3-bucket/--s3-key, never --zip-file")
PY

if [ -n "$UPLOAD" ]; then
  DIGEST="$(cat "$BUILD/zip.sha256")"
  # A trailing slash means "name the object for its content". A fixed key in an
  # unversioned bucket is the deploy that silently does nothing: same
  # S3Bucket/S3Key property values, no CloudFormation change, old code stays.
  case "$UPLOAD" in
    */) UPLOAD="${UPLOAD}ws13-query-${DIGEST:0:16}.zip" ;;
  esac
  echo "==> Uploading to $UPLOAD"
  aws s3 cp "$OUTPUT" "$UPLOAD"
  UPLOAD_BUCKET="$(echo "$UPLOAD" | sed -e 's|^s3://||' -e 's|/.*$||')"
  UPLOAD_KEY="$(echo "$UPLOAD" | sed -e 's|^s3://[^/]*/||')"
  VERSION="$(aws s3api head-object --bucket "$UPLOAD_BUCKET" \
               --key "$UPLOAD_KEY" --query VersionId --output text \
               2>/dev/null || true)"
  echo "    bucket $UPLOAD_BUCKET"
  echo "    key    $UPLOAD_KEY"
  if [ -n "$VERSION" ] && [ "$VERSION" != "None" ] && [ "$VERSION" != "null" ]; then
    echo "    version $VERSION"
    echo "    deploy: aws cloudformation deploy --template-file \\"
    echo "              infra/ws13_retrieval.yaml --stack-name <stack> \\"
    echo "              --parameter-overrides CodeS3Bucket=$UPLOAD_BUCKET \\"
    echo "                CodeS3Key=$UPLOAD_KEY CodeS3ObjectVersion=$VERSION"
  else
    echo "    WARNING: no S3 version id, so this bucket is not versioned."
    echo "    With neither a version id nor a content-addressed key, uploading"
    echo "    a new zip over this key and running 'aws cloudformation deploy'"
    echo "    changes no property, updates nothing, and leaves the function"
    echo "    running the PREVIOUS code. Re-run with"
    echo "    --upload s3://$UPLOAD_BUCKET/$(dirname "$UPLOAD_KEY")/ for a key"
    echo "    named after the sha256, or turn on bucket versioning."
    echo "    deploy: aws cloudformation deploy --template-file \\"
    echo "              infra/ws13_retrieval.yaml --stack-name <stack> \\"
    echo "              --parameter-overrides CodeS3Bucket=$UPLOAD_BUCKET \\"
    echo "                CodeS3Key=$UPLOAD_KEY"
  fi
  echo "    or: aws lambda update-function-code --function-name <name> \\"
  echo "          --s3-bucket $UPLOAD_BUCKET --s3-key $UPLOAD_KEY"
fi

echo "done: $OUTPUT"
