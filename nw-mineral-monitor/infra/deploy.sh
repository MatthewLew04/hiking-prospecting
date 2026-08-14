#!/usr/bin/env bash
# NW Mineral Monitor — one-shot AWS deploy.
# Usage:
#   ./deploy.sh                # full deploy (stack + lambda code + site upload + first refresh)
#   ./deploy.sh update-site    # re-upload site/ only
#   ./deploy.sh upload-ws10-assets  # upload ignored quad rasters/tiles (never deletes remote assets)
#   ./deploy.sh upload-release-assets  # upload immutable, gate-approved WS11 state archives
#   ./deploy.sh preflight           # run executable registry/data/tiling gates
#   ./deploy.sh refresh        # trigger the claims updater now
#   ./deploy.sh teardown       # delete everything (empties the bucket first)
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
STACK="${STACK_NAME:-nw-mineral-monitor}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SITE="$HERE/../site"
WS10_ASSETS="$HERE/../pipelines/cache/ws10/assets"
WS10_ASSET_PREFIX="ws10-assets"
NATIONAL_ASSET_PREFIX="map-assets"
RELEASE_ASSETS="$SITE/map-assets/releases"
RELEASE_ASSET_PREFIX="map-assets/releases"
RELEASE_ASSET_VALIDATOR="$HERE/../pipelines/validate_release_assets.py"
PUBLIC_SITE_VALIDATOR="$HERE/../pipelines/validate_public_site.py"
EARLY_RELEASE_ALLOWLIST=""
PUBLIC_BASELINE_UPLOAD_PLAN=""
PUBLIC_DEPLOYMENT_MANIFEST=""

cleanup_release_allowlist() {
  if [ -n "$EARLY_RELEASE_ALLOWLIST" ] && [ -f "$EARLY_RELEASE_ALLOWLIST" ]; then
    rm -f "$EARLY_RELEASE_ALLOWLIST"
  fi
  if [ -n "$PUBLIC_BASELINE_UPLOAD_PLAN" ] && [ -f "$PUBLIC_BASELINE_UPLOAD_PLAN" ]; then
    rm -f "$PUBLIC_BASELINE_UPLOAD_PLAN"
  fi
  if [ -n "$PUBLIC_DEPLOYMENT_MANIFEST" ] && [ -f "$PUBLIC_DEPLOYMENT_MANIFEST" ]; then
    rm -f "$PUBLIC_DEPLOYMENT_MANIFEST"
  fi
}
trap cleanup_release_allowlist EXIT

write_release_allowlist() {
  local output="$1"
  python3 "$RELEASE_ASSET_VALIDATOR" --site "$SITE" --format paths > "$output"
}

write_public_baseline_deployment() {
  local plan_output="$1"
  local manifest_output="$2"
  python3 "$PUBLIC_SITE_VALIDATOR" --site "$SITE" --format deployment \
    --manifest-output "$manifest_output" > "$plan_output"
}

refresh_public_baseline_deployment() {
  local replacement_plan replacement_manifest
  replacement_plan="$(mktemp "${TMPDIR:-/tmp}/nwmm-public-baselines.XXXXXX")"
  replacement_manifest="$(mktemp "${TMPDIR:-/tmp}/nwmm-public-manifest.XXXXXX")"
  if ! write_public_baseline_deployment \
      "$replacement_plan" "$replacement_manifest"; then
    rm -f "$replacement_plan" "$replacement_manifest"
    return 1
  fi
  if [ -n "$PUBLIC_BASELINE_UPLOAD_PLAN" ] && [ -f "$PUBLIC_BASELINE_UPLOAD_PLAN" ]; then
    rm -f "$PUBLIC_BASELINE_UPLOAD_PLAN"
  fi
  if [ -n "$PUBLIC_DEPLOYMENT_MANIFEST" ] && [ -f "$PUBLIC_DEPLOYMENT_MANIFEST" ]; then
    rm -f "$PUBLIC_DEPLOYMENT_MANIFEST"
  fi
  PUBLIC_BASELINE_UPLOAD_PLAN="$replacement_plan"
  PUBLIC_DEPLOYMENT_MANIFEST="$replacement_manifest"
}

