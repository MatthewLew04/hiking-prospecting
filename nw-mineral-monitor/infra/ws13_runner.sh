#!/bin/bash
# WS13 runner bootstrap: install python deps (resiliently), run the named
# pipeline script, upload its log, and power off (instance terminates on
# shutdown). A dependency failure uploads logs and stops instead of running
# a partial job.
set -u
BUCKET="$1"
RPREFIX="$2"
SCRIPT="$3"
shift 3
cd /opt/ws13
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' 2>/dev/null || true)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
export AWS_DEFAULT_REGION="${REGION:-us-west-2}"

dnf install -y python3-pip python3-boto3 docker >> /var/log/ws13-setup.log 2>&1 || true
systemctl start docker >> /var/log/ws13-setup.log 2>&1 || true
for attempt in 1 2 3 4 5; do
  python3 -m pip install --quiet pypdf pyarrow \
    >> /var/log/ws13-setup.log 2>&1 && break
  echo "pip attempt $attempt failed; retrying" >> /var/log/ws13-setup.log
  sleep 20
done

if ! python3 -c 'import boto3, pypdf' 2>> /var/log/ws13-setup.log; then
  echo 'FATAL: python dependencies unavailable' >> /var/log/ws13-setup.log
  aws s3 cp /var/log/ws13-setup.log "s3://$BUCKET/$RPREFIX/final/setup.log" || true
  shutdown -h now
  exit 1
fi

python3 "$SCRIPT" "$@" >> /var/log/ws13-job.log 2>&1
STATUS=$?
echo "job exit: $STATUS" >> /var/log/ws13-job.log
aws s3 cp /var/log/ws13-job.log "s3://$BUCKET/$RPREFIX/final/job.log" || true
aws s3 cp /var/log/ws13-setup.log "s3://$BUCKET/$RPREFIX/final/setup.log" || true
shutdown -h now
