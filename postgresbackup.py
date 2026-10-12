#!/usr/bin/env python3
"""
Config-driven pg_dump -> pg_restore migration runner.

Reads a hidden JSON config file listing one or more migration entries, runs
pg_dump against each source and pg_restore into each destination, writes a
timestamped log file recording the outcome (success/failure + errors) for
every entry, and emails the report to a configured technical service address.

Commands:
  postgresbackup.py run
  postgresbackup.py enable
  postgresbackup.py disable
  postgresbackup.py config --set key:value [--set key:value ...]
  postgresbackup.py config --show

Migrations file (/etc/pg_migrate/.migrations.json), a list of entries:
[
  {
    "source_uri": "postgresql://user:pass@source-host:5432/db",
    "destination_uri": "postgresql://user:pass@target-host:5432/db",
    "dump_dir": "/var/backups/pg/mydb",
    "jobs": 4,
    "clean": true
  },
  ...
]

Settings file (/etc/pg_migrate/.settings.json), managed via `config --set`:
  smtp_host           SMTP server hostname                      (required)
  smtp_port           SMTP port (default: 587 / 465 / 25 by security mode)
  smtp_security       starttls | ssl | none  (default: starttls)
  smtp_username       SMTP login (optional; also the default sender)
  smtp_password       SMTP password (leave the value empty to be prompted)
  sender_address      From address (defaults to smtp_username)
  receiver_address    Technical service address to notify       (required)
  notify_on_failure   true | false (default: true) also mail runs with failures

Both files hold credentials. Keep them filesystem-permissioned to the user
this script runs as (the settings file is created with mode 600).
"""

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlsplit, urlunsplit

CONFIG_DIR = "/etc/pstgresbackup"
CONFIG_FILENAME = ".migrations.json"  # hidden file
SETTINGS_FILENAME = ".settings.json"  # hidden file
LOG_DIR = "/var/log/pg_migrate"
CRON_MARKER = "# postgresbackup-managed"

REQUIRED_FIELDS = {
    "source_uri": str,
    "destination_uri": str,
    "dump_dir": str,
    "jobs": int,
    "clean": bool,
}

USAGE = "Usage: postgresbackup.py (run | enable | disable | config)"


# --------------------------------------------------------------------------
# Settings (SMTP credentials + receiver)
# --------------------------------------------------------------------------


def _validate_nonempty(key):
    def check(value: str) -> str:
        if not value:
            raise ValueError(f"'{key}' cannot be empty")
        return value

    return check


