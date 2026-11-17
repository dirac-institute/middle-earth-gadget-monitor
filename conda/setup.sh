#!/bin/bash
# Creates or updates the conda probe's environment under one directory, on the
# host that will probe it (README.md, "Conda probe"). Safe to re-run.
#
#   conda/setup.sh DIR [ENVIRONMENT_YML_BASE64]
#
# DIR gets its own Miniconda (DIR/miniconda3), not the system conda, with the
# environment from conda/environment.yml in DIR/miniconda3/envs/gadget-probe.
# A conda install can't be moved or copied (its own path is written into it),
# so each location is installed in place. init.sh runs this over SSH, with
# the environment file base64-encoded as the second argument; run from a
# checkout, it reads conda/environment.yml next to itself.
set -euo pipefail

MINICONDA=Miniconda3-py313_26.9.4-1-Linux-x86_64.sh
MINICONDA_SHA256=f210d607d85cb52592396eebbdf62c38c9bca868a3a18681368955c76412888b
ENV_NAME=gadget-probe

DIR="${1:?usage: setup.sh DIR [ENVIRONMENT_YML_BASE64]}"
CONDA_DIR="$DIR/miniconda3"
ENV_DIR="$CONDA_DIR/envs/$ENV_NAME"
# The environment file the env was last built from.
STAMP="$ENV_DIR/.gadget-environment.yml"

mkdir -p "$DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "${2:-}" ]; then
    base64 -d <<<"$2" > "$tmp/environment.yml"
else
    cp "$(dirname "${BASH_SOURCE[0]}")/environment.yml" "$tmp/environment.yml"
fi

if [ ! -x "$CONDA_DIR/bin/conda" ]; then
    echo "$DIR: installing $MINICONDA"
    curl -fsSL -o "$tmp/$MINICONDA" "https://repo.anaconda.com/miniconda/$MINICONDA"
    echo "$MINICONDA_SHA256  $tmp/$MINICONDA" | sha256sum -c --quiet
    rm -rf "$CONDA_DIR"   # left by an interrupted install
    bash "$tmp/$MINICONDA" -b -p "$CONDA_DIR"
fi
# conda-forge only (see environment.yml), and nothing from the account's own
# ~/.condarc: CONDARC replaces the search for user config files.
cat > "$CONDA_DIR/.condarc" <<'RC'
channels:
  - conda-forge
auto_update_conda: false
notify_outdated_conda: false
RC
export CONDARC="$CONDA_DIR/.condarc"

if [ ! -e "$STAMP" ]; then
    echo "$DIR: creating $ENV_NAME"
    rm -rf "$ENV_DIR"
    "$CONDA_DIR/bin/conda" env create -q -y -p "$ENV_DIR" -f "$tmp/environment.yml"
elif ! cmp -s "$STAMP" "$tmp/environment.yml"; then
    echo "$DIR: environment.yml changed, updating $ENV_NAME"
    "$CONDA_DIR/bin/conda" env update -q -p "$ENV_DIR" -f "$tmp/environment.yml" --prune
else
    echo "$DIR: $ENV_NAME up to date"
fi
cp "$tmp/environment.yml" "$STAMP"
"$CONDA_DIR/bin/conda" clean -q -y --all >/dev/null
