"""Conda probe.

Times activating a conda environment, starting its python3 and importing
packages, on each storage location in config/conda.toml. It runs on the
targets as an ordinary check named "conda", through the persistent session,
so it is scheduled, timed out and recorded like the others; only its script
is put together here, from the config and conda/probe.sh.
"""

import shlex
import tomllib

from collector import SRV
from ssh_checks import Check

CONFIG_FILE = SRV / "config" / "conda.toml"
SCRIPT_FILE = SRV / "conda" / "probe.sh"
# Locations run one after another, and the agent needs a little to report.
SLACK_S = 15


def load_conda_check():
    cfg = tomllib.loads(CONFIG_FILE.read_text())
    envs = [f"{e['name']}={e['dir']}" for e in cfg["env"]]
    header = (
        f"ENVS=({' '.join(shlex.quote(e) for e in envs)})\n"
        f"PACKAGES={shlex.quote(' '.join(cfg['packages']))}\n"
        f"ENV_TIMEOUT={int(cfg['env_timeout'])}\n"
    )
    # timeout -k gives each location 5 s more after its timeout.
    timeout = len(envs) * (cfg["env_timeout"] + 5) + SLACK_S
    return Check("conda", header + SCRIPT_FILE.read_text(), cfg["interval"], timeout,
                 tuple(cfg["hosts"]))
