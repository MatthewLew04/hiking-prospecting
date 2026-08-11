#!/usr/bin/env bash
# NW Mineral Monitor — one-shot AWS deploy.
# Usage:
#   ./deploy.sh                # full deploy (stack + lambda code + site upload + first refresh)
#   ./deploy.sh update-site    # re-upload site/ only
#   ./deploy.sh upload-ws10-assets  # upload ignored quad rasters/tiles (never deletes remote assets)
#   ./deploy.sh refresh        # trigger the claims updater now
#   ./deploy.sh teardown       # delete everything (empties the bucket first)
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
STACK="${STACK_NAME:-nw-mineral-monitor}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SITE="$HERE/../site"
WS10_ASSETS="$HERE/../pipelines/cache/ws10/assets"
WS10_ASSET_PREFIX="ws10-assets"

need() { command -v "$1" >/dev/null || { echo "ERROR: '$1' not found — see DEPLOY.md prerequisites"; exit 1; }; }
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
    echo "==> [1/5] Creating/updating CloudFormation stack '$STACK' in $REGION (5-10 min first time)…"
    wait_for_clear_state
    aws cloudformation deploy --template-file "$HERE/template.yaml" \
      --stack-name "$STACK" --region "$REGION" --capabilities CAPABILITY_IAM \
      --no-fail-on-empty-changeset

    BUCKET="$(outputs BucketName)"; FN="$(outputs UpdaterFunctionName)"; URL="$(outputs SiteURL)"
    echo "    bucket: $BUCKET"; echo "    lambda: $FN"

    echo "==> [2/5] Uploading Lambda code (updater + AI relay)…"
    ( cd "$HERE" && rm -f updater.zip && cp lambda_updater.py index.py && zip -q updater.zip index.py && rm index.py )
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
      ( cd "$HERE" && rm -f watch.zip && cp watch_lambda.py index.py && zip -q watch.zip index.py && rm index.py )
      aws lambda update-function-code --function-name "$WATCHFN" --region "$REGION" \
        --zip-file "fileb://$HERE/watch.zip" >/dev/null
      rm -f "$HERE/watch.zip"
      echo "    expiration watch deployed: $WATCHFN (daily + Aug25-Sep10 6h window)"
    fi

    echo "==> [3/5] Uploading site ($(du -sh "$SITE" | cut -f1)) + enabling Cognito login…"
    aws s3 sync "$SITE" "s3://$BUCKET" --region "$REGION" --delete \
      --cache-control "public, max-age=3600" --exclude "data/*" --exclude "auth.json" --exclude "index.html" \
      --exclude "ckpt/*" --exclude "watch/*" --exclude "$WS10_ASSET_PREFIX/*" # runtime/S3-only state
    aws s3 cp "$SITE/index.html" "s3://$BUCKET/index.html" --region "$REGION" \
      --cache-control "public, max-age=60" --content-type "text/html"
    aws s3 sync "$SITE/data" "s3://$BUCKET/data" --region "$REGION" --delete \
      --exclude "alerts/*" --exclude "$WS10_ASSET_PREFIX/*" \
      --cache-control "public, max-age=900"

    POOL="$(outputs UserPoolId)"; CLIENT="$(outputs UserPoolClientId)"; ASKURL="$(outputs AskUrl)"
    printf '{"region":"%s","clientId":"%s","askUrl":"%s"}' "$REGION" "$CLIENT" "$ASKURL" > /tmp/auth.json
    aws s3 cp /tmp/auth.json "s3://$BUCKET/auth.json" --region "$REGION" \
      --cache-control "public, max-age=300" --content-type "application/json"
    aws cognito-idp admin-set-user-password --region "$REGION" \
      --user-pool-id "$POOL" --username "${COGNITO_USER:-codyClinger}" \
      --password "${COGNITO_PASS:-testing123}" --permanent
    echo "    login enabled: user '${COGNITO_USER:-codyClinger}' (pool $POOL)"

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
    BUCKET="$(outputs BucketName)"
    aws s3 sync "$SITE" "s3://$BUCKET" --region "$REGION" --delete \
      --cache-control "public, max-age=3600" --exclude "data/*" --exclude "auth.json" --exclude "index.html" \
      --exclude "ckpt/*" --exclude "watch/*" --exclude "$WS10_ASSET_PREFIX/*" # runtime/S3-only state
    aws s3 cp "$SITE/index.html" "s3://$BUCKET/index.html" --region "$REGION" \
      --cache-control "public, max-age=60" --content-type "text/html"
    aws s3 sync "$SITE/data" "s3://$BUCKET/data" --region "$REGION" --delete \
      --exclude "alerts/*" --exclude "$WS10_ASSET_PREFIX/*" \
      --cache-control "public, max-age=900"
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
    aws s3 rm "s3://$BUCKET" --recursive --region "$REGION"
    aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
    echo "delete requested — watch progress in the CloudFormation console"
    ;;
  *) echo "usage: ./deploy.sh [deploy|update-site|upload-ws10-assets|refresh|watch [daily|seasonal]|teardown]"; exit 1 ;;
esac
