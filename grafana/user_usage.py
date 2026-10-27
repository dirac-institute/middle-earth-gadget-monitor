"""The one definition of "active" and "logged in", shared by both dashboards.

Imported by gen_hosts_overview.py (public: daily and weekly user counts) and
gen_users.py (admin only: per-user usage). Change a threshold here and
regenerate both; the thresholds are applied at query time, so a change also
reinterprets all stored history.

A user is judged per host per 5-minute interval (the users check's cadence):

  logged in  at least one process (not counting the systemd user manager)
  active     any of: CPU averaged >= CPU_CORES cores, memory held >= MEM_BYTES,
             GPU busy >= GPU_PERCENT, GPU memory held >= GPU_MEM_BYTES

A user is active on a day (week) if any interval that day (week) was active,
and usage tallies count only active intervals. Days and weeks are Pacific,
weeks start Monday.
"""

CPU_CORES = 2
MEM_BYTES = 5 * 2**30                  # 5 GiB
GPU_PERCENT = 5
GPU_MEM_BYTES = 2**30                  # 1 GiB
STEP = "5m"
TZ = "America/Los_Angeles"


def _hu(expr):
    return f"max by (host, user) ({expr})"


def cpu_cores(sel):
    return _hu(f"rate(gadget_user_cpu_seconds_total{{{sel}}}[{STEP}])")


def cpu_seconds(sel):
    return _hu(f"increase(gadget_user_cpu_seconds_total{{{sel}}}[{STEP}])")


def mem(sel):
    return _hu(f"max_over_time(gadget_user_memory_bytes{{{sel}}}[{STEP}])")


def gpu_percent(sel):
    return _hu(f"avg_over_time(gadget_user_gpu_percent{{{sel}}}[{STEP}])")


def gpu_mem(sel):
    return _hu(f"max_over_time(gadget_user_gpu_memory_bytes{{{sel}}}[{STEP}])")


# The two indicators below return a series per (host, user) only when the
# condition holds; the value itself is meaningless. (Not wrapped in sgn():
# VictoriaMetrics returns sgn() values for times with no data when it is
# inside a subquery with an offset.)

def logged_in(sel):
    """A series for each (host, user) with processes in the interval ending now."""
    return f"({_hu(f'max_over_time(gadget_user_processes{{{sel}}}[{STEP}])')} > 0)"


def active(sel):
    """A series for each (host, user) whose interval ending now was active."""
    return (f"({cpu_cores(sel)} >= {CPU_CORES} or {mem(sel)} >= {MEM_BYTES:.0f} "
            f"or {gpu_percent(sel)} >= {GPU_PERCENT} or {gpu_mem(sel)} >= {GPU_MEM_BYTES:.0f})")


def _local(fn):
    return f'{fn}(time() + timezone_offset("{TZ}"))'


def distinct_users(indicator, period):
    """Distinct users (across the selected hosts) with the indicator set at
    least once in each Pacific day ("1d") or Monday-start week ("7d").

    Evaluate at a 1h step: only the point at each period's local midnight
    survives, and it looks forward over that period (so the current, partial
    period is included and the bar starts where the period does).
    """
    start = f"{_local('hour')} == 0"
    if period == "7d":
        start += f" and on() {_local('day_of_week')} == 1"
    return (f"count(max by (user) (max_over_time(({indicator})[{period}:{STEP}] offset -{period}))) "
            f"and on() ({start})")


def only_active(expr, sel):
    """expr per (host, user), kept only in active intervals."""
    return f"({expr} and on (host, user) {active(sel)})"


def over_range(expr, rng="$__range"):
    """Sum a per-interval quantity over the dashboard range, per user."""
    return f"sum by (user) (sum_over_time(({expr})[{rng}:{STEP}]))"


# Per-user tallies over the dashboard range, counting only active intervals.
# Each returns (expr, unit) for one table column.
INTERVAL_S = 300

def tallies(sel):
    return {
        "Active time": (f"sum by (user) (count_over_time(({active(sel)})[$__range:{STEP}])) * {INTERVAL_S}", "s"),
        "CPU core-hours": (f"{over_range(only_active(cpu_seconds(sel), sel))} / 3600", "suffix: core·h"),
        "Memory GiB-hours": (f"{over_range(only_active(mem(sel), sel))} * {INTERVAL_S} / 3600 / 2^30", "suffix: GiB·h"),
        "Peak memory": (f"max by (user) (max_over_time(gadget_user_memory_bytes{{{sel}}}[$__range]))", "bytes"),
        "GPU-hours": (f"{over_range(only_active(gpu_percent(sel), sel))} / 100 * {INTERVAL_S} / 3600", "suffix: GPU·h"),
        "GPU memory GiB-hours": (f"{over_range(only_active(gpu_mem(sel), sel))} * {INTERVAL_S} / 3600 / 2^30", "suffix: GiB·h"),
        "Peak GPU memory": (f"max by (user) (max_over_time(gadget_user_gpu_memory_bytes{{{sel}}}[$__range]))", "bytes"),
    }
