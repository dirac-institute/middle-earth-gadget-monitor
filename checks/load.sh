#!/bin/bash
# interval: 300
# timeout: 30
# Load averages from /proc/loadavg.
read -r l1 l5 l15 _ < /proc/loadavg
echo "node_load1 $l1"
echo "node_load5 $l5"
echo "node_load15 $l15"