def _validate_email(key):
    def check(value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError(f"'{key}' must be a valid email address")
        return value

    return check


def _validate_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise ValueError("'smtp_port' must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("'smtp_port' must be between 1 and 65535")
    return port


def _validate_security(value: str) -> str:
    value = value.lower()
    if value not in ("starttls", "ssl", "none"):
        raise ValueError("'smtp_security' must be one of: starttls, ssl, none")
    return value


def _validate_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in ("true", "yes", "1", "on"):
        return True
    if lowered in ("false", "no", "0", "off"):
        return False
    raise ValueError("'notify_on_failure' must be true or false")


def _validate_any(value: str) -> str:
    return value


SETTINGS_SCHEMA = {
    "smtp_host": _validate_nonempty("smtp_host"),
    "smtp_port": _validate_port,
    "smtp_security": _validate_security,
    "smtp_username": _validate_any,
    "smtp_password": _validate_any,
    "sender_address": _validate_email("sender_address"),
    "receiver_address": _validate_email("receiver_address"),
    "notify_on_failure": _validate_bool,
}

SECRET_KEYS = {"smtp_password"}


def settings_path() -> str:
    return os.path.join(CONFIG_DIR, SETTINGS_FILENAME)


def load_settings() -> dict:
    """Read the settings file. Returns {} if it doesn't exist yet."""
    path = settings_path()
    if not os.path.isfile(path):
        return {}
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Settings file '{path}' must contain a JSON object")
    return data


def save_settings(settings: dict) -> None:
    """Atomically write the settings file with owner-only permissions."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    path = settings_path()
    tmp_path = f"{path}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def parse_assignment(item: str) -> tuple:
    """Split 'key:value' on the FIRST colon only (values may contain colons)."""
    if ":" not in item:
        raise ValueError(f"'{item}' is not in key:value form")
    key, value = item.split(":", 1)
    key = key.strip()
    if key not in SETTINGS_SCHEMA:
        raise ValueError(
            f"Unknown setting '{key}'. Valid keys: {', '.join(sorted(SETTINGS_SCHEMA))}"
        )
    return key, value.strip()


def show_settings(settings: dict) -> None:
    if not settings:
        print(f"No settings configured ({settings_path()}).")
        return
    for key in sorted(SETTINGS_SCHEMA):
        if key in settings:
            shown = "********" if key in SECRET_KEYS else settings[key]
            print(f"{key} = {shown}")


def config(argv: list) -> int:
    """Handle `postgresbackup config --set key:value` / `--show`."""
    parser = argparse.ArgumentParser(
        prog="postgresbackup.py config",
        description="View or change notification settings.",
    )
    parser.add_argument(
        "--set",
        dest="assignments",
        action="append",
        default=[],
        metavar="KEY:VALUE",
        help="set a value (repeatable). Use 'smtp_password:' with an empty "
        "value to be prompted without echo.",
    )
    parser.add_argument("--show", action="store_true", help="show current settings")
    args = parser.parse_args(argv)

    if not args.assignments and not args.show:
        parser.print_usage(sys.stderr)
        print("Error: provide --set key:value and/or --show", file=sys.stderr)
        return 1

    try:
        settings = load_settings()

        if args.assignments:
            updates = {}
            for item in args.assignments:
                key, raw = parse_assignment(item)
                if key in SECRET_KEYS and raw == "":
                    raw = getpass.getpass(f"{key}: ")
                updates[key] = SETTINGS_SCHEMA[key](raw)
            settings.update(updates)
            save_settings(settings)
            print(f"Saved: {', '.join(sorted(updates))} -> {settings_path()}")

        if args.show:
            show_settings(settings)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


# --------------------------------------------------------------------------
# Email notification
# --------------------------------------------------------------------------


def send_email(settings: dict, subject: str, body: str) -> None:
    """Send a plain-text email using the configured SMTP settings."""
    host = settings.get("smtp_host")
    receiver = settings.get("receiver_address")
    sender = settings.get("sender_address") or settings.get("smtp_username")
    missing = [
        name
        for name, val in (
            ("smtp_host", host),
            ("receiver_address", receiver),
            ("sender_address (or an email-style smtp_username)", sender),
        )
        if not val
    ]
    if missing:
        raise ValueError(f"missing required settings: {', '.join(missing)}")

    security = settings.get("smtp_security", "starttls")
    default_ports = {"starttls": 587, "ssl": 465, "none": 25}
    port = settings.get("smtp_port", default_ports[security])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver
    msg.set_content(body)

    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)

    with server:
        if security == "starttls":
            server.starttls(context=context)
        if settings.get("smtp_username"):
            server.login(settings["smtp_username"], settings.get("smtp_password", ""))
        server.send_message(msg)


def notify(results: list, report: str) -> bool:
    """
    Email the run report if configured. Returns False only when a send was
    attempted (or required settings were unreadable) and failed.
    """
    failed_count = sum(1 for r in results if r["status"] != "success")

    try:
        settings = load_settings()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Could not read settings, no email sent: {exc}", file=sys.stderr)
        return False

    if failed_count and not settings.get("notify_on_failure", True):
        return True

    if not settings.get("smtp_host") or not settings.get("receiver_address"):
        print(
            "Email not configured (smtp_host / receiver_address); skipping "
            "notification. Use: postgresbackup.py config --set key:value",
            file=sys.stderr,
        )
        return True

    status = "SUCCESS" if not failed_count else f"FAILURES ({failed_count})"
    subject = (
        f"[pg_migrate] {status}: {len(results) - failed_count}/{len(results)} "
        f"entries on {socket.gethostname()}"
    )
    try:
        send_email(settings, subject, report)
    except Exception as exc:
        print(f"Failed to send notification email: {exc}", file=sys.stderr)
        return False

    print(f"Report emailed to {settings['receiver_address']}.")
    return True


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def mask_uri(uri: str) -> str:
    """Replace the password in a connection URI with '***'."""
    try:
        parts = urlsplit(uri)
        if parts.password is None:
            return uri
        host_part = parts.netloc.rsplit("@", 1)[1]
        user = parts.username or ""
        return urlunsplit(parts._replace(netloc=f"{user}:***@{host_part}"))
    except ValueError:
        return "<unparseable uri>"


def pg_dump(db_uri: str, dump_dir: str, jobs: int = 1) -> None:
    """Dump `db_uri` into `dump_dir` using pg_dump's directory format."""
    assert (
        shutil.which("pg_dump") is not None
    ), "pg_dump not found on PATH. Install the postgresql-client package."
    assert not os.path.exists(dump_dir), (
        f"'{dump_dir}' already exists. pg_dump's directory format refuses "
        "to write into an existing directory."
    )
    assert jobs >= 1, "jobs must be >= 1"

    cmd = [
        "pg_dump",
        "--dbname",
        db_uri,
        "--format",
        "directory",
        "--jobs",
        str(jobs),
        "--file",
        dump_dir,
        "--verbose",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"pg_dump failed (exit code {result.returncode}):\n{result.stderr}"
        )


def pg_restore(db_uri: str, dump_dir: str, jobs: int = 1, clean: bool = True) -> None:
    """Restore the directory-format dump at `dump_dir` into `db_uri`."""
    assert (
        shutil.which("pg_restore") is not None
    ), "pg_restore not found on PATH. Install the postgresql-client package."
    assert os.path.isdir(
        dump_dir
    ), f"'{dump_dir}' is not a directory. Expected a directory-format dump."
    assert jobs >= 1, "jobs must be >= 1"

    cmd = [
        "pg_restore",
        "--dbname",
        db_uri,
        "--jobs",
        str(jobs),
        "--no-owner",
        "--verbose",
    ]
    if clean:
        cmd += ["--clean", "--if-exists"]
    cmd.append(dump_dir)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"pg_restore failed (exit code {result.returncode}):\n{result.stderr}"
        )


def load_config() -> list:
    """Read and validate the hidden migrations config file."""
    config_path = os.path.join(CONFIG_DIR, CONFIG_FILENAME)

    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found at '{config_path}'")

    with open(config_path, "r") as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        raise ValueError("Config file must contain a JSON list of entries")

    for i, entry in enumerate(entries):
        for field, field_type in REQUIRED_FIELDS.items():
            if field not in entry:
                raise ValueError(f"Entry {i} is missing required field '{field}'")
            if not isinstance(entry[field], field_type):
                raise ValueError(
                    f"Entry {i} field '{field}' must be of type "
                    f"{field_type.__name__}, got {type(entry[field]).__name__}"
                )

    return entries


def run_entry(entry: dict) -> dict:
    """Run dump+restore for a single entry. Returns a result record."""
    label = entry.get("dump_dir", "<unnamed>")
    record = {
        "dump_dir": label,
        "source_uri": entry["source_uri"],
        "destination_uri": entry["destination_uri"],
        "status": "success",
        "error": None,
    }

    try:
        pg_dump(entry["source_uri"], entry["dump_dir"], jobs=entry["jobs"])
        pg_restore(
            entry["destination_uri"],
            entry["dump_dir"],
            jobs=entry["jobs"],
            clean=entry["clean"],
        )
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
    finally:
        # Clean up the dump directory regardless of outcome so a failed run
        # doesn't block the next scheduled attempt via the "already exists"
        # check in pg_dump.
        if os.path.isdir(entry["dump_dir"]):
            shutil.rmtree(entry["dump_dir"], ignore_errors=True)

    return record


def build_report(results: list, started_at: datetime, finished_at: datetime) -> str:
    """Build the human-readable report used for both the log and the email."""
    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = len(results) - success_count

    lines = [
        f"Host: {socket.gethostname()}",
        f"Migration run started: {started_at.isoformat()}",
        f"Migration run finished: {finished_at.isoformat()}",
        f"Duration: {(finished_at - started_at).total_seconds():.1f}s",
        f"Entries: {len(results)}",
        "",
        f"Succeeded: {success_count}  Failed: {failed_count}",
        "=" * 60,
    ]

    for r in results:
        lines.append(f"[{r['status'].upper()}] {r['dump_dir']}")
        lines.append(f"  source:      {mask_uri(r['source_uri'])}")
        lines.append(f"  destination: {mask_uri(r['destination_uri'])}")
        if r["error"]:
            lines.append(f"  error: {r['error']}")
        lines.append("-" * 60)

    return "\n".join(lines) + "\n"


def write_log(report: str, started_at: datetime) -> str:
    """Write the report to a timestamped log file."""
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"migration_{timestamp}.log")

    with open(log_path, "w") as f:
        f.write(report)

    return log_path


