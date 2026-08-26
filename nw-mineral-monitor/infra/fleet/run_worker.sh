#!/bin/bash
cd /opt/ws13
if [ "$WS13_MODE" = confidence ]; then
  # WS13_SHARD is this PROCESS's index, not the node's: the node
  # owns slot WS13_NODE_SLOT and its WS13_WORKERS_PER_NODE
  # processes take the consecutive shards inside it, so every
  # process in the group divides by the same WS13_SHARD_COUNT and
  # the shard space is a partition rather than a set of guesses.
  #
  # The pass is SWEPT, not run once, because its exit code answers
  # "is this shard finished", not "did this process work":
  # ws13_confidence_pass.main() returns 0 only when the shard has
  # 0 pages left, and work routinely remains after a sweep
  # (run_shard() stops on lack of progress, and a document cut
  # short by its 3600 s deadline leaves pages behind -- the
  # 1,407-page document needs three deadlines). Handing that to
  # node_agent.sh as a failure would be read as a dead worker --
  # bad=8 of COUNT=8, node parked in the 6 h hold,
  # ws13-worker-exit-failures in ALARM -- on the most ordinary
  # outcome a first sweep has.
  #
  # THE OVERLOADED 1. That "work remains" answer used to BE exit 1,
  # which is also CPython's status for any uncaught exception. This
  # loop could not tell an ordinary first sweep from a process that
  # died on an unhandled error, so a crashing pass was re-run
  # immediately, with no backoff, for the full 24 h ceiling. The
  # pass now answers with distinct codes and 1 is left to mean what
  # the interpreter makes it mean:
  #   0  this shard is measured
  #   10 pages remain and this sweep measured some -> sweep again
  #   11 pages remain and this sweep measured NOTHING -> back off
  #   2  shard arithmetic outside 0..shards-1   } real failures,
  #   3  no docker or no page renderer          } passed through
  #   1  unhandled: retried, then treated as a failure after
  #      CONF_MAX_FAULTS CONSECUTIVE occurrences
  # Only 10, 11 and 1 are swept again. Anything else -- 12, or a
  # signal death like 137 (OOM) or 127 -- leaves the loop on its
  # FIRST occurrence and is handed to the node agent as-is, which
  # is deliberate: those say the environment is wrong, and a
  # second identical sweep learns nothing. (This comment used to
  # say "1 or anything else", which promised those codes three
  # retries they have never had.) 11 backs off geometrically
  # because a sweep that measured nothing will measure nothing
  # again a second later, and gives up after CONF_MAX_STALLS so a
  # wedged shard surfaces within minutes instead of at the 24 h
  # ceiling. Both counters are consecutive in fact as well as in
  # name: each arm zeroes the other's counter.
  #
  # Two other stops: the drain file, which reports 0 because being
  # asked to leave is not a failure (the agent then releases the
  # claim so another node re-measures the shard), and the
  # wall-clock ceiling, which leaves the last non-zero rc in place
  # so a shard that will not converge holds its node and raises the
  # alarm instead of sweeping for ever.
  SHARD=$(( WS13_NODE_SLOT * WS13_WORKERS_PER_NODE + $1 - 1 ))
  LOG=/var/log/ws13-confidence-"$1".log
  END=$(( $(date +%s) + WS13_SWEEP_MAX_SECONDS ))
  CONF_BACKOFF_START=60
  CONF_BACKOFF_MAX=900
  CONF_MAX_STALLS=6
  CONF_MAX_FAULTS=3
  backoff=$CONF_BACKOFF_START
  stalls=0
  faults=0
  rc=10
  while [ "$rc" -eq 10 ] || [ "$rc" -eq 11 ] || [ "$rc" -eq 1 ]; do
    if [ -f "$WS13_DRAIN_FILE" ]; then
      echo "drain requested; shard $SHARD left unfinished for another node" >> "$LOG"
      rc=0
      break
    fi
    if [ "$(date +%s)" -ge "$END" ]; then
      echo "shard $SHARD still unfinished after $WS13_SWEEP_MAX_SECONDS s; giving up (last rc=$rc)" >> "$LOG"
      break
    fi
    WS13_SHARD=$SHARD WS13_WORKER_ID="$(hostname)-$1" \
      python3 ws13_confidence_pass.py --limit "$WS13_SWEEP_DOCS" >> "$LOG" 2>&1
    rc=$?
    if [ "$rc" -eq 10 ]; then
      # Progress. Reset both counters and sweep straight on.
      backoff=$CONF_BACKOFF_START; stalls=0; faults=0
    elif [ "$rc" -eq 11 ]; then
      # Each counter is reset by ANY other outcome, not only by
      # rc=10. Resetting on 10 alone made neither count
      # consecutive: a shard alternating 11,1,11,1,11,1 reached
      # faults=3 with no two exit-1s adjacent, abandoned the
      # shard, and logged "3 times" as though it had crash-looped.
      faults=0
      stalls=$((stalls + 1))
      if [ "$stalls" -ge "$CONF_MAX_STALLS" ]; then
        echo "shard $SHARD measured nothing in $stalls consecutive sweeps; not sweeping again" >> "$LOG"
        break
      fi
      echo "shard $SHARD measured nothing; sleeping ${backoff}s before sweep $((stalls + 1))" >> "$LOG"
      sleep $backoff
      backoff=$((backoff * 2))
      [ "$backoff" -gt "$CONF_BACKOFF_MAX" ] && backoff=$CONF_BACKOFF_MAX
    elif [ "$rc" -eq 1 ]; then
      # An unhandled exception, now distinguishable. Retried a few
      # times because a torn database connection presents this way,
      # but never hot-looped and never for 24 h.
      stalls=0
      faults=$((faults + 1))
      if [ "$faults" -ge "$CONF_MAX_FAULTS" ]; then
        echo "shard $SHARD exited 1 (unhandled) $faults times; leaving it failed for the node agent" >> "$LOG"
        break
      fi
      echo "shard $SHARD exited 1 (unhandled); sleeping ${backoff}s" >> "$LOG"
      sleep $backoff
      backoff=$((backoff * 2))
      [ "$backoff" -gt "$CONF_BACKOFF_MAX" ] && backoff=$CONF_BACKOFF_MAX
    fi
  done
  echo "$rc" > /opt/ws13/status/"$1"
  exit 0
fi
WS13_WORKER_ID="$(hostname)-$1" python3 ws13_worker.py >> /var/log/ws13-worker-"$1".log 2>&1
echo $? > /opt/ws13/status/"$1"
