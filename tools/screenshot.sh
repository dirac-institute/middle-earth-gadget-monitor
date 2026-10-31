#!/bin/bash
# Screenshot a Grafana dashboard, or one panel of it, as a viewer sees it.
#
#   tools/screenshot.sh <dashboard-uid> [panel] [options]
#
#   tools/screenshot.sh gadget-hosts-overview                   # whole dashboard
#   tools/screenshot.sh gadget-hosts-overview "GPU util" --from now-6h
#   tools/screenshot.sh gadget-hosts-overview 16 --var host=gondor
#   tools/screenshot.sh gadget-users --admin
#
# panel is a panel id or a unique piece of its title. Options:
#   --from, --to      time range (default: the dashboard's)
#   --var NAME=V[,V]  template variable (a repeated panel shows the first value)
#   --width, --height viewport in px (default 1000x500 for a panel, 1600 wide
#                     and as tall as the dashboard otherwise)
#   --theme light     (default dark)
#   --tz ZONE         browser time zone (default America/Los_Angeles)
#   --admin           log in as admin; the Admin folder needs it
#   --out FILE        where to write the PNG (default /tmp/gadget-shots/...)
#
# Prints the PNG's path and how many times the page shows "No data".
# Runs headless Chromium in a throwaway container (built from
# tools/screenshot/ on first use, ~2.5 GB) on the host network, so it reaches
# Grafana on 127.0.0.1:8080. It only reads from Grafana.
set -euo pipefail

IMAGE=localhost/gadget-monitor-tools/screenshot
PW_FILE=/service/shire/gadget-monitor/secrets/grafana-admin-password
dir=$(cd "$(dirname "$0")/screenshot" && pwd)

[ $# -ge 1 ] || { sed -n '2,24s/^# \{0,1\}//p' "$0"; exit 2; }
args=() out="" auth=""
while [ $# -gt 0 ]; do
    case "$1" in
        --admin) auth="admin:$(sudo -u astro-gadget cat "$PW_FILE")" ;;
        --out) out=$2; shift ;;
        *) args+=("$1") ;;
    esac
    shift
done
if [ -z "$out" ]; then
    name=$(printf '%s' "${args[*]}" | tr -c 'A-Za-z0-9=._-' '_' | cut -c1-80)
    out=/tmp/gadget-shots/$name.png
fi
mkdir -p "$(dirname "$out")"

podman image exists "$IMAGE" || podman build -q -t "$IMAGE" "$dir" >&2

# The container writes into a fresh directory (relabelled for SELinux by :z)
# rather than wherever --out points.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# The password goes in through the environment, not the command line.
GRAFANA_AUTH=$auth podman run --rm --network host -e GRAFANA_AUTH \
    -v "$dir/shot.py:/shot.py:ro,z" -v "$tmp:/out:z" \
    "$IMAGE" python3 /shot.py "${args[@]}" --out /out/shot.png | sed "s|/out/shot.png|$out|"
mv "$tmp/shot.png" "$out"
