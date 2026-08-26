#!/bin/bash
# Everything a WS13 node does after its bundle is unpacked.
#
# This file, and the four scripts beside it, used to live inside
# infra/ws13_fleet.yaml as heredocs in the LaunchTemplate UserData. They were
# moved out because that UserData had grown to ~30,000 bytes against EC2's
# 16,384-byte limit, so `aws cloudformation deploy` failed on the
# LaunchTemplate and rolled the stack back before a node ever booted --
# FleetMode: confidence had never been deployable, from the commit that
# introduced it. The node already downloads ws13/fleet/bundle.tar.gz; carrying
# the shell in it costs nothing and removes the ceiling entirely.
#
# The parameters !Sub used to interpolate arrive as environment variables
# instead, exported by the ~40 lines of UserData that remain:
#
#   WS13_BUCKET WS13_QUEUE_URL WS13_DB_DSN WS13_MODE WS13_FLEET_NAME
#   WS13_NODE_SLOTS WS13_CLAIM_STALE WS13_WORKERS_PER_NODE WS13_SWEEP_DOCS
#   WS13_SWEEP_MAX_SECONDS
#
# The consequence to know: the shell is now versioned with the BUNDLE, not
# with the CloudFormation stack. A stack update no longer changes what a node
# runs -- rebuilding and uploading the bundle does. tools/build_ws13_bundle.py
# prints a sha256 per member for exactly that reason, and untars
# bundle_manifest.json beside this file so a node can say which version it is.
set -x
if [ "$WS13_MODE" = confidence ]; then
# The seeder below enqueues the corpus. Running it in this mode
# would put documents on the OCR queue for a pass that reads no
# queue, and would re-OCR the very text the measurement exists to
# judge -- the one conflation this workstream cannot afford. Claim
# a shard slot instead.
#
# The Auto Scaling group hands an instance no ordinal, and
# WS13_SHARD must be distinct per node or the pass measures some
# pages twice and never touches others. The slot is claimed with
# the same S3 conditional put the seed lock above uses: PutObject
# with If-None-Match '*' succeeds for exactly one caller per key,
# so the first key this node wins is its slot and no two nodes can
# win the same one. Deriving the ordinal from the group's instance
# list was the alternative and is worse: that list changes as nodes
# launch and terminate, so two nodes reading it seconds apart
# disagree about both their own index AND the total, which
# renumbers the shard space mid-run without saying so.
#
# FAILURE MODE WHEN A NODE IS REPLACED MID-RUN, stated plainly.
# The node agent refreshes this claim every 300 s and DELETES it if
# the node retires with the shard unfinished, so an orderly
# replacement hands the slot straight back. A node that dies hard
# (kernel panic, an ASG health-check replacement) deletes nothing,
# and its claim would otherwise sit there for good: that shard's
# pages stay NULL, every other shard reports complete, and the run
# looks finished. claim_slot.sh takes over a claim that has gone
# ClaimStaleSeconds without a refresh.
#
# WHY THIS WAITS INSTEAD OF RETIRING. A replacement for a
# hard-dead node arrives in a couple of minutes; its predecessor's
# claim does not go stale for ClaimStaleSeconds (1800 s). So the
# replacement used to find every slot taken, retire itself WITH
# --should-decrement-desired-capacity, and permanently remove the
# only capacity that could ever have reclaimed the dead node's
# shard. Decrementing is a promise that this node is surplus, and
# inside the staleness window that promise cannot be made: a claim
# refreshed 30 s ago and a claim abandoned 30 s ago look identical.
# After ClaimStaleSeconds plus one refresh interval they do not --
# anything still unfinished has been refreshed inside the window,
# which means a live node is holding it and this node really is
# surplus. So: wait that long, re-attempting the claim, and only
# then give the capacity back.
  CTOK=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
  IID=$(curl -s -H "X-aws-ec2-metadata-token: $CTOK" http://169.254.169.254/latest/meta-data/instance-id)
  CLAIMS=ws13/fleet/confidence/claims
  echo "$IID" > /opt/ws13/claim
  CLAIM_WAIT=$(( ${WS13_CLAIM_STALE} + 300 ))
  CLAIM_DEADLINE=$(( $(date +%s) + CLAIM_WAIT ))
  SLOT=""
  while : ; do
    SLOT=$(/opt/ws13/claim_slot.sh claim)
    [ -n "$SLOT" ] && break
    unfinished=$(/opt/ws13/claim_slot.sh incomplete)
    if [ "$unfinished" -eq 0 ]; then
      # Every slot is marked complete. The run is over, this node
      # is surplus, and giving the capacity back is correct.
      echo "every one of ${WS13_NODE_SLOTS} shard slots is complete; nothing to measure" >> /var/log/ws13-setup.log
      break
    fi
    if [ "$(date +%s)" -ge "$CLAIM_DEADLINE" ]; then
      # Stated as an observation, not as a conclusion. head_of
      # cannot tell "no such object" from "could not ask" -- a
      # throttled or failing s3api call takes the same branch as
      # an absent claim -- so a node that never once reached S3
      # would otherwise retire announcing that live nodes hold
      # every slot, which it has no evidence for.
      echo "waited ${CLAIM_WAIT}s and claimed nothing; ${unfinished} slot(s) read as unfinished and none went stale. Either live nodes hold them, or this node could not read the claim store -- see /var/log/ws13-claim.log, uploaded beside this file." >> /var/log/ws13-setup.log
      break
    fi
    echo "no free shard slot yet; ${unfinished} of ${WS13_NODE_SLOTS} unfinished. Holding this node until one goes stale rather than decrementing the group." >> /var/log/ws13-setup.log
    sleep 60
  done
  if [ -z "$SLOT" ]; then
    # Every slot is complete, or every unfinished one is held by a
    # node that is demonstrably alive. Retire the way a drained
    # node does -- terminate AND decrement -- and retry it, because
    # the call is rejected while the instance is still Pending and
    # the fallback below does NOT decrement: `shutdown` alone
    # leaves DesiredCapacity untouched, the group launches a
    # replacement, and the replacement reaches this same line. That
    # is the boot/die/replace loop the hold logic further down
    # exists to prevent.
    echo "no claimable confidence shard slot of ${WS13_NODE_SLOTS} after waiting ${CLAIM_WAIT}s; retiring" >> /var/log/ws13-setup.log
    # Both logs. ws13-claim.log holds every s3api error this node
    # hit while trying to claim, and it is the only record of WHY
    # it could not -- node_agent.sh's upload_logs, which sweeps
    # every ws13-* file, is never reached on this path.
    aws s3 cp /var/log/ws13-setup.log s3://${WS13_BUCKET}/ws13/fleet/logs/"$(hostname)"/ws13-setup.log
    aws s3 cp /var/log/ws13-claim.log s3://${WS13_BUCKET}/ws13/fleet/logs/"$(hostname)"/ws13-claim.log
    for a in 1 2 3 4 5 6; do
      aws autoscaling terminate-instance-in-auto-scaling-group --instance-id "$IID" --should-decrement-desired-capacity >> /var/log/ws13-setup.log 2>&1 && exit 0
      sleep 30
    done
    shutdown -h now
    exit 0
  fi
  export WS13_NODE_SLOT=$SLOT WS13_CLAIM_KEY=$CLAIMS/shard-$SLOT
  export WS13_WORKERS_PER_NODE=${WS13_WORKERS_PER_NODE}
  export WS13_SHARD_COUNT=$(( ${WS13_NODE_SLOTS} * ${WS13_WORKERS_PER_NODE} ))
  export WS13_SWEEP_DOCS=${WS13_SWEEP_DOCS}
  export WS13_SWEEP_MAX_SECONDS=${WS13_SWEEP_MAX_SECONDS}
  # ws13_worker.py's own default for the drain file, restated so the
  # confidence wrapper and the node agent share one contract
  # instead of each knowing a default.
  export WS13_DRAIN_FILE=/opt/ws13/drain
  echo "confidence shard slot $SLOT of ${WS13_NODE_SLOTS}; shards $(( SLOT * ${WS13_WORKERS_PER_NODE} ))-$(( SLOT * ${WS13_WORKERS_PER_NODE} + ${WS13_WORKERS_PER_NODE} - 1 )) of $WS13_SHARD_COUNT" >> /var/log/ws13-setup.log
else
# exactly one node seeds: an SQS-backed lock via S3 conditional put
touch /opt/ws13/empty; if aws s3api put-object --bucket ${WS13_BUCKET} --key ws13/fleet/seed.lock --body /opt/ws13/empty --if-none-match '*' >> /var/log/ws13-setup.log 2>&1; then
  aws s3 cp s3://${WS13_BUCKET}/ws13/fleet/manifest.jsonl var/ws12/manifest.jsonl >> /var/log/ws13-setup.log 2>&1 || true
  python3 ws13_seed.py --bucket ${WS13_BUCKET} --queue-url ${WS13_QUEUE_URL} --dsn "$WS13_DB_DSN" >> /var/log/ws13-seed.log 2>&1
  aws s3 cp /var/log/ws13-seed.log s3://${WS13_BUCKET}/ws13/fleet/seed.log
else
  # wait for the seeder to finish schema init before workers start
  until aws s3 ls s3://${WS13_BUCKET}/ws13/fleet/seed.log >/dev/null 2>&1; do sleep 15; done
fi
fi
# Each worker runs under a wrapper that records its EXIT STATUS.
# "all workers gone" is not one condition but two -- the queue
# drained (exit 0) or the process died at startup (non-zero, e.g.
# the pip install above exhausted its retries, bundle.tar.gz is
# stale, or WS13_BUCKET/WS13_QUEUE_URL/WS13_DB_DSN are unset) --
# and the node must not treat the second as the first.
# start_workers.sh is a separate file, not a loop inline here,
# because the node agent starts workers too: in confidence mode a
# node that finishes its own shard adopts an orphaned slot rather
# than retiring, and that means launching a second generation with
# the same contract -- same count, same status directory, same pid
# file. Two copies of that would be two contracts.
/opt/ws13/start_workers.sh
# The agent reads the pids from /opt/ws13/worker.pids rather than
# from argv, so a generation it starts itself is watched too.
nohup /opt/ws13/node_agent.sh "$WS13_WORKERS_PER_NODE" >/dev/null 2>&1 &
