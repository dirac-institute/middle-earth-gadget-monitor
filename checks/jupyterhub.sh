#!/bin/bash
# interval: 300
# timeout: 15
# JupyterHub processes on this host: the hub, its proxy, and running servers.
#
# The probe from the VM (README.md, "JupyterHubs") says whether the hub
# answers; this says which piece is missing when it doesn't, and how many
# single-user servers (one per user with a server started) are running.
# pgrep reads /proc; the bash running this script has "bash -s" as its
# command line, so it never matches itself.
count() { pgrep -c -f "$1"; }
echo "gadget_jupyterhub_processes{process=\"hub\"} $(count '/bin/jupyterhub( |$)')"
echo "gadget_jupyterhub_processes{process=\"proxy\"} $(count '/bin/configurable-http-proxy( |$)')"
echo "gadget_jupyterhub_processes{process=\"singleuser\"} $(count '/bin/jupyterhub-singleuser( |$)')"
exit 0
