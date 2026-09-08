#!/usr/bin/env python3
"""TTY model picker for the local claude-ark launcher."""

from __future__ import annotations

import argparse
import json
import os
import select
import sys
import termios
import time
import tty
from collections.abc import Callable
from pathlib import Path
from typing import TextIO


UP = b"\x1b[A"
DOWN = b"\x1b[B"
ENTER = (b"\r", b"\n")
ESCAPE = b"\x1b"
IDLE_AUTO_START_SECONDS = 3.0


def select_index(key_stream: bytes, initial_index: int, item_count: int) -> int:
    """Return the chosen index for a finite stream of terminal key bytes.

    Arrow presses move within the configured range. Enter confirms the current
    highlight. Escape and an empty stream deliberately retain the initial
    selection, which is the launcher's default model.
    """
    if item_count < 1:
        raise ValueError("item_count must be positive")
    if not 0 <= initial_index < item_count:
        raise ValueError("initial_index is outside the item range")

    current_index = initial_index
    position = 0
    while position < len(key_stream):
        if key_stream.startswith(UP, position):
            current_index = max(0, current_index - 1)
            position += len(UP)
        elif key_stream.startswith(DOWN, position):
            current_index = min(item_count - 1, current_index + 1)
            position += len(DOWN)
        elif key_stream[position : position + 1] in ENTER:
            return current_index
        elif key_stream[position : position + 1] == ESCAPE:
            return initial_index
        else:
            position += 1
    return initial_index


def read_terminal_key(file_descriptor: int, timeout_seconds: float | None = None) -> bytes | None:
    """Read one key, or return ``None`` if no key arrives before ``timeout``."""
    if not select.select([file_descriptor], [], [], timeout_seconds)[0]:
        return None
    first = os.read(file_descriptor, 1)
    if first != ESCAPE:
        return first
    if not select.select([file_descriptor], [], [], 0.05)[0]:
        return ESCAPE

    second = os.read(file_descriptor, 1)
    if second != b"[":
        return first + second
    if not select.select([file_descriptor], [], [], 0.05)[0]:
        return ESCAPE
    return first + second + os.read(file_descriptor, 1)


def select_with_idle_timeout(
    initial_index: int,
    item_count: int,
    read_key: Callable[[float | None], bytes | None],
    on_move: Callable[[int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Select a row, auto-confirming after 3 seconds unless navigation begins.

    The initial three-second window is intentionally only cancelled by Up or
    Down. This lets a user begin navigating without a timer racing them, while
    allowing an untouched picker to continue directly with its default.
    """
    if item_count < 1:
        raise ValueError("item_count must be positive")
    if not 0 <= initial_index < item_count:
        raise ValueError("initial_index is outside the item range")

    highlighted_index = initial_index
    auto_start_deadline: float | None = clock() + IDLE_AUTO_START_SECONDS
    while True:
        timeout_seconds = (
            None
            if auto_start_deadline is None
            else max(0.0, auto_start_deadline - clock())
        )
        key = read_key(timeout_seconds)
        if key is None:
            return highlighted_index
        if key == UP:
            highlighted_index = max(0, highlighted_index - 1)
            auto_start_deadline = None
            if on_move is not None:
                on_move(highlighted_index)
        elif key == DOWN:
            highlighted_index = min(item_count - 1, highlighted_index + 1)
            auto_start_deadline = None
            if on_move is not None:
                on_move(highlighted_index)
        elif key in ENTER:
            return highlighted_index
        elif key.startswith(ESCAPE):
            return initial_index


def render_picker(
    stream: TextIO,
    rows: list[tuple[str, str]],
    highlighted_index: int,
    previous_line_count: int | None,
) -> int:
    """Draw the selector in place and return its line count for the next redraw."""
    lines = [
        "",
        "Select configured model (↑/↓ move; auto-starts in 3s unless moved; Enter confirm; Esc selects default):",
    ]
    for index, (model, provider) in enumerate(rows):
        prefix = ">" if index == highlighted_index else " "
        row = f"{prefix} {index + 1:2d}) {model} [{provider}]"
        if index == highlighted_index:
            row = f"\x1b[1;32m{row}\x1b[0m"
        lines.append(row)

    if previous_line_count is not None:
        stream.write(f"\r\x1b[{previous_line_count - 1}A")
    stream.write("\x1b[J")
    # `tty.setraw()` disables ONLCR, so a bare LF would keep the current
    # column and make subsequent rows drift diagonally across the terminal.
    stream.write("\r\n".join(lines))
    stream.flush()
    return len(lines)


def select_interactively(rows: list[tuple[str, str]], initial_index: int) -> int:
    """Render the selector on stderr and return the selected row index."""
    file_descriptor = sys.stdin.fileno()
    previous_settings = termios.tcgetattr(file_descriptor)
    line_count: int | None = None

    try:
        tty.setraw(file_descriptor)
        sys.stderr.write("\x1b[?25l")
        line_count = render_picker(sys.stderr, rows, initial_index, line_count)

        def redraw(highlighted_index: int) -> None:
            nonlocal line_count
            line_count = render_picker(sys.stderr, rows, highlighted_index, line_count)

        return select_with_idle_timeout(
            initial_index=initial_index,
            item_count=len(rows),
            read_key=lambda timeout_seconds: read_terminal_key(file_descriptor, timeout_seconds),
            on_move=redraw,
        )
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, previous_settings)
        sys.stderr.write("\x1b[?25h\n")
        sys.stderr.flush()


def load_rows(model_map: Path) -> list[tuple[str, str]]:
    """Load configured model names and provider labels without reading key values."""
    mapping = json.loads(model_map.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise ValueError("model map must be a JSON object")
    rows = [(str(model), str(config.get("provider", "unknown"))) for model, config in mapping.items()]
    if not rows:
        raise ValueError("model map contains no configured models")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, required=True, dest="model_map")
    parser.add_argument("--default", required=True, dest="default_model")
    args = parser.parse_args()

    rows = load_rows(args.model_map)
    names = [model for model, _ in rows]
    if args.default_model not in names:
        parser.error(f"default model is not configured: {args.default_model}")
    default_index = names.index(args.default_model)

    selected_index = default_index
    if sys.stdin.isatty() and sys.stderr.isatty():
        selected_index = select_interactively(rows, default_index)
    sys.stdout.write(f"{rows[selected_index][0]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
