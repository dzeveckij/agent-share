import json
import os
import re
import sys
import termios
import tomllib
import tty
from pathlib import Path
from typing import Any, List, Optional, Tuple

ARROW_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left"}
CHAR_KEYS = {
    "\x01": "ctrl+a",
    "\x03": "ctrl+c",
    "\x06": "ctrl+f",
    "\r": "return",
    "\n": "return",
    "\x08": "backspace",
    "\x7f": "backspace",
    " ": "space",
}


def scandir(dir_path: str):
    try:
        return list(os.scandir(dir_path)) if os.path.isdir(dir_path) else []
    except OSError:
        return []


def home_dir(options: Optional[dict] = None) -> str:
    return (options or {}).get("homeDir") or os.path.expanduser("~")


def configured_extra_dirs(name: str, options: Optional[dict] = None) -> List[str]:
    values = (options or {}).get(name) or os.environ.get(
        f"AGENT_SHARE_{name.upper()}", ""
    )
    return [
        expand_home(x.strip(), options)
        for x in (values if isinstance(values, list) else values.split(os.pathsep))
        if x.strip()
    ]


def write_text(file_path: str, body: str):
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text(body, encoding="utf-8")


def name_from_path(file_path: str) -> str:
    return path_obj.stem if (path_obj := Path(file_path)).suffix else path_obj.name


def compact_path(location: str) -> str:
    return (
        "~" + location[len(home) :]
        if (home := os.environ.get("HOME"))
        and (location == home or location.startswith(home + os.sep))
        else location
    )


def expand_home(input_path: str, options: Optional[dict] = None) -> str:
    return (
        home_dir(options)
        if input_path == "~"
        else (
            os.path.join(home_dir(options), input_path[2:])
            if input_path.startswith("~/")
            else input_path
        )
    )


def load_config(file_path: str, format_type: str) -> dict:
    try:
        val = (
            json.loads(Path(file_path).read_text(encoding="utf-8"))
            if format_type == "json"
            else tomllib.loads(Path(file_path).read_text(encoding="utf-8"))
        )
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def write_config(file_path: str, format_type: str, data: dict):
    write_text(
        file_path,
        f"{json.dumps(data, indent=2)}\n" if format_type == "json" else dump_toml(data),
    )


def disabled_path(options: dict) -> str:
    return os.path.join(
        (options or {}).get("appDir")
        or os.path.join(home_dir(options), ".agent-share"),
        "disabled.json",
    )


def read_disabled(options: dict) -> set:
    return {
        x
        for x in load_config(disabled_path(options), "json").get("disabled", [])
        if isinstance(x, str)
    }


def write_disabled(options: dict, disabled: set):
    write_text(
        disabled_path(options),
        f"{json.dumps({'disabled': sorted(disabled)}, indent=2)}\n",
    )


def toml_key(key: Any) -> str:
    text = str(key)
    return text if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", text) else json.dumps(text)


def toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return f"[{', '.join(toml_scalar(x) for x in value)}]"
    return (
        "{ "
        + ", ".join(f"{toml_key(k)} = {toml_scalar(val)}" for k, val in value.items())
        + " }"
        if isinstance(value, dict)
        else '""'
    )


def dump_toml(data: dict) -> str:
    lines = []

    def write_table(prefix: List[str], table: dict):
        for k, v in table.items():
            if not isinstance(v, dict):
                lines.append(f"{toml_key(k)} = {toml_scalar(v)}")
        for k, v in table.items():
            if isinstance(v, dict):
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(f"[{'.'.join(toml_key(x) for x in prefix + [k])}]")
                write_table(prefix + [k], v)

    write_table([], data)
    return "\n".join(lines).strip() + "\n"


def get_key() -> Tuple[str, Optional[str]]:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch in CHAR_KEYS:
            return CHAR_KEYS[ch], None
        if ch == "\x1b":
            if sys.stdin.read(1) == "[":
                c3 = sys.stdin.read(1)
                if c3 in ARROW_KEYS:
                    return ARROW_KEYS[c3], None
                if c3 == "3" and sys.stdin.read(1) == "~":
                    return "delete", None
            return "escape", None
        return ch, ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
