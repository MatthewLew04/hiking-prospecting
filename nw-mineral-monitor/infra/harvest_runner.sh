#!/bin/bash
# WS12 detached harvest runner. Runs on the EC2 runner instance from the
# uploaded bundle: resumes the crawl from the durable queue, heartbeats a
# status JSON to S3 every 10 minutes, uploads final state, and powers off
# (the instance terminates on shutdown).
set -u
BUCKET="$1"
PORTAL="${2:-igs_mines}"
RPREFIX="${3:-ws12/runner}"
PEER_FLAGS=""
if [ "${4:-}" = "peer" ]; then
  PEER_FLAGS="--node-name remote --peer-sync-prefix s3://$BUCKET/$RPREFIX/urlmap"
fi
cd /opt/ws12

( while true; do
    python3 status_snapshot.py > /tmp/status.json 2>/dev/null && \
      aws s3 cp /tmp/status.json "s3://$BUCKET/$RPREFIX/status.json" \
        --quiet || true
    sleep 600
  done ) &
HEARTBEAT=$!

attempts=0
until python3 pipelines/mine_file_harvest.py crawl --portal "$PORTAL" \
    --queue var/ws12/queue.sqlite3 --manifest var/ws12/manifest.jsonl \
    --coverage var/ws12/coverage.json --diff-dir var/ws12/diffs \
    --s3-bucket "$BUCKET" --s3-prefix ws12/originals \
    --admit-research-copies --s3-research-prefix ws12/research-copies \
    --admit-licensed-copies --s3-licensed-prefix ws12/licensed-copies \
    --claim-order asc $PEER_FLAGS \
    >> /var/log/ws12-crawl.log 2>&1; do
  attempts=$((attempts + 1))
  echo "crawl exited nonzero (attempt $attempts)" >> /var/log/ws12-crawl.log
  if [ "$attempts" -ge 20 ]; then
    echo "retry budget exhausted; uploading state and stopping" \
      >> /var/log/ws12-crawl.log
    break
  fi
  sleep 60
done

kill "$HEARTBEAT" 2>/dev/null || true
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('var/ws12/queue.sqlite3')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()
PY
python3 status_snapshot.py > /tmp/status.json 2>/dev/null || true
aws s3 cp /tmp/status.json "s3://$BUCKET/$RPREFIX/status.json" || true
aws s3 cp var/ws12/queue.sqlite3 "s3://$BUCKET/$RPREFIX/final/queue.sqlite3"
aws s3 cp var/ws12/manifest.jsonl "s3://$BUCKET/$RPREFIX/final/manifest.jsonl"
aws s3 cp var/ws12/coverage.json "s3://$BUCKET/$RPREFIX/final/coverage.json"
aws s3 sync var/ws12/diffs "s3://$BUCKET/$RPREFIX/final/diffs" --quiet || true
aws s3 cp /var/log/ws12-crawl.log "s3://$BUCKET/$RPREFIX/final/crawl.log" || true
shutdown -h now
