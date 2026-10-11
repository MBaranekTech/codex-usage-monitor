#!/usr/bin/env python3
"""Configure Codex's native TUI status line safely."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path


PRESETS = {
    "compact": [
        "model-with-reasoning",
        "project-name",
        "context-used",
        "five-hour-limit",
        "weekly-limit",
    ],
    "git": [
        "model-with-reasoning",
        "project-name",
        "git-branch",
        "context-used",
        "five-hour-limit",
        "weekly-limit",
    ],
    "full": [
        "run-state",
        "model-with-reasoning",
        "project-name",
        "git-branch",
        "context-used",
        "five-hour-limit",
        "weekly-limit",
        "used-tokens",
    ],
}

SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
KEY_RE = re.compile(r"^\s*(status_line|status_line_use_colors)\s*=")


def default_config_path() -> Path:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return root / "config.toml"


def toml_array(values: list[str]) -> str:
    return "[" + ", ".join(f'\"{value}\"' for value in values) + "]"


def validate_toml(text: str) -> None:
    tomllib.loads(text)


def assignment_end(lines: list[str], start: int) -> int:
    """Return the line after an assignment, including a multiline array."""
    balance = 0
    seen_array = False
    in_string = False
    escaped = False
    for index in range(start, len(lines)):
        for char in lines[index]:
            if escaped:
                escaped = False
                continue
            if char == "\\" and in_string:
                escaped = True
            elif char == '"':
                in_string = not in_string
            elif not in_string and char == "#":
                break
            elif not in_string and char == "[":
                seen_array = True
                balance += 1
            elif not in_string and char == "]":
                balance -= 1
        if not seen_array or balance <= 0:
            return index + 1
    return len(lines)


def find_tui_section(lines: list[str]) -> tuple[int, int] | None:
    start = None
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if not match:
            continue
        if start is not None:
            return start, index
        if match.group(1).strip() == "tui":
            start = index
    return (start, len(lines)) if start is not None else None


def remove_managed_assignments(lines: list[str], start: int, end: int) -> list[str]:
    result: list[str] = []
    index = start
    while index < end:
        if KEY_RE.match(lines[index]):
            index = assignment_end(lines, index)
        else:
            result.append(lines[index])
            index += 1
    return result


def update_config(text: str, items: list[str], colors: bool) -> str:
    lines = text.splitlines(keepends=True)
    setting_lines = [
        f"status_line = {toml_array(items)}\n",
        f"status_line_use_colors = {str(colors).lower()}\n",
    ]
    section = find_tui_section(lines)
    if section is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(["[tui]\n", *setting_lines])
    else:
        start, end = section
        body = remove_managed_assignments(lines, start + 1, end)
        lines = lines[: start + 1] + setting_lines + body + lines[end:]
    updated = "".join(lines)
    validate_toml(updated)
    return updated


def uninstall_config(text: str) -> str:
    lines = text.splitlines(keepends=True)
    section = find_tui_section(lines)
    if section is None:
        return text
    start, end = section
    body = remove_managed_assignments(lines, start + 1, end)
    updated = "".join(lines[: start + 1] + body + lines[end:])
    validate_toml(updated)
    return updated


def backup(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = path.with_name(f"{path.name}.statusline-{timestamp}.bak")
    shutil.copy2(path, destination)
    return destination


def write_config(path: Path, updated: str, dry_run: bool) -> Path | None:
    if dry_run:
        print(updated, end="")
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup(path) if path.exists() else None
    temporary = path.with_name(f".{path.name}.statusline.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)
    return backup_path


def codex_version() -> tuple[int, ...] | None:
    try:
        output = subprocess.run(
            ["codex", "--version"], check=True, capture_output=True, text=True
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    return tuple(map(int, match.groups())) if match else None


def command_install(args: argparse.Namespace) -> int:
    path = args.config.expanduser()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original:
        validate_toml(original)
    updated = update_config(original, PRESETS[args.preset], not args.no_colors)
    backup_path = write_config(path, updated, args.dry_run)
    if not args.dry_run:
        print(f"Installed the {args.preset!r} Codex status line in {path}")
        if backup_path:
            print(f"Backup: {backup_path}")
        print("Restart Codex, or start a new Codex session, to see it.")
    return 0


def command_uninstall(args: argparse.Namespace) -> int:
    path = args.config.expanduser()
    if not path.exists():
        print(f"Nothing to remove: {path} does not exist.")
        return 0
    original = path.read_text(encoding="utf-8")
    validate_toml(original)
    updated = uninstall_config(original)
    if updated == original:
        print("Nothing to remove: no managed status-line settings were found.")
        return 0
    backup_path = write_config(path, updated, args.dry_run)
    if not args.dry_run:
        print(f"Removed status-line settings from {path}")
        print(f"Backup: {backup_path}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    path = args.config.expanduser()
    problems = 0
    version = codex_version()
    if version is None:
        print("FAIL  Codex CLI was not found on PATH")
        problems += 1
    else:
        print(f"OK    Codex CLI {'.'.join(map(str, version))}")
    if not path.exists():
        print(f"FAIL  Config does not exist: {path}")
        return 1
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        print(f"OK    Valid TOML: {path}")
    except tomllib.TOMLDecodeError as error:
        print(f"FAIL  Invalid TOML: {error}")
        return 1
    items = data.get("tui", {}).get("status_line")
    if not items:
        print("FAIL  tui.status_line is not configured")
        problems += 1
    else:
        print(f"OK    Status line: {', '.join(items)}")
    return int(problems > 0)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Install a compact, usage-aware native Codex status line."
    )
    result.add_argument(
        "--config", type=Path, default=default_config_path(), help="config.toml path"
    )
    subparsers = result.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="install or update the status line")
    install.add_argument("--preset", choices=PRESETS, default="compact")
    install.add_argument("--no-colors", action="store_true")
    install.add_argument("--dry-run", action="store_true", help="print without writing")
    install.set_defaults(func=command_install)

    uninstall = subparsers.add_parser("uninstall", help="remove managed settings")
    uninstall.add_argument("--dry-run", action="store_true", help="print without writing")
    uninstall.set_defaults(func=command_uninstall)

    doctor = subparsers.add_parser("doctor", help="check Codex and its configuration")
    doctor.set_defaults(func=command_doctor)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        return args.func(args)
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