preflight() {
  echo "==> Validating WS11 registry, coverage, delivery, and legacy quad assets…"
  python3 "$HERE/../pipelines/state_registry.py" validate
  python3 "$HERE/../pipelines/build_runtime_registry.py"
  python3 "$HERE/../pipelines/reconcile_manifest.py" --check
  python3 "$HERE/../pipelines/build_coverage.py" --check
  python3 "$RELEASE_ASSET_VALIDATOR" --site "$SITE"
  if [ -n "$PUBLIC_BASELINE_UPLOAD_PLAN" ] && \
      [ -f "$PUBLIC_BASELINE_UPLOAD_PLAN" ] && \
      [ -n "$PUBLIC_DEPLOYMENT_MANIFEST" ] && \
      [ -f "$PUBLIC_DEPLOYMENT_MANIFEST" ]; then
    echo "    public content-addressed deployment already validated before AWS credential discovery"
  else
    refresh_public_baseline_deployment
  fi
  # Use the same strict runner as CI. It binds the repository's ``tests``
  # namespace explicitly so an unrelated site-packages ``tests`` package
  # cannot shadow shared fixtures during deployment preflight.
  python3 "$HERE/../ci/run_tests.py"
  # The strict test runtime may be a pinned geospatial environment, while the
  # exhaustive pure-Python PMTiles decoder is materially faster in the system
  # interpreter. Keep the default unchanged but allow operators to select that
  # interpreter explicitly without weakening or skipping either gate.
  validator_python="${NWMM_VALIDATOR_PYTHON:-python3}"
  "$validator_python" "$HERE/../pipelines/validate_national.py" --profile progress
  python3 "$HERE/../pipelines/validate_quad_geology.py" --skip-assets
}

require_cognito_secrets() {
  local missing=()
  for name in COGNITO_PASS COGNITO_PASS_SEAN COGNITO_PASS_RACHEL; do
    [ -n "${!name:-}" ] || missing+=("$name")
  done
  if [ "${#missing[@]}" -ne 0 ]; then
    echo "ERROR: explicit Cognito passwords are required; no repository defaults exist."
    echo "       Set ${missing[*]} to distinct strong values (12+ characters with upper/lowercase, number, and symbol)."
    exit 1
  fi
}

need() { command -v "$1" >/dev/null || { echo "ERROR: '$1' not found — see DEPLOY.md prerequisites"; exit 1; }; }
if [ "${1:-deploy}" = "preflight" ]; then
  preflight
  exit 0
fi
# Standalone release publication validates the exact local allowlist before
# credentials, stack discovery, or any other AWS request.  At 0/49 released
# states an empty tree is the only valid tree and there is nothing to upload.
if [ "${1:-deploy}" = "upload-release-assets" ]; then
  EARLY_RELEASE_ALLOWLIST="$(mktemp "${TMPDIR:-/tmp}/nwmm-release-assets.XXXXXX")"
  write_release_allowlist "$EARLY_RELEASE_ALLOWLIST"
  if [ ! -s "$EARLY_RELEASE_ALLOWLIST" ]; then
    echo "    no gate-approved WS11 release archives to upload (0 allowlisted files)"
    exit 0
  fi
fi
# A normal site publication also fails before credential or stack discovery if
# the public tree contains an undeclared binary, symlink, hidden/temp/backup
# file, or sensitive path.  The resulting exact allowlist is the only input to
# the binary upload phase below.
if [ "${1:-deploy}" = "deploy" ] || [ "${1:-deploy}" = "update-site" ]; then
  refresh_public_baseline_deployment
fi
need aws; need zip
aws sts get-caller-identity >/dev/null || { echo "ERROR: AWS credentials not configured — run 'aws configure'"; exit 1; }

outputs() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

# AWS CLI v1 doesn't know --cli-binary-format (and doesn't need it)
if aws --version 2>&1 | grep -q '^aws-cli/1'; then PAYLOAD_FMT=""; else PAYLOAD_FMT="--cli-binary-format raw-in-base64-out"; fi