def run() -> int:
    started_at = datetime.now(timezone.utc)

    try:
        entries = load_config()
    except Exception as exc:
        # Can't even start: write a minimal log (and notify) rather than
        # failing silently.
        finished_at = datetime.now(timezone.utc)
        results = [
            {
                "dump_dir": "<config load>",
                "source_uri": "",
                "destination_uri": "",
                "status": "failed",
                "error": str(exc),
            }
        ]
        report = build_report(results, started_at, finished_at)
        write_log(report, started_at)
        print(f"Failed to load config: {exc}", file=sys.stderr)
        notify(results, report)
        return 1

    results = [run_entry(entry) for entry in entries]
    finished_at = datetime.now(timezone.utc)
    report = build_report(results, started_at, finished_at)
    log_path = write_log(report, started_at)

    failed = [r for r in results if r["status"] == "failed"]
    print(f"Migration run complete. Log written to: {log_path}")
    if failed:
        print(f"{len(failed)} of {len(results)} entries failed.", file=sys.stderr)

    notified = notify(results, report)

    if failed:
        return 1
    if not notified:
        return 2  # migrations succeeded, but the notification email did not
    return 0


# --------------------------------------------------------------------------
# Scheduling (cron)
# --------------------------------------------------------------------------


def _prompt_time() -> tuple:
    """Prompt for a time in HH:MM (24h) format. Returns (hour, minute)."""
    raw = input("Time to run (HH:MM, 24-hour): ").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise ValueError(f"'{raw}' is not in HH:MM format")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"'{raw}' is not a valid time of day")
    return hour, minute


