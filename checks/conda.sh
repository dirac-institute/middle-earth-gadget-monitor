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
# Seconds for one location (eviction, the timed stages, and rebuilding the
# eviction list when needed) before it counts as failed. The timeout above
# covers every location: 3 x (60 + 5 for timeout -k) + 15 to report.
ENV_TIMEOUT=60

# Before each run the probe drops the files it will read from this host's
# page cache, so every run reads them from the NFS server, as a user's first
# import of the day would; otherwise they stay cached here between runs (a few
# hundred MB against a TB of RAM) and the probe would only time this host's
# memory. The server's own cache is left alone: client-side trouble is what
# this is for. POSIX_FADV_DONTNEED needs no privilege and only drops clean
# pages of these files. It runs under the system python3, on local disk, so
# the eviction itself doesn't load anything back from the location.
# shellcheck disable=SC2016  # read by python
export EVICT_PY='
import os, sys
for f in open(sys.argv[1]).read().split("\n"):
    try:
        fd = os.open(f, os.O_RDONLY)
    except OSError:
        continue
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)
'
# Prints every file this interpreter has loaded: modules, their .pyc, and
# shared libraries. That is the list to evict (about 6,000 files, 1 s to evict).
export LOADED_PY='
import os, sys
def loaded():
    fs = set()
    for m in list(sys.modules.values()):
        for a in ("__file__", "__cached__"):
            f = getattr(m, a, None)
            if isinstance(f, str) and os.path.exists(f):
                fs.add(f)
    for line in open("/proc/self/maps"):
        p = line.split()
        if len(p) >= 6 and p[5].startswith("/"):
            fs.add(p[5])
    print("\n".join(sorted(fs)))
'

# Runs in its own shell. Prints "<stage> [package] <seconds>" as each stage
# finishes, so a location that hangs still reports the stages before it.
# shellcheck disable=SC2016  # expanded by the inner shell
PROBE='
dir=$1 list=$2; shift 2
env_dir=$dir/miniconda3/envs/gadget-probe
export CONDARC=$dir/miniconda3/.condarc
since() { local us=$(( ${EPOCHREALTIME/./} - ${1/./} )); printf "%d.%06d" $((us / 1000000)) $((us % 1000000)); }
[ -s "$list" ] && python3 -c "$EVICT_PY" "$list"
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
# (Re)build the eviction list on the first run and after conda/setup.sh has
# changed the environment (it rewrites its stamp file). Untimed; the files
# are cached by now. "conda activate" runs conda under the base interpreter.
[ "$list" -nt "$env_dir/.gadget-environment.yml" ] && exit 0
{
    "$dir/miniconda3/bin/python" -c "$LOADED_PY
sys.argv = [\"conda\", \"shell.posix\", \"activate\", sys.argv[1]]
from conda.cli import main
try:
    main()
except SystemExit:
    pass
sys.stdout = sys.__stdout__
loaded()" "$env_dir"
    python3 -c "$LOADED_PY
for p in sys.argv[1:]:
    __import__(p)
loaded()" "$@"
    echo "$dir/miniconda3/etc/profile.d/conda.sh"
} | awk -v d="$dir/" "index(\$0, d) == 1" | sort -u > "$list.$$" && mv -f "$list.$$" "$list"
'

for pair in "${ENVS[@]}"; do
    name=${pair%%=*} dir=${pair#*=}
    # Kept on this host's local disk, one per location and user.
    list=/tmp/gadget-conda-$name-$(id -u).files
    # The agent feeds this script to bash on stdin: keep the probe off it.
    # shellcheck disable=SC2086  # PACKAGES is a list
    out=$(timeout -k 5 "$ENV_TIMEOUT" bash --noprofile --norc -c "$PROBE" probe "$dir" "$list" $PACKAGES </dev/null 2>/dev/null)
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
