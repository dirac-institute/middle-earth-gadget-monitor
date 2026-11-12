#!/bin/bash
# interval: 300
# timeout: 30
# RDMA (RoCE/InfiniBand) port state and selected hardware error counters for
# every device and port, from /sys/class/infiniband. NFS to shire runs over
# RoCE; in the November 2026 outage these counters showed packet loss on that
# transport. A host without RDMA devices prints nothing.
#
# State numbers are the kernel's (as node_exporter's infiniband collector):
#   state           1 DOWN, 2 INIT, 3 ARMED, 4 ACTIVE, 5 ACTIVE_DEFER
#   physical state  2 Polling, 3 Disabled, 5 LinkUp, 7 LinkErrorRecovery, ...
# The counters are cumulative since the driver loaded; only a few of the ~35
# are kept, to bound the series count. The last five are congestion (RoCE
# ECN: CNPs this port sent and acted on, packets that arrived ECN-marked),
# receives dropped because no receive buffer was posted (out_of_buffer: the
# consumer, e.g. nfsd, fell behind), and requests a peer retried
# (duplicate_request).
shopt -s nullglob
counters="packet_seq_err out_of_sequence local_ack_timeout_err roce_adp_retrans
roce_slow_restart_cnps req_remote_access_errors req_cqe_error resp_cqe_error
np_cnp_sent rp_cnp_handled np_ecn_marked_roce_packets out_of_buffer duplicate_request"
for port in /sys/class/infiniband/*/ports/*; do
    dev="${port#/sys/class/infiniband/}"; dev="${dev%%/*}"
    l="device=\"$dev\",port=\"${port##*/}\""
    read -r state _ < "$port/state" && echo "node_infiniband_state_id{$l} ${state%:}"
    read -r phys _ < "$port/phys_state" && echo "node_infiniband_physical_state_id{$l} ${phys%:}"
    for c in $counters; do
        [ -r "$port/hw_counters/$c" ] || continue
        read -r v < "$port/hw_counters/$c" && echo "gadget_rdma_hw_counter_total{$l,counter=\"$c\"} $v"
    done
done
exit 0