def _read_crontab() -> list:
    """Return the current user's crontab as a list of lines (empty if none)."""
    assert shutil.which("crontab") is not None, "crontab not found on PATH."
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        if "no crontab" in result.stderr.lower():
            return []
        raise RuntimeError(f"Failed to read crontab: {result.stderr.strip()}")
    return result.stdout.splitlines()


def _write_crontab(lines: list) -> None:
    """Replace the current user's crontab with `lines`."""
    text = "\n".join(lines) + "\n" if lines else ""
    result = subprocess.run(
        ["crontab", "-"], input=text, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to write crontab: {result.stderr.strip()}")


def _is_managed_entry_at(line: str, hour: int, minute: int) -> bool:
    """True if `line` is a cron entry created by enable() for this time."""
    if CRON_MARKER not in line or line.lstrip().startswith("#"):
        return False
    fields = line.split()
    if len(fields) < 2:
        return False
    try:
        return int(fields[0]) == minute and int(fields[1]) == hour
    except ValueError:
        return False


def enable():
    """
    Prompts user to input the time to run HH:MM
    Configures a cron job to run the backup at that specified time
    """
    try:
        hour, minute = _prompt_time()
        lines = _read_crontab()
    except (ValueError, RuntimeError, AssertionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if any(_is_managed_entry_at(line, hour, minute) for line in lines):
        print(f"A backup job is already scheduled at {hour:02d}:{minute:02d}.")
        return 0

    script = os.path.abspath(__file__)
    command = f"{shlex.quote(sys.executable)} {shlex.quote(script)} run"
    # '%' has special meaning in crontab lines and must be escaped.
    command = command.replace("%", r"\%")
    lines.append(f"{minute} {hour} * * * {command} {CRON_MARKER}")

    try:
        _write_crontab(lines)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Backup scheduled daily at {hour:02d}:{minute:02d}.")
    return 0


def disable():
    """
    Prompts user to input the time for a configured cron job run
    Disables any cron job configured to run the backup at that time
    """
    try:
        hour, minute = _prompt_time()
        lines = _read_crontab()
    except (ValueError, RuntimeError, AssertionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    remaining = [l for l in lines if not _is_managed_entry_at(l, hour, minute)]
    removed = len(lines) - len(remaining)

    if removed == 0:
        print(f"No backup job found at {hour:02d}:{minute:02d}.")
        return 0

    try:
        _write_crontab(remaining)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Removed {removed} backup job(s) scheduled at {hour:02d}:{minute:02d}.")
    return 0


if __name__ == "__main__":
    args_ = sys.argv
    if len(args_) < 2:
        raise SystemExit(USAGE)

    cmd = args_[1]
    if cmd == "run":
        sys.exit(run())
    elif cmd == "enable":
        sys.exit(enable())
    elif cmd == "disable":
        sys.exit(disable())
    elif cmd == "config":
        sys.exit(config(args_[2:]))

    raise SystemExit(USAGE)