upload_ws10_assets() {
  local bucket="$1"
  local destination="s3://$bucket/$WS10_ASSET_PREFIX"
  local -a binary_sync json_sync

  [ -d "$WS10_ASSETS" ] || {
    echo "ERROR: WS10 asset directory not found: $WS10_ASSETS"
    echo "       Build the quad rasters/COGs/tiles first; see RUNBOOK.md."
    return 1
  }
  find "$WS10_ASSETS" -type f -print -quit | grep -q . || {
    echo "ERROR: WS10 asset directory contains no files: $WS10_ASSETS"
    return 1
  }
  if find "$WS10_ASSETS" -type l -print -quit | grep -q .; then
    echo "ERROR: refusing to follow symlinks below $WS10_ASSETS"
    return 1
  fi
  echo "==> Uploading WS10 quad assets to $destination (add/update only; no remote delete)…"
  # Binary map assets change infrequently. JSON/GeoJSON pointers and TileJSON
  # get a shorter TTL so provenance or bounds corrections propagate promptly.
  binary_sync=(aws s3 sync "$WS10_ASSETS" "$destination" --region "$REGION" \
    --no-follow-symlinks --exclude ".DS_Store" --exclude "*.json" --exclude "*.geojson" \
    --cache-control "public, max-age=86400")
  json_sync=(aws s3 sync "$WS10_ASSETS" "$destination" --region "$REGION" \
    --no-follow-symlinks --exclude "*" --include "*.json" --include "*.geojson" \
    --cache-control "public, max-age=900")
  if [ "${WS10_UPLOAD_DRY_RUN:-0}" = "1" ]; then
    binary_sync+=(--dryrun)
    json_sync+=(--dryrun)
    echo "    dry run only (WS10_UPLOAD_DRY_RUN=1)"
  else
    # Thousands of tiny XYZ objects otherwise flood CI/agent logs. Errors and
    # the final invalidation id remain visible.
    binary_sync+=(--only-show-errors)
    json_sync+=(--only-show-errors)
  fi
  "${binary_sync[@]}"
  "${json_sync[@]}"

  if [ "${WS10_UPLOAD_DRY_RUN:-0}" != "1" ]; then
    local dist
    dist="$(outputs DistributionId)"
    if [ -n "$dist" ] && [ "$dist" != "None" ]; then
      aws cloudfront create-invalidation --distribution-id "$dist" \
        --paths "/$WS10_ASSET_PREFIX/*" --query 'Invalidation.Id' --output text
    fi
    echo "WS10 assets uploaded to $destination; no remote objects were removed."
  fi
}

upload_release_assets() {
  local bucket="$1"
  local allowlist
  local destination="s3://$bucket/$RELEASE_ASSET_PREFIX"
  local relative object_key upload_status=0

  allowlist="$(mktemp "${TMPDIR:-/tmp}/nwmm-release-assets.XXXXXX")"
  if ! write_release_allowlist "$allowlist"; then
    rm -f "$allowlist"
    return 1
  fi
  if [ ! -s "$allowlist" ]; then
    echo "    no gate-approved WS11 release archives to upload (0 allowlisted files)"
    rm -f "$allowlist"
    return 0
  fi
  echo "==> Uploading immutable WS11 release archives to $destination (add/update only)…"
  # Upload exact validated objects one at a time.  A recursive sync would let
  # an orphan or disabled-state artifact escape the registry authorization.
  while IFS= read -r relative || [ -n "$relative" ]; do
    [ -n "$relative" ] || continue
    object_key="${relative#${RELEASE_ASSET_PREFIX}/}"
    if ! aws s3 cp "$SITE/$relative" "$destination/$object_key" --region "$REGION" \
        --only-show-errors --cache-control "public, max-age=31536000, immutable"; then
      upload_status=1
      break
    fi
  done < "$allowlist"
  rm -f "$allowlist"
  [ "$upload_status" = "0" ] || return "$upload_status"
  echo "    release archives uploaded before their public manifest pointers; no remote objects removed"
}

