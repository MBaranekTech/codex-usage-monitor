#!/usr/bin/env python3
"""Local, read-only Codex usage monitor."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class Window:
    label: str
    used_percent: float
    window_minutes: int
    resets_at: int | None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass
class Usage:
    observed_at: str
    session_file: str
    project: str | None
    plan: str | None
    context_used_percent: float | None
    total_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    windows: list[Window]
    modified_at: float = 0.0

    @property
    def session_id(self) -> str:
        return Path(self.session_file).stem


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def session_files(root: Path, limit: int | None = 200) -> list[Path]:
    sessions = root / "sessions"
    if not sessions.exists():
        return []
    files = sessions.glob("**/*.jsonl")
    result = sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)
    return result[:limit] if limit else result


def json_lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except OSError:
        return


def usage_from_file(path: Path) -> Usage | None:
    project = None
    latest: dict[str, Any] | None = None
    observed_at = ""
    saw_session = False
    for record in json_lines(path):
        payload = record.get("payload", {})
        if record.get("type") == "session_meta":
            saw_session = True
            project = payload.get("cwd")
            observed_at = record.get("timestamp", observed_at)
        if record.get("type") == "event_msg" and payload.get("type") == "token_count":
            latest = payload
            observed_at = record.get("timestamp", observed_at)
    if latest is None and not saw_session:
        return None

    latest = latest or {}
    info = latest.get("info") or {}
    tokens = info.get("total_token_usage") or {}
    active_tokens = info.get("last_token_usage") or {}
    context_window = info.get("model_context_window") or 0
    total_tokens = int(tokens.get("total_tokens") or 0)
    active_total = int(active_tokens.get("total_tokens") or 0)
    context_percent = (
        min(100.0, active_total * 100.0 / context_window)
        if context_window and active_total
        else None
    )
    rate_limits = latest.get("rate_limits") or {}
    windows = []
    for key in ("primary", "secondary"):
        value = rate_limits.get(key)
        if not value:
            continue
        minutes = int(value.get("window_minutes") or 0)
        label = "5h" if minutes == 300 else "week" if minutes == 10080 else f"{minutes}m"
        windows.append(
            Window(
                label=label,
                used_percent=float(value.get("used_percent") or 0),
                window_minutes=minutes,
                resets_at=value.get("resets_at"),
            )
        )
    return Usage(
        observed_at=observed_at,
        session_file=str(path),
        project=project,
        plan=rate_limits.get("plan_type"),
        context_used_percent=context_percent,
        total_tokens=total_tokens,
        input_tokens=int(tokens.get("input_tokens") or 0),
        cached_input_tokens=int(tokens.get("cached_input_tokens") or 0),
        output_tokens=int(tokens.get("output_tokens") or 0),
        windows=windows,
        modified_at=path.stat().st_mtime,
    )


def all_usages(root: Path, limit: int | None = 10) -> list[Usage]:
    """Return the newest usage snapshot from each recent session."""
    scan_limit = None if not limit else max(200, limit)
    usages = [
        usage for path in session_files(root, scan_limit) if (usage := usage_from_file(path))
    ]
    usages.sort(key=lambda usage: (usage.observed_at, usage.modified_at), reverse=True)
    return usages[:limit] if limit else usages


def latest_usage(root: Path) -> Usage | None:
    return next(iter(all_usages(root, limit=1)), None)


def color(percent_left: float) -> str:
    return "\033[32m" if percent_left > 50 else "\033[33m" if percent_left > 20 else "\033[31m"


def bar(percent_left: float, width: int = 20, ansi: bool = True) -> str:
    filled = round(max(0.0, min(100.0, percent_left)) * width / 100)
    value = "█" * filled + "░" * (width - filled)
    return f"{color(percent_left)}{value}\033[0m" if ansi else value


def countdown(timestamp: int | None, now: float | None = None) -> str:
    if not timestamp:
        return "unknown"
    seconds = max(0, int(timestamp - (now if now is not None else time.time())))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes:02d}m"


def human_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def render(usage: Usage, ansi: bool = True) -> str:
    project = Path(usage.project).name if usage.project else "unknown"
    lines = [f"Codex usage · {project}" + (f" · {usage.plan}" if usage.plan else "")]
    if usage.context_used_percent is not None:
        remaining = 100.0 - usage.context_used_percent
        lines.append(
            f"Context  {bar(remaining, ansi=ansi)} {usage.context_used_percent:5.1f}% used"
        )
    for window in usage.windows:
        lines.append(
            f"{window.label:<8} {bar(window.remaining_percent, ansi=ansi)} "
            f"{window.remaining_percent:5.1f}% left · resets {countdown(window.resets_at)}"
        )
    lines.append(
        "Tokens   "
        f"{human_tokens(usage.total_tokens)} total · "
        f"{human_tokens(usage.input_tokens)} in · "
        f"{human_tokens(usage.output_tokens)} out · "
        f"{human_tokens(usage.cached_input_tokens)} cached"
    )
    observed = usage.observed_at.replace("T", " ").replace("Z", " UTC")
    lines.append(f"Updated  {observed}")
    return "\n".join(lines)


def session_state(usage: Usage, active_within: float, now: float | None = None) -> str:
    age = (time.time() if now is None else now) - usage.modified_at
    return "active" if age <= active_within * 60 else "idle"


def render_all(usages: list[Usage], ansi: bool = True, active_within: float = 60) -> str:
    lines = [f"Codex sessions · {len(usages)} shown"]
    for index, usage in enumerate(usages):
        if index:
            lines.append("")
        lines.append(f"[{session_state(usage, active_within).upper():6}] {usage.session_id}")
        lines.extend(render(usage, ansi=ansi).splitlines())
    return "\n".join(lines)


def age_text(modified_at: float, now: float | None = None) -> str:
    seconds = max(0, int((time.time() if now is None else now) - modified_at))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def render_overview(usages: list[Usage], active_within: float = 60) -> str:
    """Render a compact dashboard that fits several sessions on one screen."""
    active = sum(session_state(usage, active_within) == "active" for usage in usages)
    lines = [f"Codex sessions · {active} active · {len(usages)} shown"]
    limits = " · ".join(
        f"{window.label} {window.remaining_percent:.0f}% left, resets {countdown(window.resets_at)}"
        for window in usages[0].windows
    )
    if limits:
        lines.append(f"Limits: {limits}")
    lines.extend(
        [
            "",
            f"{'STATE':<6} {'CONTEXT':>8} {'TOKENS':>9} {'UPDATED':>7}  PROJECT / SESSION",
        ]
    )
    for usage in usages:
        state = session_state(usage, active_within).upper()
        context = (
            f"{usage.context_used_percent:.1f}%"
            if usage.context_used_percent is not None
            else "—"
        )
        project = Path(usage.project).name if usage.project else "unknown"
        short_id = usage.session_id.rsplit("-", 1)[-1][:8]
        lines.append(
            f"{state:<6} {context:>8} {human_tokens(usage.total_tokens):>9} "
            f"{age_text(usage.modified_at):>7}  {project} / {short_id}"
        )
    return "\n".join(lines)


def json_usage(usage: Usage) -> str:
    return json.dumps(asdict(usage), indent=2)


def json_usages(usages: list[Usage], active_within: float = 60) -> str:
    return json.dumps(
        [
            asdict(usage)
            | {
                "session_id": usage.session_id,
                "state": session_state(usage, active_within),
            }
            for usage in usages
        ],
        indent=2,
    )


def project_summary(usages: list[Usage]) -> list[dict[str, Any]]:
    projects: dict[str, dict[str, Any]] = {}
    for usage in usages:
        name = usage.project or "unknown"
        item = projects.setdefault(
            name, {"project": name, "sessions": 0, "total_tokens": 0, "latest": ""}
        )
        item["sessions"] += 1
        item["total_tokens"] += usage.total_tokens
        item["latest"] = max(item["latest"], usage.observed_at)
    return sorted(projects.values(), key=lambda item: item["latest"], reverse=True)


def render_projects(items: list[dict[str, Any]]) -> str:
    lines = ["Codex usage by project", f"{'SESSIONS':>8}  {'TOKENS':>10}  PROJECT"]
    for item in items:
        lines.append(f"{item['sessions']:>8}  {human_tokens(item['total_tokens']):>10}  {item['project']}")
    return "\n".join(lines)


def compact_status(usages: list[Usage], active_within: float = 60) -> str:
    active = sum(session_state(usage, active_within) == "active" for usage in usages)
    limits = " ".join(
        f"{window.label}:{window.remaining_percent:.0f}%" for window in usages[0].windows
    )
    return f"Codex {active}/{len(usages)} active" + (f" · {limits}" if limits else "")


def show_once(args: argparse.Namespace) -> int:
    usages = all_usages(args.codex_home.expanduser(), args.limit)
    if not usages:
        print("No Codex token-usage events found. Run a Codex task first.", file=sys.stderr)
        return 1
    print(
        json_usages(usages, args.active_within)
        if args.json
        else render_all(usages, ansi=not args.no_color, active_within=args.active_within)
    )
    return 0


def show_projects(args: argparse.Namespace) -> int:
    usages = all_usages(args.codex_home.expanduser(), limit=None)
    if not usages:
        print("No Codex token-usage events found. Run a Codex task first.", file=sys.stderr)
        return 1
    items = project_summary(usages)
    print(json.dumps(items, indent=2) if args.json else render_projects(items))
    return 0


def show_status(args: argparse.Namespace) -> int:
    usages = all_usages(args.codex_home.expanduser(), args.limit)
    print(compact_status(usages, args.active_within) if usages else "Codex 0/0 active")
    return 0


def watch(args: argparse.Namespace) -> int:
    try:
        while True:
            usages = all_usages(args.codex_home.expanduser(), args.limit)
            sys.stdout.write("\033[2J\033[H")
            if not usages:
                print("Waiting for Codex usage data…")
            else:
                if args.layout == "detailed":
                    print(render_all(usages, active_within=args.active_within))
                else:
                    print(render_overview(usages, active_within=args.active_within))
                if any(window.remaining_percent <= args.alert_below for usage in usages for window in usage.windows):
                    sys.stdout.write("\a")
            print(f"\nRefreshing every {args.interval:g}s · Ctrl+C to stop")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Monitor local Codex usage and limits.")
    result.add_argument("--codex-home", type=Path, default=codex_home())
    subparsers = result.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show", help="print recent session snapshots")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    show.add_argument("--no-color", action="store_true")
    show.add_argument("--limit", type=int, default=10, help="sessions to show; 0 means all")
    show.add_argument("--active-within", type=float, default=60, metavar="MINUTES")
    show.set_defaults(func=show_once)
    live = subparsers.add_parser("watch", help="continuously refresh the dashboard")
    live.add_argument("--interval", type=float, default=5.0)
    live.add_argument("--alert-below", type=float, default=10.0, metavar="PERCENT")
    live.add_argument("--limit", type=int, default=10, help="sessions to show; 0 means all")
    live.add_argument("--active-within", type=float, default=60, metavar="MINUTES")
    live.add_argument(
        "--layout",
        choices=("overview", "detailed"),
        default="overview",
        help="compact table or visual progress bars",
    )
    live.set_defaults(func=watch)
    projects = subparsers.add_parser("projects", help="summarize usage by project")
    projects.add_argument("--json", action="store_true", help="machine-readable output")
    projects.set_defaults(func=show_projects)
    status = subparsers.add_parser("status", help="one-line shell/tmux status")
    status.add_argument("--limit", type=int, default=10, help="sessions to inspect; 0 means all")
    status.add_argument("--active-within", type=float, default=60, metavar="MINUTES")
    status.set_defaults(func=show_status)
    return result


def main() -> int:
    args = parser().parse_args()
    if getattr(args, "interval", 1) <= 0:
        print("error: --interval must be greater than zero", file=sys.stderr)
        return 2
    if getattr(args, "limit", 0) < 0 or getattr(args, "active_within", 1) < 0:
        print("error: --limit and --active-within cannot be negative", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
