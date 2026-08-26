#!/bin/bash
# usage: claim_slot.sh claim      -> prints a slot index, or rc 1
#        claim_slot.sh incomplete -> how many slots are not
#                                    marked state=complete
#
# A slot is a key under ws13/fleet/confidence/claims. PutObject
# with If-None-Match '*' succeeds for exactly one caller per key,
# so the first key a node wins is its slot and no two nodes can
# win the same one. An unfinished claim that has gone
# ClaimStaleSeconds without a refresh is taken over
# unconditionally: two nodes can lose that race together and
# measure one shard twice, which costs CPU and rewrites nothing
# (the pass only fills rows where confidence IS NULL), while a
# skipped shard is pages nothing measures and nothing reports.
CLAIMS=ws13/fleet/confidence/claims
LOG=/var/log/ws13-claim.log
IID=$(cat /opt/ws13/claim 2>/dev/null)
LAST=$(( ${WS13_NODE_SLOTS} - 1 ))
head_of() {
  aws s3api head-object --bucket ${WS13_BUCKET} --key $CLAIMS/shard-"$1" --query '[LastModified, Metadata.state]' --output text 2>/dev/null
}
# cut, not `set -- $head`: that would overwrite this script's own
# positional parameters as a side effect of reading a header.
# Field 1 is LastModified, field 2 is the state ('None' when a
# claim predates the metadata).
state_of() { printf '%s' "$1" | cut -f2; }
# The lmsec=0 fallback is the direction to fail in. If
# LastModified ever fails to parse, epoch 0 makes the claim look
# ancient and it is taken over, which at worst duplicates a shard.
# The alternative -- an empty age, a comparison that errors, and a
# claim silently left alone -- would leave a dead node's pages
# unmeasured with nothing to show for it.
age_of() {
  lm=$(printf '%s' "$1" | cut -f1)
  lmsec=$(date -d "$lm" +%s 2>/dev/null)
  [ -n "$lmsec" ] || lmsec=0
  echo $(( $(date +%s) - lmsec ))
}
case "$1" in
claim)
  for i in $(seq 0 $LAST); do
    if aws s3api put-object --bucket ${WS13_BUCKET} --key $CLAIMS/shard-$i --body /opt/ws13/claim --metadata state=running,node=$IID --if-none-match '*' >> $LOG 2>&1; then
      echo "$i"; exit 0
    fi
  done
  for i in $(seq 0 $LAST); do
    head=$(head_of $i) || continue
    [ "$(state_of "$head")" = complete ] && continue
    age=$(age_of "$head")
    if [ "$age" -gt ${WS13_CLAIM_STALE} ]; then
      aws s3api put-object --bucket ${WS13_BUCKET} --key $CLAIMS/shard-$i --body /opt/ws13/claim --metadata state=running,node=$IID >> $LOG 2>&1
      echo "$(date -u +%FT%TZ) RECLAIMED shard slot $i: unrefreshed for ${age}s. If its original node is somehow alive, this shard is now being measured twice." >> $LOG
      echo "$i"; exit 0
    fi
  done
  exit 1;;
incomplete)
  # An ABSENT claim counts. A slot nobody ever claimed is exactly
  # the silent hole this count exists to expose: it has no object
  # in S3, so a scan that only looked at existing claims would
  # report the run finished.
  n=0
  for i in $(seq 0 $LAST); do
    head=$(head_of $i)
    if [ -z "$head" ]; then n=$((n + 1)); continue; fi
    [ "$(state_of "$head")" = complete ] && continue
    n=$((n + 1))
  done
  echo "$n";;
*)
  echo "usage: claim_slot.sh claim|incomplete" >&2; exit 2;;
esac
