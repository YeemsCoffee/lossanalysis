"""
The .ebextensions config is only exercised during a deploy, where a mistake is
expensive: a malformed file fails the deploy, and a wrong schedule syncs at
the wrong hours without anything complaining. These tests read the real file
and check both.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT   = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".ebextensions/square-sync.config"

SERVICE = "/etc/systemd/system/square-sync.service"
TIMER   = "/etc/systemd/system/square-sync.timer"
SCRIPT  = "/opt/elasticbeanstalk/bin/square-sync.sh"
ENVFILE = "/opt/elasticbeanstalk/deployment/env"

OPEN_HOUR, OPEN_MINUTE = 6, 30     # 6:30am Pacific
CLOSE_HOUR = 17                    # 5:00pm Pacific

# Only this shape is used, so only this shape needs parsing: every day, with
# explicit comma lists for hours and minutes and a fixed second.
_SPEC = re.compile(r"^\*-\*-\* ([\d,]+):([\d,]+):(\d\d)$")


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(CONFIG.read_text())


@pytest.fixture(scope="module")
def specs(config):
    body = config["files"][TIMER]["content"]
    return [ln.split("=", 1)[1].strip() for ln in body.splitlines()
            if ln.startswith("OnCalendar=")]


def _covered(specs):
    """Every (hour, minute) the timer fires at."""
    slots = set()
    for spec in specs:
        m = _SPEC.match(spec)
        assert m, f"unexpected OnCalendar shape, extend the parser: {spec!r}"
        hours, minutes = m.group(1), m.group(2)
        for h in hours.split(","):
            for mi in minutes.split(","):
                slots.add((int(h), int(mi)))
    return slots


def _unit(config, path):
    return config["files"][path]["content"]


# --- the file itself ---------------------------------------------------------

def test_config_is_valid_yaml(config):
    """A malformed .ebextensions file fails the whole deploy."""
    assert "files" in config and "commands" in config


def test_the_old_cron_config_is_gone():
    """Two configs would install two schedulers."""
    assert not (ROOT / ".ebextensions/cron-square-sync.config").exists()


def test_instance_clock_is_set_to_pacific(config):
    """
    The schedule is written in store time, so the box has to agree.

    In UTC the window moves an hour between PST and PDT and straddles
    midnight; setting the zone keeps the timer readable and correct.
    """
    commands = " ".join(c["command"] for c in config["commands"].values())
    assert "America/Los_Angeles" in commands


def test_sync_script_covers_two_days_including_today(config):
    script = _unit(config, SCRIPT)
    assert "--include-today" in script
    assert "--days 2" in script
    assert "--catch-up" in script


def test_log_is_rotated(config):
    assert "/etc/logrotate.d/square-sync" in config["files"]


# --- it has to be able to run at all ---------------------------------------------

def test_runs_from_a_systemd_timer_not_cron(config):
    """
    Amazon Linux 2023 does not install cron. A file in /etc/cron.d with no
    crond to read it does nothing and logs nothing.
    """
    assert TIMER in config["files"] and SERVICE in config["files"]
    assert not any(p.startswith("/etc/cron") for p in config["files"])


def test_previous_cron_job_is_removed(config):
    """Where cron does exist it would run alongside the timer, env bug and all."""
    commands = " ".join(c["command"] for c in config["commands"].values())
    assert "rm -f /etc/cron.d/square-sync" in commands


def test_timer_is_enabled_and_restarted_on_deploy(config):
    cmd = config["commands"]["04_enable_timer"]
    assert "daemon-reload" in cmd["command"]
    assert "enable square-sync.timer" in cmd["command"]
    # restart, not start: a changed schedule must take effect on redeploy
    assert "restart square-sync.timer" in cmd["command"]
    # A deploy that silently ends up with no scheduler is the original bug.
    assert not cmd.get("ignoreErrors", False)


def test_environment_comes_from_systemd_like_the_web_app(config):
    """
    The env file is systemd's format, not shell. Sourcing it emptied any
    value containing `&`, `$` or a space, and an empty DATABASE_URL sends
    the app to a local SQLite file nobody reads.
    """
    service = _unit(config, SERVICE)
    assert f"EnvironmentFile={ENVFILE}" in service
    assert "User=webapp" in service
    assert "WorkingDirectory=/var/app/current" in service

    script = _unit(config, SCRIPT)
    assert ENVFILE not in script, "the script must not source the env file"


def test_overlapping_runs_are_prevented(config):
    """A oneshot unit still running is not started again by its timer."""
    assert "Type=oneshot" in _unit(config, SERVICE)


def test_the_script_refuses_to_run_without_a_database_url(config):
    """Better a loud FATAL than a successful sync into the wrong database."""
    script = _unit(config, SCRIPT)
    assert '-z "${DATABASE_URL:-}"' in script and "FATAL" in script


def test_the_script_refuses_without_a_database_url_when_run(config, tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    script = tmp_path / "s.sh"
    script.write_text(_unit(config, SCRIPT))
    r = subprocess.run([bash, str(script)], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 1
    assert "FATAL: DATABASE_URL is not set" in r.stdout


def test_the_script_does_not_abort_before_it_can_report(config):
    """`set -e` exits on the failing line without saying which."""
    assert "set -e" not in _unit(config, SCRIPT).replace("set -uo", "")


def test_the_log_is_writable_by_the_service_user(config):
    """webapp cannot create files in root-owned /var/log."""
    cmd = config["commands"]["03_create_sync_log"]["command"]
    assert "touch /var/log/square-sync.log" in cmd
    assert "chown webapp /var/log/square-sync.log" in cmd
    assert ">> /var/log/square-sync.log" in _unit(config, SERVICE)


def test_the_log_is_retrievable_from_the_console(config):
    """A failure before Python starts is invisible in the app, so the log
    has to reach the EB console instead of dying on the instance."""
    for task in ("taillogs.d", "bundlelogs.d"):
        path = f"/opt/elasticbeanstalk/tasks/{task}/square-sync.conf"
        assert path in config["files"], f"missing {path}"
        assert "/var/log/square-sync.log" in config["files"][path]["content"]


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd-analyze")
def test_systemd_accepts_every_schedule(specs):
    """Parse with systemd itself, not with the regex above."""
    for spec in specs:
        r = subprocess.run(["systemd-analyze", "calendar", spec],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{spec!r}: {r.stderr.strip()}"


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd-analyze")
def test_systemd_accepts_the_unit_files(config, tmp_path):
    for path in (SERVICE, TIMER):
        (tmp_path / Path(path).name).write_text(_unit(config, path))
    r = subprocess.run(["systemd-analyze", "verify", "--man=no",
                        str(tmp_path / "square-sync.timer"),
                        str(tmp_path / "square-sync.service")],
                       capture_output=True, text=True)
    # A missing webapp user or paths on this machine are fine; syntax is not.
    problems = [ln for ln in r.stderr.splitlines()
                if ln.strip() and "webapp" not in ln and "No such file" not in ln]
    assert not problems, "\n".join(problems)


# --- the schedule ------------------------------------------------------------

def test_fires_every_15_minutes_through_the_service_day(specs):
    covered = _covered(specs)
    expected = {(OPEN_HOUR, 30), (OPEN_HOUR, 45)}
    for hour in range(OPEN_HOUR + 1, CLOSE_HOUR + 1):
        expected |= {(hour, m) for m in (0, 15, 30, 45)}

    missing = sorted(expected - covered)
    assert not missing, f"service window not covered at: {missing[:8]}"


def test_keeps_running_past_close_to_catch_the_lag(specs):
    """
    Square's reporting lags ~15 minutes, so tickets closing just before 5pm
    only land afterwards. Stopping exactly at close would lose them.
    """
    assert (CLOSE_HOUR, 45) in _covered(specs), "nothing runs after close to catch the tail"


def test_does_not_run_before_opening(specs):
    covered = _covered(specs)
    too_early = [(h, m) for (h, m) in covered
                 if h == OPEN_HOUR and m < OPEN_MINUTE]
    assert not too_early, f"fires before the stores open: {sorted(too_early)}"

    assert not [(h, m) for (h, m) in covered if 3 <= h < OPEN_HOUR], \
        "fires in the small hours for no reason"


def test_does_not_run_all_evening(specs):
    covered = _covered(specs)
    evening = sorted((h, m) for (h, m) in covered if CLOSE_HOUR + 1 < h <= 23)
    assert not evening, f"still firing late into the evening: {evening[:8]}"


def test_reconciles_once_overnight(specs):
    """One quiet run to pick up anything Square settled late."""
    overnight = [(h, m) for (h, m) in _covered(specs) if 0 <= h < 3]
    assert len(overnight) == 1, f"expected exactly one overnight run, got {overnight}"


def test_daily_run_count_is_sane(specs):
    """Roughly a service day's worth — not 96, not a handful."""
    runs = len(_covered(specs))
    assert 40 <= runs <= 60, f"{runs} runs a day looks wrong"