upload_watch_evidence() {
  local bucket="$1"
  local source="$SITE/data/evidence/watch"
  [ -d "$source" ] || return 0
  # Runtime writes are already in S3. This add/update-only sync also carries
  # reviewed evidence checked into a release checkout, while the mutable data
  # sync below excludes this prefix so --delete cannot erase Lambda evidence.
  aws s3 sync "$source" "s3://$bucket/data/evidence/watch" --region "$REGION" \
    --no-follow-symlinks --only-show-errors \
    --cache-control "public, max-age=31536000, immutable"
}

public_data_binary_identity() {
  local path="$1"
  python3 - "$path" <<'PY'
import base64
import hashlib
import os
import sys

path = sys.argv[1]
digest = hashlib.sha256()
with open(path, 'rb') as source:
    while True:
        block = source.read(1024 * 1024)
        if not block:
            break
        digest.update(block)
print(f'{os.path.getsize(path)}\t{base64.b64encode(digest.digest()).decode("ascii")}')
PY
}

remote_public_data_binary_identity() {
  local bucket="$1"
  local object_key="$2"
  aws s3api head-object --bucket "$bucket" --key "$object_key" \
    --region "$REGION" --checksum-mode ENABLED \
    --query '[ContentLength,ChecksumSHA256]' --output text
}

