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


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def session_files(root: Path, limit: int = 40) -> list[Path]:
    sessions = root / "sessions"
    if not sessions.exists():
        return []
    files = sessions.glob("**/*.jsonl")
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


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
    for record in json_lines(path):
        payload = record.get("payload", {})
        if record.get("type") == "session_meta":
            project = payload.get("cwd")
        if record.get("type") == "event_msg" and payload.get("type") == "token_count":
            latest = payload
            observed_at = record.get("timestamp", observed_at)
    if latest is None:
        return None

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
    )


def latest_usage(root: Path) -> Usage | None:
    candidates = [usage for path in session_files(root) if (usage := usage_from_file(path))]
    return max(candidates, key=lambda usage: usage.observed_at, default=None)


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


def json_usage(usage: Usage) -> str:
    return json.dumps(asdict(usage), indent=2)


def show_once(args: argparse.Namespace) -> int:
    usage = latest_usage(args.codex_home.expanduser())
    if usage is None:
        print("No Codex token-usage events found. Run a Codex task first.", file=sys.stderr)
        return 1
    print(json_usage(usage) if args.json else render(usage, ansi=not args.no_color))
    return 0


def watch(args: argparse.Namespace) -> int:
    try:
        while True:
            usage = latest_usage(args.codex_home.expanduser())
            sys.stdout.write("\033[2J\033[H")
            if usage is None:
                print("Waiting for Codex usage data…")
            else:
                print(render(usage))
                if any(window.remaining_percent <= args.alert_below for window in usage.windows):
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
    show = subparsers.add_parser("show", help="print the latest usage snapshot")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    show.add_argument("--no-color", action="store_true")
    show.set_defaults(func=show_once)
    live = subparsers.add_parser("watch", help="continuously refresh the dashboard")
    live.add_argument("--interval", type=float, default=5.0)
    live.add_argument("--alert-below", type=float, default=10.0, metavar="PERCENT")
    live.set_defaults(func=watch)
    return result


def main() -> int:
    args = parser().parse_args()
    if getattr(args, "interval", 1) <= 0:
        print("error: --interval must be greater than zero", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
