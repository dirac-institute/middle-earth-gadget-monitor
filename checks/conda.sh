#!/bin/bash
# interval: 300
# timeout: 210
# hosts: arnor gondor
# Conda probe (README.md, "Conda probe"): how long a user waits to get a conda
# environment's Python going, with the same environment on each location in
# ENVS. For each location, in a clean shell as a user would: activate the
# environment conda/setup.sh built there, start its python3, then import each
# package. Locations run one after another so they don't slow each other.
#
# init.sh reads ENVS and the first host above to build the environments, and
# gen_hosts_overview.py reads ENVS for its panels: keep one name=dir per line.
# The name is the `env` label (keep it stable); dir is the path on the hosts.
ENVS=(
    shire=/astro/store/shire/@Service/gadget/conda-probe
    shiren=/astro/store/shiren/gadget/conda-probe
    home=/astro/users/astro-gadget/conda-probe
)
# Imported one after another, each timed. Each must be in conda/environment.yml.
PACKAGES="lsdb"
# Seconds for one location before it counts as failed. The timeout above
# covers every location: 3 x (60 + 5 for timeout -k) + 15 to report.
ENV_TIMEOUT=60

# Runs in its own shell. Prints "<stage> [package] <seconds>" as each stage
# finishes, so a location that hangs still reports the stages before it.
# shellcheck disable=SC2016  # expanded by the inner shell
PROBE='
dir=$1; shift
env_dir=$dir/miniconda3/envs/gadget-probe
export CONDARC=$dir/miniconda3/.condarc
since() { local us=$(( ${EPOCHREALTIME/./} - ${1/./} )); printf "%d.%06d" $((us / 1000000)) $((us % 1000000)); }
t0=$EPOCHREALTIME
. "$dir/miniconda3/etc/profile.d/conda.sh" && conda activate "$env_dir" || exit 1
echo "activate $(since "$t0")"
t1=$EPOCHREALTIME
# The prefix proves python3 is the one in the environment, not the system one.
[ "$(python3 -c "import sys; print(sys.prefix)")" = "$env_dir" ] || exit 1
echo "python $(since "$t1")"
python3 -c "
import sys, time
for p in sys.argv[1:]:
    t = time.perf_counter()
    __import__(p)
    print(\"import\", p, round(time.perf_counter() - t, 6), flush=True)
" "$@" || exit 1
echo "total $(since "$t0")"
'

for pair in "${ENVS[@]}"; do
    name=${pair%%=*} dir=${pair#*=}
    # The agent feeds this script to bash on stdin: keep the probe off it.
    # shellcheck disable=SC2086  # PACKAGES is a list
    out=$(timeout -k 5 "$ENV_TIMEOUT" bash --noprofile --norc -c "$PROBE" probe "$dir" $PACKAGES </dev/null 2>/dev/null)
    ok=0
    while read -r stage a b; do
        [ -n "$stage" ] || continue
        case $stage in
            import) echo "gadget_conda_seconds{env=\"$name\",stage=\"import\",package=\"$a\"} $b" ;;
            total)  echo "gadget_conda_seconds{env=\"$name\",stage=\"total\"} $a"; ok=1 ;;
            *)      echo "gadget_conda_seconds{env=\"$name\",stage=\"$stage\"} $a" ;;
        esac
    done <<<"$out"
    echo "gadget_conda_success{env=\"$name\"} $ok"
done
exit 0
