#!/bin/bash
# One loop that owns this node's whole life: wait for the workers,
# keep the queue's CloudWatch series alive, honour a scale-in
# drain, then classify the exit and either retire the node, adopt
# another shard slot, or hold it for diagnosis.
# usage: node_agent.sh <worker count>
#
# The watcher this replaces looped on 'pgrep -f ws13_worker.py',
# which also matched its own command line, so the condition was
# never false: no node ever uploaded a log or scaled itself in.
#
# The pids are read from /opt/ws13/worker.pids rather than taken
# on argv, because in confidence mode this agent can start a
# SECOND generation of workers -- see the adopt step below -- and
# a pid list frozen at boot would then be watching processes that
# have already exited.
COUNT=$1; shift
STATUS=/opt/ws13/status
exec >> /var/log/ws13-node-agent.log 2>&1
say() { echo "$(date -u +%FT%TZ) $*"; }
imds() {
  tok=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -s -H "X-aws-ec2-metadata-token: $tok" http://169.254.169.254/latest/meta-data/"$1"
}
IID=$(imds instance-id)
# The NODE's clock, set once and never reset. GEN_BOOT below is
# the current generation's, and the two must not be the same
# variable: the "everything died immediately, so this is a bad
# bundle" heuristic asks about the node's life, and reading a
# generation clock there parked a node that had already measured
# a whole shard in the 6 h hold over one worker failing ten
# seconds after it adopted the next slot.
AGENT_BOOT=$(date +%s)
GEN_BOOT=$AGENT_BOOT
# Inherited from the UserData exports. Defaulted anyway, so an
# agent started by hand on an older node behaves like the ocr
# fleet rather than dereferencing an empty claim key.
MODE="$WS13_MODE"
[ -n "$MODE" ] || MODE=ocr
say "agent up: $COUNT workers, instance $IID, mode $MODE"
drain=0
# Outer loop: one iteration per generation of workers. In ocr mode
# it runs exactly once. In confidence mode a node that has finished
# its own shard cleanly may adopt an orphaned slot and come round
# again, which is what stops the group decrementing to zero with a
# shard nobody ever measured.
while :; do
PIDS=$(cat /opt/ws13/worker.pids 2>/dev/null)
say "watching pids $PIDS for slot ${WS13_NODE_SLOT:-none}"
tick=0
while :; do
  live=0
  for p in $PIDS; do
    if kill -0 "$p" 2>/dev/null; then live=$((live + 1)); fi
  done
  finished=$(ls -1 $STATUS 2>/dev/null | wc -l | tr -d ' ')
  # Two independent proofs, so a recycled PID cannot hang the node
  # and a SIGKILLed worker cannot hide: every worker wrote a
  # status, or nothing is alive any more.
  # This is the completion condition in BOTH modes, on purpose:
  # it asks about the processes and never about the queue. An ocr
  # worker exits when the queue drains; a confidence process exits
  # when its shard is measured, and "the queue is empty" would be
  # true from its first second -- a loop waiting on that would
  # never end. Either way the node is finished when its processes
  # are. The queue read below is only a CloudWatch keepalive and
  # has never been what ends this loop.
  if [ "$finished" -ge "$COUNT" ] || [ "$live" -eq 0 ]; then break; fi
  # One cheap call a minute keeps the work queue "active" so
  # CloudWatch keeps publishing AWS/SQS metrics. Those series stop
  # after six hours with no messages AND no API calls -- exactly
  # the state ws13-fleet-idle exists to detect, which is why that
  # alarm sat in INSUFFICIENT_DATA (TreatMissingData notBreaching,
  # so: silent) through 53.78 h of four idle c7g.2xlarge nodes.
  if [ "$MODE" = ocr ] && [ $((tick % 3)) -eq 0 ]; then
    aws sqs get-queue-attributes --queue-url ${WS13_QUEUE_URL} --attribute-names ApproximateNumberOfMessages > /dev/null 2>&1
  fi
  # confidence mode has no queue to keep alive -- it is empty for
  # the whole run by design. Two things take its place every 300 s.
  # The shard claim is refreshed, which is what stops another node
  # from deciding this one is dead and re-measuring its shard. And
  # ShardHeld=1 is published, which is what ws13-fleet-idle reads
  # in this mode instead of a queue depth that would call every
  # working node idle.
  if [ "$MODE" = confidence ] && [ $((tick % 15)) -eq 0 ]; then
    aws s3api put-object --bucket ${WS13_BUCKET} --key "$WS13_CLAIM_KEY" --body /opt/ws13/claim --metadata state=running,node="$IID" > /dev/null 2>&1
    aws cloudwatch put-metric-data --namespace WS13/Fleet --metric-name ShardHeld --unit Count --value 1 --dimensions AutoScalingGroupName=${WS13_FLEET_NAME} > /dev/null 2>&1
  fi
  if [ "$drain" -eq 0 ] && [ "$(imds autoscaling/target-lifecycle-state)" = Terminated ]; then
    drain=1
    touch /opt/ws13/drain
    say "scale-in requested: workers will finish the document in hand and exit"
  fi
  # Each heartbeat resets the hook's timeout, so a node draining a
  # long document is not cut off at HeartbeatTimeout.
  # ocr only, and that is what the heartbeat is for: a node
  # draining a 1,407-page document must not be cut off at
  # HeartbeatTimeout. In confidence mode the unit of work is one
  # sweep of ConfidenceSweepDocs documents and the wrapper checks
  # the drain file between sweeps -- so a draining node leaves in
  # one sweep (~7 min at the batched 1.6 s/page seed, the same
  # number the ConfidenceSweepDocs description gives) plus, if
  # the wrapper is mid-backoff on a stalled shard, up to
  # CONF_BACKOFF_MAX (900 s) before it looks at the drain file at
  # all: ~22 min worst case. This comment said "about half an
  # hour", which was the old 5-8 s/page figure that description
  # no longer uses. Either way it is far inside the hook's 7200 s,
  # and extending the hook indefinitely here would only let a
  # wedged process hold a node for ever. The hook
  # itself is untouched -- its 7200 s HeartbeatTimeout and
  # DefaultResult CONTINUE stay the ceiling, and a node terminated
  # that way leaves a claim that goes stale and is reclaimed, so
  # the shard is re-measured rather than lost.
  if [ "$MODE" = ocr ] && [ "$drain" -eq 1 ] && [ $((tick % 30)) -eq 0 ]; then
    aws autoscaling record-lifecycle-action-heartbeat --auto-scaling-group-name ${WS13_FLEET_NAME} --lifecycle-hook-name ws13-drain --instance-id "$IID" || true
  fi
  tick=$((tick + 1))
  sleep 20
