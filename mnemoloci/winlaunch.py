"""Command lines and .cmd launchers that survive any Windows path.

- cmd.exe reads a batch file in the OEM code page: `chcp 65001` first makes it read
  the rest as UTF-8, so "Città" or "José" paths work;
- `%` is expanded by cmd.exe even inside quotes: doubled in batch files;
- a quoted path ending in a backslash ("C:\\Users\\you\\") turns `\\"` into an escaped quote for
  the program's argv parser: a trailing "." keeps the same folder and the quote intact.
"""

from pathlib import Path

MAX_TASK_COMMAND = 261  # schtasks /TR limit


def arg(value: str | Path) -> str:
    """One quoted argument for a Windows command line."""
    text = str(value)
    if text.endswith("\\"):
        text += "."
    return f'"{text}"'


def batch_arg(value: str | Path) -> str:
    """One quoted argument inside a .cmd file."""
    return arg(value).replace("%", "%%")


def write_batch(target: Path, marker: str, lines: list[str]) -> None:
    body = ["@echo off", "chcp 65001>nul", f"rem {marker}", *lines]
    target.write_text("\r\n".join(body) + "\r\n", encoding="utf-8", newline="")  # no \r\r\n
