"""
The .ebextensions config is only exercised during a deploy, where a mistake is
expensive: a malformed file fails the deploy, and a wrong cron expression
syncs at the wrong hours without anything complaining. These tests read the
real file and check both.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

CONFIG = Path(__file__).resolve().parent.parent / ".ebextensions/cron-square-sync.config"

OPEN_HOUR, OPEN_MINUTE = 6, 30     # 6:30am Pacific
CLOSE_HOUR = 17                    # 5:00pm Pacific


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(CONFIG.read_text())


@pytest.fixture(scope="module")
def cron_lines(config):
    body = config["files"]["/etc/cron.d/square-sync"]["content"]
    return [ln for ln in body.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
            and not ln.startswith(("SHELL", "PATH"))]


def _field(spec, low, high):
    """Expand one cron field ('*', '*/15', '7-17', '30,45') to a set."""
    values = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/")
            step = int(step_s)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_s, end_s = part.split("-")
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def _covered(cron_lines):
    """Every (hour, minute) the schedule fires at."""
    slots = set()
    for line in cron_lines:
        minute, hour = line.split()[0], line.split()[1]
        for h in _field(hour, 0, 23):
            for m in _field(minute, 0, 59):
                slots.add((h, m))
    return slots


# --- the file itself ---------------------------------------------------------

def test_config_is_valid_yaml(config):
    """A malformed .ebextensions file fails the whole deploy."""
    assert "files" in config and "commands" in config


def test_instance_clock_is_set_to_pacific(config):
    """
    The schedule is written in store time, so the box has to agree.

    In UTC the window moves an hour between PST and PDT and straddles
    midnight; setting the zone is what keeps the cron lines readable and
    correct year round.
    """
    commands = " ".join(c["command"] for c in config["commands"].values())
    assert "America/Los_Angeles" in commands


def test_sync_script_covers_two_days_including_today(config):
    script = config["files"]["/opt/elasticbeanstalk/bin/square-sync.sh"]["content"]
    assert "--include-today" in script
    assert "--days 2" in script
    # Environment properties must be sourced or the token is invisible to cron.
    assert "/opt/elasticbeanstalk/deployment/env" in script


def test_overlapping_runs_are_prevented(cron_lines):
    """A slow sync must not have the next one start on top of it."""
    assert all("flock -n" in line for line in cron_lines)


def test_log_is_rotated(config):
    assert "/etc/logrotate.d/square-sync" in config["files"]


# --- the schedule ------------------------------------------------------------

def test_fires_every_15_minutes_through_the_service_day(cron_lines):
    covered = _covered(cron_lines)
    expected = {(OPEN_HOUR, 30), (OPEN_HOUR, 45)}
    for hour in range(OPEN_HOUR + 1, CLOSE_HOUR + 1):
        expected |= {(hour, m) for m in (0, 15, 30, 45)}

    missing = sorted(expected - covered)
    assert not missing, f"service window not covered at: {missing[:8]}"


def test_keeps_running_past_close_to_catch_the_lag(cron_lines):
    """
    Square's reporting lags ~15 minutes, so tickets closing just before 5pm
    only land afterwards. Stopping exactly at close would lose them.
    """
    covered = _covered(cron_lines)
    assert (CLOSE_HOUR, 45) in covered, "nothing runs after close to catch the tail"


def test_does_not_run_before_opening(cron_lines):
    covered = _covered(cron_lines)
    too_early = [(h, m) for (h, m) in covered
                 if h == OPEN_HOUR and m < OPEN_MINUTE]
    assert not too_early, f"fires before the stores open: {sorted(too_early)}"

    assert not [(h, m) for (h, m) in covered if 3 <= h < OPEN_HOUR], \
        "fires in the small hours for no reason"


def test_does_not_run_all_evening(cron_lines):
    covered = _covered(cron_lines)
    evening = sorted((h, m) for (h, m) in covered if CLOSE_HOUR + 1 < h <= 23)
    assert not evening, f"still firing late into the evening: {evening[:8]}"


def test_reconciles_once_overnight(cron_lines):
    """One quiet run to pick up anything Square settled late."""
    covered = _covered(cron_lines)
    overnight = [(h, m) for (h, m) in covered if 0 <= h < 3]
    assert len(overnight) == 1, f"expected exactly one overnight run, got {overnight}"


def test_daily_run_count_is_sane(cron_lines):
    """Roughly a service day's worth — not 96, not a handful."""
    runs = len(_covered(cron_lines))
    assert 40 <= runs <= 60, f"{runs} runs a day looks wrong"