done
failures=0
for f in $STATUS/*; do
  [ -f "$f" ] || continue
  if [ "$(cat "$f")" != 0 ]; then failures=$((failures + 1)); fi
  echo "worker $(basename "$f") exit=$(cat "$f")" >> /var/log/ws13-worker-exit-status.txt
done
finished=$(ls -1 $STATUS 2>/dev/null | wc -l | tr -d ' ')
bad=$((failures + COUNT - finished))
say "workers done: $finished/$COUNT reported, $bad bad, drain=$drain"
upload_logs() { aws s3 cp /var/log/ s3://${WS13_BUCKET}/ws13/fleet/logs/"$(hostname)"/ --recursive --exclude "*" --include "ws13-*"; }
upload_logs
aws cloudwatch put-metric-data --namespace WS13/Fleet --metric-name WorkerExitFailures --unit Count --value $bad --dimensions AutoScalingGroupName=${WS13_FLEET_NAME}
# Give the shard slot back, or mark it done. Marking complete stops
# a node that boots later from paying to re-walk a shard that is
# already measured; deleting is what makes an unfinished shard
# claimable again. Anything that is not a clean finish releases,
# because an unreleased unfinished shard is unmeasured pages that
# no log, metric or alarm would ever mention.
if [ "$MODE" = confidence ]; then
  # Both calls are RETRIED and their status is CHECKED. Neither
  # used to be, and the log said "complete" or "RELEASED" either
  # way -- an assertion about the claim store made without
  # reading the answer. The failure that matters is the delete: a
  # released slot is ABSENT, which the next node's conditional
  # put wins immediately, while a slot left at state=running with
  # a fresh timestamp is untouchable for another
  # ClaimStaleSeconds. So a failed delete silently converts "free
  # now" into "free in half an hour" while the log claims the
  # first.
  claim_write() {
    for a in 1 2 3; do
      if "$@" > /dev/null 2>&1; then return 0; fi
      sleep 5
    done
    return 1
  }
  if [ "$drain" -eq 0 ] && [ "$bad" -eq 0 ]; then
    if claim_write aws s3api put-object --bucket ${WS13_BUCKET} --key "$WS13_CLAIM_KEY" --body /opt/ws13/claim --metadata state=complete,node="$IID"; then
      say "shard slot $WS13_NODE_SLOT complete"
    else
      say "WARNING: shard slot $WS13_NODE_SLOT IS measured but could not be marked complete; it stays state=running and will read as incomplete in SlotsIncomplete and to any node that inspects it. Re-run --verify-complete before believing this slot is unmeasured."
    fi
  else
    if claim_write aws s3api delete-object --bucket ${WS13_BUCKET} --key "$WS13_CLAIM_KEY"; then
      say "shard slot $WS13_NODE_SLOT RELEASED unfinished (drain=$drain bad=$bad); its remaining pages stay NULL until a node reclaims the slot"
    else
      say "WARNING: could not release shard slot $WS13_NODE_SLOT (drain=$drain bad=$bad); the claim is still present, so no node can take it for another ${WS13_CLAIM_STALE}s rather than immediately"
    fi
  fi
  # ADOPT BEFORE RETIRING. Nothing used to assert that every slot
  # reached 'complete': once the group decremented to zero, "the
  # run finished" and "the run finished with slots nobody ever
  # claimed" were the same observable -- the same silent-hole
  # defect as the rc=127 that left 760,059 pages unmeasured while
  # every log said success. A node that has finished its own shard
  # is the one piece of capacity in the account that is provably
  # able to measure another, so before it hands that capacity back
  # it looks for a slot that is unclaimed or whose holder has gone
  # ClaimStaleSeconds without a refresh, and takes it.
  #
  # This is what makes the group's decrement to zero mean
  # something: the last node out has, by then, found no claimable
  # slot at all.
  # AND IT WAITS, for the same reason the boot-time claim loop
  # waits. One attempt was not enough: inside ClaimStaleSeconds a
  # claim refreshed 30 s ago and a claim abandoned 30 s ago are
  # the same object, so a single miss here fell through to a
  # --should-decrement-desired-capacity that promises this node
  # is surplus -- the exact blocker the boot-time wait was added
  # to remove, replicated at the other end of the node's life,
  # and worse here because this node has already proved it can
  # measure a shard. Same deadline, same 60 s poll, and the same
  # early exit when nothing is left unfinished.
  if [ "$drain" -eq 0 ] && [ "$bad" -eq 0 ]; then
    NEXT=""
    ADOPT_DEADLINE=$(( $(date +%s) + ${WS13_CLAIM_STALE} + 300 ))
    while : ; do
      NEXT=$(/opt/ws13/claim_slot.sh claim)
      [ -n "$NEXT" ] && break
      left=$(/opt/ws13/claim_slot.sh incomplete)
      if [ "$left" -eq 0 ]; then
        say "every one of ${WS13_NODE_SLOTS} slots is complete; nothing left to adopt"
        break
      fi
      if [ "$(date +%s)" -ge "$ADOPT_DEADLINE" ]; then
        say "waited out ClaimStaleSeconds+300 with ${left} slot(s) unfinished: every one was refreshed inside the window, so a live node holds it and this node really is surplus"
        break
      fi
      say "no adoptable slot yet (${left} unfinished); holding this node rather than decrementing the group"
      sleep 60
    done
    if [ -n "$NEXT" ]; then
      export WS13_NODE_SLOT=$NEXT
      export WS13_CLAIM_KEY=ws13/fleet/confidence/claims/shard-$NEXT
      say "ADOPTED shard slot $NEXT: it was unclaimed or its holder stopped refreshing. Measuring it rather than decrementing the group."
      /opt/ws13/start_workers.sh
      # Generation clock only. AGENT_BOOT, set once before the
      # outer loop, is what the 'everything died immediately'
      # heuristic reads -- resetting THAT here parked a node that
      # had already driven a shard to complete in the 6 h hold
      # over one partial failure ten seconds into generation 2.
      GEN_BOOT=$(date +%s)
      continue
    fi
  fi
  # Nothing claimable. Publish what is still outstanding, so the
  # difference between "finished" and "stopped" is a number an
  # alarm can read rather than an inference from an empty group.
  incomplete=$(/opt/ws13/claim_slot.sh incomplete)
  aws cloudwatch put-metric-data --namespace WS13/Fleet --metric-name SlotsIncomplete --unit Count --value "$incomplete" --dimensions AutoScalingGroupName=${WS13_FLEET_NAME} > /dev/null 2>&1
  if [ "$incomplete" -gt 0 ]; then
    say "WARNING: retiring with $incomplete of ${WS13_NODE_SLOTS} shard slots not complete. Their claims are being refreshed by other nodes, or this node is draining/failed. Confirm the run with: ws13_confidence_pass.py --verify-complete --shards $(( ${WS13_NODE_SLOTS} * ${WS13_WORKERS_PER_NODE} ))"
  else
    say "all ${WS13_NODE_SLOTS} shard slots complete"
  fi
fi
break
done
if [ "$drain" -eq 1 ]; then
  aws autoscaling complete-lifecycle-action --auto-scaling-group-name ${WS13_FLEET_NAME} --lifecycle-hook-name ws13-drain --instance-id "$IID" --lifecycle-action-result CONTINUE
  exit 0
fi
ran=$(( $(date +%s) - AGENT_BOOT ))
# Nothing survived, or everything died immediately: that is a bad
# bundle / bad environment, not a drained queue. Retiring here
# would let the still-full backlog request capacity again at once
# and 40 nodes would boot, die and retire in a loop with nothing
# indexed. Hold the node instead -- republishing the failure
# metric so the alarm STAYS in ALARM rather than blinking once --
# and give up after 21600 s (6 h), so a failure nobody looks at
# costs a node-day at most, not a c7g.4xlarge forever. A PARTIAL
# failure after real work is different: the metric is published,
# the exit statuses go to S3, and the node still retires.
if [ "$bad" -ge "$COUNT" ] || { [ "$bad" -gt 0 ] && [ "$ran" -lt 300 ]; }; then
  say "HELD after ${ran}s: $bad of $COUNT workers exited non-zero; see /var/log/ws13-worker-*.log"
  held=0
  while [ "$held" -lt 21600 ]; do
    sleep 300
    held=$((held + 300))
    aws cloudwatch put-metric-data --namespace WS13/Fleet --metric-name WorkerExitFailures --unit Count --value $bad --dimensions AutoScalingGroupName=${WS13_FLEET_NAME}
    # A held node is still in service and still idle, so keep the
    # queue series alive for ws13-fleet-idle, and leave at once if
    # the group asks for the node back -- there is nothing left to
    # drain.
    if [ "$MODE" = ocr ]; then
      aws sqs get-queue-attributes --queue-url ${WS13_QUEUE_URL} --attribute-names ApproximateNumberOfMessages > /dev/null 2>&1
    else
      # A held node in confidence mode is in service and measuring
      # nothing, which is exactly what ws13-fleet-idle exists to
      # catch here. Its claim was released above, so this publishes
      # 0 rather than pretending to still hold a shard.
      aws cloudwatch put-metric-data --namespace WS13/Fleet --metric-name ShardHeld --unit Count --value 0 --dimensions AutoScalingGroupName=${WS13_FLEET_NAME} > /dev/null 2>&1
    fi
    if [ "$(imds autoscaling/target-lifecycle-state)" = Terminated ]; then
      upload_logs
      aws autoscaling complete-lifecycle-action --auto-scaling-group-name ${WS13_FLEET_NAME} --lifecycle-hook-name ws13-drain --instance-id "$IID" --lifecycle-action-result CONTINUE
      exit 0
    fi
  done
  say "hold expired; retiring the node"
  upload_logs
fi
# Queue drained. Leave the group properly: terminate AND decrement
# DesiredCapacity, or the group just launches a replacement.
aws autoscaling terminate-instance-in-auto-scaling-group --instance-id "$IID" --should-decrement-desired-capacity || shutdown -h now
