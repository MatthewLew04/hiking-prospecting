#!/bin/bash
# WS13 fleet status: discovers the data-plane stack, reports queue depth,
# live nodes/uptime, and (via SSM on one worker) manifest progress+throughput.
# Read-only. Writes the same report to nw-mineral-monitor/var/ws13-status.txt
set -u
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/var"
OUT="$ROOT/var/ws13-status.txt"
exec > >(tee "$OUT") 2>&1

echo "===== WS13 STATUS  $(date -u '+%Y-%m-%dT%H:%M:%SZ')  region=$REGION ====="

STACK=$(aws cloudformation describe-stacks --region "$REGION" \
  --query "Stacks[?Outputs[?OutputKey=='WorkQueueUrl']].StackName | [0]" \
  --output text 2>/dev/null)
echo "data-plane stack: ${STACK:-NONE}"
out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
    --output text 2>/dev/null
}
QURL=""; DLQ=""; DBEP=""; SECRET=""
if [ -n "${STACK:-}" ] && [ "$STACK" != "None" ]; then
  QURL=$(out WorkQueueUrl); DLQ=$(out DeadLetterQueueUrl)
  DBEP=$(out DbEndpoint);   SECRET=$(out DbSecretArn)
fi
[ -z "$QURL" ] || [ "$QURL" = "None" ] && QURL=$(aws sqs get-queue-url --region "$REGION" \
  --queue-name ws13-ocr-work --query QueueUrl --output text 2>/dev/null)
[ -z "$DLQ" ] || [ "$DLQ" = "None" ] && DLQ=$(aws sqs get-queue-url --region "$REGION" \
  --queue-name ws13-ocr-dlq --query QueueUrl --output text 2>/dev/null)

echo
echo "----- QUEUES -----"
for pair in "work:$QURL" "dlq:$DLQ"; do
  label="${pair%%:*}"; url="${pair#*:}"
  if [ -n "$url" ] && [ "$url" != "None" ]; then
    printf '%-5s %s\n' "$label" "$url"
    aws sqs get-queue-attributes --region "$REGION" --queue-url "$url" \
      --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
      --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' \
      --output text | awk -v l="$label" '{print "      "l" waiting="$1"  in-flight="$2}'
  else
    echo "$label: not found"
  fi
done

echo
echo "----- FLEET (ASG ws13-workers) -----"
aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --auto-scaling-group-names ws13-workers \
  --query 'AutoScalingGroups[0].[DesiredCapacity,MinSize,MaxSize]' --output text \
  | awk '{print "desired="$1" min="$2" max="$3}'
IDS=$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --auto-scaling-group-names ws13-workers \
  --query 'AutoScalingGroups[0].Instances[?LifecycleState==`InService`].InstanceId' \
  --output text 2>/dev/null)
echo "in-service: ${IDS:-none}"
if [ -n "${IDS:-}" ]; then
  aws ec2 describe-instances --region "$REGION" --instance-ids $IDS \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime,State.Name]' \
    --output text | while read -r id typ launched state; do
      up=$(python3 -c "import datetime as d,sys;t=d.datetime.fromisoformat(sys.argv[1].replace('Z','+00:00'));print(round((d.datetime.now(d.timezone.utc)-t).total_seconds()/3600,2))" "$launched" 2>/dev/null)
      echo "  $id $typ $state launched=$launched uptime_h=${up:-?}"
    done
fi

echo
echo "----- QUEUE DEPTH, LAST 24 h (CloudWatch hourly avg) -----"
QNAME="${QURL##*/}"
if [ -n "$QNAME" ] && [ "$QNAME" != "None" ]; then
  ST=$(python3 -c "import datetime as d;print((d.datetime.now(d.timezone.utc)-d.timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  EN=$(python3 -c "import datetime as d;print(d.datetime.now(d.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  aws cloudwatch get-metric-statistics --region "$REGION" --namespace AWS/SQS \
    --metric-name ApproximateNumberOfMessagesVisible \
    --dimensions Name=QueueName,Value="$QNAME" \
    --start-time "$ST" --end-time "$EN" --period 3600 --statistics Average \
    --query 'sort_by(Datapoints,&Timestamp)[].[Timestamp,Average]' --output text
fi

echo
echo "----- MANIFEST PROGRESS + THROUGHPUT (via SSM on a worker) -----"
NODE=$(echo "${IDS:-}" | awk '{print $1}')
if [ -z "$NODE" ] || [ -z "$SECRET" ] || [ "$SECRET" = "None" ]; then
  echo "skipped (need an in-service node and the DbSecretArn stack output)"
else
  REMOTE=$(cat <<'SQL'
set -u
PW=$(aws secretsmanager get-secret-value --secret-id __SECRET__ --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["password"])')
DSN="postgresql://nwmm:$PW@__DBEP__:5432/nwmm?sslmode=require"
echo "== by status =="
psql "$DSN" -X -A -F'|' -c "SELECT status, count(*) FROM ws13_manifest GROUP BY 1 ORDER BY 2 DESC;"
echo "== totals =="
psql "$DSN" -X -A -F'|' -c "SELECT count(*) AS total, count(*) FILTER (WHERE status='done') AS done, count(*) FILTER (WHERE status='error') AS err, sum(pages) AS pages, sum(chunks) AS chunks, min(updated_at) AS first_write, max(updated_at) AS last_write FROM ws13_manifest;"
echo "== per-hour, last 24 h =="
psql "$DSN" -X -A -F'|' -c "SELECT date_trunc('hour',updated_at) AS hr, count(*) AS docs, sum(pages) AS pages, round(avg(seconds)::numeric,1) AS avg_sec FROM ws13_manifest WHERE status='done' AND updated_at > now()-interval '24 hours' GROUP BY 1 ORDER BY 1;"
echo "== live in last 10 min =="
psql "$DSN" -X -A -F'|' -c "SELECT count(DISTINCT worker_id) AS live_workers, count(*) AS docs FROM ws13_manifest WHERE updated_at > now()-interval '10 minutes';"
SQL
)
  REMOTE="${REMOTE//__SECRET__/$SECRET}"
  REMOTE="${REMOTE//__DBEP__/$DBEP}"
  python3 -c 'import json,sys;print(json.dumps({"commands":[sys.argv[1]]}))' "$REMOTE" > /tmp/ws13-ssm.json
  CID=$(aws ssm send-command --region "$REGION" --instance-ids "$NODE" \
    --document-name AWS-RunShellScript --parameters file:///tmp/ws13-ssm.json \
    --query 'Command.CommandId' --output text 2>&1)
  echo "ssm node=$NODE command=$CID"
  case "$CID" in
    cmd-*|[0-9a-f]*-*)
      for i in $(seq 1 40); do
        S=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CID" \
              --instance-id "$NODE" --query Status --output text 2>/dev/null)
        case "$S" in Success|Failed|TimedOut|Cancelled) break;; esac
        sleep 5
      done
      echo "ssm status: ${S:-unknown}"
      aws ssm get-command-invocation --region "$REGION" --command-id "$CID" \
        --instance-id "$NODE" --query StandardOutputContent --output text
      aws ssm get-command-invocation --region "$REGION" --command-id "$CID" \
        --instance-id "$NODE" --query StandardErrorContent --output text | head -20
      ;;
    *) echo "send-command failed: $CID";;
  esac
fi
echo
echo "===== END  (saved to $OUT) ====="