upload_and_verify_public_data_binaries() {
  local bucket="$1"
  local local_path lower_path relative object_key content_type identity extra
  local content_basename content_digest local_suffix remote_suffix
  local expected_size expected_sha256_hex expected_sha256 current_size current_sha256
  local remote_identity remote_size remote_sha256
  local count=0

  if [ -z "$PUBLIC_BASELINE_UPLOAD_PLAN" ] || \
      [ ! -f "$PUBLIC_BASELINE_UPLOAD_PLAN" ]; then
    echo "ERROR: validated public baseline deployment plan is unavailable" >&2
    return 1
  fi
  echo "==> Uploading and remotely verifying immutable public baseline PMTiles/COGs…"
  while IFS=$'\t' read -r relative object_key expected_size expected_sha256_hex \
      expected_sha256 extra || \
      [ -n "$relative" ]; do
    [ -n "$relative" ] || continue
    if [ -n "$extra" ] || [ -z "$object_key" ] || [ -z "$expected_size" ] || \
        [ -z "$expected_sha256_hex" ] || [ -z "$expected_sha256" ]; then
      echo "ERROR: malformed validated public baseline deployment row" >&2
      return 1
    fi
    case "$relative" in
      data/*.pmtiles|data/*.tif|data/*.tiff) ;;
      *) echo "ERROR: unsafe path in validated public baseline allowlist: $relative" >&2; return 1 ;;
    esac
    case "/$relative/" in
      *'/../'*|*'/./'*)
        echo "ERROR: non-canonical path in public baseline allowlist: $relative" >&2
        return 1 ;;
    esac
    case "$object_key" in
      map-assets/baselines/*.pmtiles|map-assets/baselines/*.tif|map-assets/baselines/*.tiff) ;;
      *) echo "ERROR: unsafe content key in public baseline deployment plan: $object_key" >&2; return 1 ;;
    esac
    content_basename="${object_key#map-assets/baselines/}"
    content_digest="${content_basename%%.*}"
    case "$content_digest" in
      *[!0-9a-f]*|'')
        echo "ERROR: non-content-addressed public baseline key: $object_key" >&2
        return 1 ;;
    esac
    if [ "${#content_digest}" -ne 64 ]; then
      echo "ERROR: public baseline key must contain a full SHA-256: $object_key" >&2
      return 1
    fi
    if [ "$content_digest" != "$expected_sha256_hex" ]; then
      echo "ERROR: public baseline key does not match its validated SHA-256: $object_key" >&2
      return 1
    fi
    local_suffix="${relative##*.}"
    remote_suffix="${object_key##*.}"
    if [ "$local_suffix" != "$remote_suffix" ]; then
      echo "ERROR: public baseline content key changed the source suffix: $object_key" >&2
      return 1
    fi
    if [ "$object_key" != \
        "map-assets/baselines/${expected_sha256_hex}.${local_suffix}" ]; then
      echo "ERROR: non-canonical public baseline content key: $object_key" >&2
      return 1
    fi
    case "$expected_size" in
      *[!0-9]*|'')
        echo "ERROR: invalid byte count in public baseline deployment plan" >&2
        return 1 ;;
    esac
    local_path="$SITE/$relative"
    if [ -L "$local_path" ] || [ ! -f "$local_path" ]; then
      echo "ERROR: allowlisted public baseline is not a regular file: $relative" >&2
      return 1
    fi
    lower_path="$(printf '%s' "$local_path" | tr '[:upper:]' '[:lower:]')"
    case "$lower_path" in
      *.pmtiles) content_type="application/vnd.pmtiles" ;;
      *.tif|*.tiff) content_type="image/tiff" ;;
      *) echo "ERROR: unsupported public baseline binary: $relative" >&2; return 1 ;;
    esac
    identity="$(public_data_binary_identity "$local_path")"
    IFS=$'\t' read -r current_size current_sha256 <<< "$identity"
    if [ "$current_size" != "$expected_size" ] || \
        [ "$current_sha256" != "$expected_sha256" ]; then
      echo "ERROR: public baseline changed after deployment validation: $relative" >&2
      return 1
    fi
    remote_identity="$(remote_public_data_binary_identity \
      "$bucket" "$object_key" 2>/dev/null || true)"
    read -r remote_size remote_sha256 <<< "$remote_identity"
    if [ "$remote_size" = "$expected_size" ] && \
        [ "$remote_sha256" = "$expected_sha256" ]; then
      count=$((count + 1))
      continue
    fi
    # PutObject is a single-object upload and S3 rejects it when the supplied
    # full-object SHA-256 does not match the received bytes.
    aws s3api put-object --bucket "$bucket" --key "$object_key" \
      --body "$local_path" --region "$REGION" \
      --checksum-sha256 "$expected_sha256" --content-type "$content_type" \
      --cache-control "public, max-age=31536000, immutable" >/dev/null
    identity="$(public_data_binary_identity "$local_path")"
    IFS=$'\t' read -r current_size current_sha256 <<< "$identity"
    if [ "$current_size" != "$expected_size" ] || \
        [ "$current_sha256" != "$expected_sha256" ]; then
      echo "ERROR: public baseline changed during upload: $relative" >&2
      return 1
    fi
    remote_identity="$(remote_public_data_binary_identity \
      "$bucket" "$object_key")"
    read -r remote_size remote_sha256 <<< "$remote_identity"
    if [ "$remote_size" != "$expected_size" ] || \
        [ "$remote_sha256" != "$expected_sha256" ]; then
      echo "ERROR: remote public baseline identity mismatch: $relative" >&2
      return 1
    fi
    count=$((count + 1))
  done < "$PUBLIC_BASELINE_UPLOAD_PLAN"
  rm -f "$PUBLIC_BASELINE_UPLOAD_PLAN"
  PUBLIC_BASELINE_UPLOAD_PLAN=""
  echo "    remotely verified $count immutable public baseline object(s)"
}

sync_public_site_without_pointers() {
  local bucket="$1"
  aws s3 sync "$SITE" "s3://$bucket" --region "$REGION" --delete \
    --no-follow-symlinks --cache-control "public, max-age=3600" \
    --exclude "data/*" --exclude "auth.json" --exclude "index.html" \
    --exclude "*.pmtiles" --exclude "*.tif" --exclude "*.tiff" \
    --exclude "ckpt/*" --exclude "watch/*" \
    --exclude "$WS10_ASSET_PREFIX/*" --exclude "$NATIONAL_ASSET_PREFIX/*"
}

sync_public_data_without_pointers_or_binaries() {
  local bucket="$1"
  aws s3 sync "$SITE/data" "s3://$bucket/data" --region "$REGION" --delete \
    --no-follow-symlinks --cache-control "public, max-age=900" \
    --exclude "alerts/*" --exclude "claims/*" --exclude "sites/*" \
    --exclude "evidence/watch/*" \
    --exclude "$WS10_ASSET_PREFIX/*" --exclude "$NATIONAL_ASSET_PREFIX/*" \
    --exclude "*.pmtiles" --exclude "*.tif" --exclude "*.tiff" \
    --exclude "manifest.json" --exclude "coverage.json"
}

upload_public_data_pointers() {
  local bucket="$1"
  # Coverage first, then the transformed primary manifest pointer. Both remain
  # behind the remotely verified content-addressed binaries and non-pointer
  # data. The checked-in friendly-path manifest is never uploaded directly.
  if [ ! -f "$SITE/data/coverage.json" ]; then
    echo "ERROR: required public data pointer is missing: data/coverage.json" >&2
    return 1
  fi
  if [ -z "$PUBLIC_DEPLOYMENT_MANIFEST" ] || \
      [ -L "$PUBLIC_DEPLOYMENT_MANIFEST" ] || \
      [ ! -f "$PUBLIC_DEPLOYMENT_MANIFEST" ]; then
    echo "ERROR: validated transformed deployment manifest is unavailable" >&2
    return 1
  fi
  aws s3 cp "$SITE/data/coverage.json" "s3://$bucket/data/coverage.json" \
    --region "$REGION" --only-show-errors \
    --cache-control "public, max-age=900" --content-type "application/json"
  aws s3 cp "$PUBLIC_DEPLOYMENT_MANIFEST" "s3://$bucket/data/manifest.json" \
    --region "$REGION" --only-show-errors \
    --cache-control "public, max-age=900" --content-type "application/json"
}

upload_public_index() {
  local bucket="$1"
  aws s3 cp "$SITE/index.html" "s3://$bucket/index.html" --region "$REGION" \
    --cache-control "public, max-age=60" --content-type "text/html"
}

remove_legacy_browser_json() {
  local bucket="$1"
  # These exact public prefixes contain only superseded whole-state columnar
  # snapshots. Bucket versioning makes the removal recoverable, while the
  # current versions disappear from CloudFront immediately.
  aws s3 rm "s3://$bucket/data/claims/" --recursive --region "$REGION" --only-show-errors
  aws s3 rm "s3://$bucket/data/sites/" --recursive --region "$REGION" --only-show-errors
  echo "    removed public legacy data/claims and data/sites snapshots (recoverable via S3 versions)"
}

wait_for_clear_state() {
  local status
  status="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)"
  case "$status" in
    DELETE_IN_PROGRESS)
      echo "    stack is mid-delete — waiting for it to finish (CloudFront teardown can take 5-15 min)…"
      aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION"
      echo "    delete finished; creating fresh." ;;
    ROLLBACK_COMPLETE|CREATE_FAILED)
      echo "    stack is in $status (a failed first create) — clearing it before recreating…"
      aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
      aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" ;;
  esac
}

case "${1:-deploy}" in
  deploy)
    preflight
    require_cognito_secrets
    echo "==> [1/5] Creating/updating CloudFormation stack '$STACK' in $REGION (5-10 min first time)…"
    wait_for_clear_state
    aws cloudformation deploy --template-file "$HERE/template.yaml" \
      --stack-name "$STACK" --region "$REGION" --capabilities CAPABILITY_IAM \
      --no-fail-on-empty-changeset

    BUCKET="$(outputs BucketName)"; FN="$(outputs UpdaterFunctionName)"; URL="$(outputs SiteURL)"
    echo "    bucket: $BUCKET"; echo "    lambda: $FN"

    echo "==> [2/5] Uploading Lambda code (updater + AI relay)…"
    ( cd "$HERE" && rm -f updater.zip && cp lambda_updater.py index.py && zip -q updater.zip index.py state_runtime.json state_clips.json spatial_clip.py && rm index.py )
    aws lambda update-function-code --function-name "$FN" --region "$REGION" \
      --zip-file "fileb://$HERE/updater.zip" >/dev/null
    rm -f "$HERE/updater.zip"
    ASKFN="$(outputs AskFunctionName)"
    if [ -n "$ASKFN" ] && [ "$ASKFN" != "None" ]; then
      ( cd "$HERE" && rm -f ask.zip && cp ask_lambda.py index.py && zip -q ask.zip index.py && rm index.py )
      aws lambda update-function-code --function-name "$ASKFN" --region "$REGION" \
        --zip-file "fileb://$HERE/ask.zip" >/dev/null
      rm -f "$HERE/ask.zip"
    fi
    WATCHFN="$(outputs WatchFunctionName)"
    if [ -n "$WATCHFN" ] && [ "$WATCHFN" != "None" ]; then
      ( cd "$HERE" && rm -f watch.zip && cp watch_lambda.py index.py && zip -q watch.zip index.py ak_deadlines.py state_runtime.json state_clips.json spatial_clip.py && rm index.py )
      aws lambda update-function-code --function-name "$WATCHFN" --region "$REGION" \
        --zip-file "fileb://$HERE/watch.zip" >/dev/null
      rm -f "$HERE/watch.zip"
      echo "    national 19-state expiration watch deployed: $WATCHFN (daily + fee window)"
    fi

    echo "==> [3/5] Uploading release archives, site ($(du -sh "$SITE" | cut -f1)), and Cognito config…"
    # The ordinary sync deliberately excludes map-assets. Publish immutable
    # DONE-gate archives first so a newly uploaded manifest cannot point at a
    # missing object, then sync the mutable site and manifest below.
    # Revalidate after the potentially long stack/Lambda phases so a local
    # tree change cannot bypass the exact allowlist captured before AWS.
    refresh_public_baseline_deployment
    upload_release_assets "$BUCKET"
    upload_watch_evidence "$BUCKET"
    sync_public_site_without_pointers "$BUCKET"
    upload_and_verify_public_data_binaries "$BUCKET"
    sync_public_data_without_pointers_or_binaries "$BUCKET"
    upload_public_data_pointers "$BUCKET"
    remove_legacy_browser_json "$BUCKET"

    POOL="$(outputs UserPoolId)"; CLIENT="$(outputs UserPoolClientId)"; ASKURL="$(outputs AskUrl)"
    printf '{"region":"%s","clientId":"%s","askUrl":"%s"}' "$REGION" "$CLIENT" "$ASKURL" > /tmp/auth.json
    aws s3 cp /tmp/auth.json "s3://$BUCKET/auth.json" --region "$REGION" \
      --cache-control "public, max-age=300" --content-type "application/json"
    # one entry per login — users are created by template.yaml (UserPoolUser
    # resources); this sets/refreshes their permanent passwords idempotently.
    for cred in \
        "${COGNITO_USER:-codyClinger}:${COGNITO_PASS}" \
        "sean:${COGNITO_PASS_SEAN}" \
        "rachel:${COGNITO_PASS_RACHEL}"; do
      u="${cred%%:*}"; p="${cred#*:}"
      aws cognito-idp admin-set-user-password --region "$REGION" \
        --user-pool-id "$POOL" --username "$u" --password "$p" --permanent \
        && echo "    login enabled: user '$u' (pool $POOL)" \
        || echo "    WARNING: could not set password for '$u' (user missing? run the stack update first)"
    done
    # index.html is the final public site object, after every dependency and
    # mutable data pointer has completed successfully.
    upload_public_index "$BUCKET"

    echo "==> [4/5] Running first claims refresh (2-4 min)…"
    aws lambda invoke --function-name "$FN" --region "$REGION" \
      $PAYLOAD_FMT --cli-read-timeout 900 \
      --payload '{"mode":"active"}' /tmp/updater-out.json >/dev/null && cat /tmp/updater-out.json && echo

    echo "==> [5/5] Clearing the CloudFront edge cache…"
    DIST="$(outputs DistributionId)"
    if [ -n "$DIST" ] && [ "$DIST" != "None" ]; then
      aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*" \
        --query 'Invalidation.Id' --output text
    else
      echo "    (DistributionId output not in stack yet — cache clears on its own within the hour)"
    fi
    echo
    echo "    Your map is live at:  $URL"
    echo "    (allow ~2-3 minutes for the invalidation, then hard-refresh)"
    ;;
  update-site)
    preflight
    BUCKET="$(outputs BucketName)"
    refresh_public_baseline_deployment
    upload_release_assets "$BUCKET"
    upload_watch_evidence "$BUCKET"
    sync_public_site_without_pointers "$BUCKET"
    upload_and_verify_public_data_binaries "$BUCKET"
    sync_public_data_without_pointers_or_binaries "$BUCKET"
    upload_public_data_pointers "$BUCKET"
    remove_legacy_browser_json "$BUCKET"
    upload_public_index "$BUCKET"
    DIST="$(outputs DistributionId)"
    [ -n "$DIST" ] && [ "$DIST" != "None" ] && aws cloudfront create-invalidation \
      --distribution-id "$DIST" --paths "/*" --query 'Invalidation.Id' --output text
    echo "site re-uploaded to s3://$BUCKET (cache cleared)"
    ;;
  upload-ws10-assets)
    BUCKET="$(outputs BucketName)"
    [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ] || {
      echo "ERROR: stack '$STACK' has no BucketName output; deploy the stack first."
      exit 1
    }
    upload_ws10_assets "$BUCKET"
    ;;
  upload-release-assets)
    BUCKET="$(outputs BucketName)"
    [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ] || {
      echo "ERROR: stack '$STACK' has no BucketName output; deploy the stack first."
      exit 1
    }
    # Revalidate after stack discovery as well; the early validation above is
    # what guarantees that no AWS call occurs for a bad standalone request.
    upload_release_assets "$BUCKET"
    DIST="$(outputs DistributionId)"
    [ -n "$DIST" ] && [ "$DIST" != "None" ] && aws cloudfront create-invalidation \
      --distribution-id "$DIST" --paths "/$RELEASE_ASSET_PREFIX/*" \
      --query 'Invalidation.Id' --output text
    ;;
  preflight)
    preflight
    ;;
  refresh)
    FN="$(outputs UpdaterFunctionName)"
    aws lambda invoke --function-name "$FN" --region "$REGION" \
      $PAYLOAD_FMT --cli-read-timeout 900 \
      --payload '{"mode":"active"}' /tmp/updater-out.json >/dev/null && cat /tmp/updater-out.json && echo
    ;;
  watch)
    # run the expiration watch now (mode: daily, or 'watch seasonal' for the fee scan)
    FN="$(outputs WatchFunctionName)"
    MODE="${2:-daily}"
    aws lambda invoke --function-name "$FN" --region "$REGION" \
      $PAYLOAD_FMT --cli-read-timeout 900 \
      --payload "{\"mode\":\"$MODE\"}" /tmp/watch-out.json >/dev/null && cat /tmp/watch-out.json && echo
    ;;
  teardown)
    BUCKET="$(outputs BucketName)"
    echo "This PERMANENTLY deletes stack '$STACK', bucket $BUCKET, the Cognito users, and the site URL."
    read -r -p "Type 'delete' to confirm: " a
    [[ "$a" == "delete" ]] || { echo "aborted."; exit 0; }
    # Versioning is enabled so a plain recursive delete would leave versions
    # and delete markers behind, causing CloudFormation bucket deletion to
    # fail. Empty the exact stack-owned bucket, including all versions.
    aws s3 rm "s3://$BUCKET" --recursive --region "$REGION"
    while :; do
      version_payload="$(aws s3api list-object-versions --bucket "$BUCKET" --region "$REGION" \
        --max-items 1000 --output json)"
      delete_payload="$(python3 -c 'import json,sys; d=json.load(sys.stdin); o=[{"Key":x["Key"],"VersionId":x["VersionId"]} for k in ("Versions","DeleteMarkers") for x in d.get(k,[])]; print(json.dumps({"Objects":o,"Quiet":True})) if o else None' <<<"$version_payload")"
      [ -n "$delete_payload" ] || break
      aws s3api delete-objects --bucket "$BUCKET" --region "$REGION" \
        --delete "$delete_payload" >/dev/null
    done
    aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
    echo "delete requested — watch progress in the CloudFormation console"
    ;;
  *) echo "usage: ./deploy.sh [deploy|update-site|upload-ws10-assets|upload-release-assets|preflight|refresh|watch [daily|seasonal]|teardown]"; exit 1 ;;
esac
