#!/bin/bash
# Launch this node's worker processes and record their pids.
# The status directory is CLEARED first: node_agent.sh reads
# "every worker wrote a status" as the completion condition, so a
# second generation starting with the first generation's status
# files would be seen as finished before it had run.
rm -rf /opt/ws13/status
mkdir -p /opt/ws13/status
pids=""
for i in $(seq 1 ${WS13_WORKERS_PER_NODE}); do
  nohup /opt/ws13/run_worker.sh $i >/dev/null 2>&1 &
  pids="$pids $!"
done
echo "$pids" > /opt/ws13/worker.pids
