#!/usr/bin/env python3
import json
import re
import shutil
import sys
from typing import Any, List, Optional, Tuple

from manager import (
    apply_action,
    cli_specs,
    compact_resource_location,
    inventory,
    logical_resource_keys,
    matching_resources,
    normalized_name,
    require_cli,
    tool_display_name,
    transfer_destination,
)
from utils import compact_path, get_key

CLEAR_SCREEN, HIDE_CURSOR, SHOW_CURSOR = "\x1b[2J\x1b[H", "\x1b[?25l", "\x1b[?25h"
KIND_FILTERS, SORT_MODES = (
    ["all", "mcp", "skill", "agent", "plugin"],
    ["cli", "kind", "name", "state"],
)


def numbered_key(key_name: str) -> bool:
    return bool(key_name and re.match(r"^[1-9]$", key_name))


def cycle_cli_filter(inventory_data: dict, current: str, delta=1) -> str:
    return (lambda ids: ids[(ids.index(current) + delta + len(ids)) % len(ids)])(
        ["all"] + [c["id"] for c in inventory_data["clis"]]
    )


def cycle_kind_filter(current: str) -> str:
    return KIND_FILTERS[(KIND_FILTERS.index(current) + 1) % len(KIND_FILTERS)]


def cycle_sort(current: str) -> str:
    return SORT_MODES[(SORT_MODES.index(current) + 1) % len(SORT_MODES)]


def move_selection(cursor: int, delta: int, size: int) -> int:
    return 0 if size <= 0 else (cursor + delta + size) % size


def resource_matches(resource: dict, state: dict) -> bool:
    query = state.get("searchQuery", "").strip().lower()
    text = (
        "\n".join(
            str(resource.get(k, ""))
            for k in ["cliId", "cliLabel", "kind", "name", "path", "sourcePath"]
        )
        + "\n"
        + json.dumps(resource.get("details", {}))
    ).lower()
    return (
        (state["cliFilter"] == "all" or resource["cliId"] == state["cliFilter"])
        and (state["kindFilter"] == "all" or resource["kind"] == state["kindFilter"])
        and (not query or query in text)
    )


def rows_for_state(inventory_data: dict, state: dict) -> List[dict]:
    return resource_rows(filtered_resources(inventory_data, state))


def clamp_cursor(state: dict, inventory_data: dict):
    state.__setitem__(
        "cursor",
        max(
            0,
            min(
                state["cursor"], max(0, len(rows_for_state(inventory_data, state)) - 1)
            ),
        ),
    )


def selected_resources(inventory_data: dict, marked: set, fallback: Any) -> List[dict]:
    selected = [r for r in inventory_data["resources"] if r["id"] in marked]
    return selected or (
        fallback if isinstance(fallback, list) else ([fallback] if fallback else [])
    )


def transfer_preview_path(resource: dict, target_id: str, options: dict) -> str:
    try:
        return compact_path(
            transfer_destination(resource, require_cli(cli_specs(options), target_id))
        )
    except Exception:
        return ""


def close_transfer(state: dict):
    state.update(
        {"transferMode": False, "transferPaths": None, "transferCheckedTargets": set()}
    )


def transfer_result_message(transferred: int, removed: int, target_count: int) -> str:
    return (
        f"{', '.join(parts)} across {target_count} editor(s)."
        if (
            parts := ([f"transferred {transferred}"] if transferred else [])
            + ([f"removed {removed}"] if removed else [])
        )
        else "Nothing changed."
    )


def remove_resources(resource_ids: List[str], options: dict) -> int:
    for r_id in resource_ids:
        apply_action({"resourceId": r_id, "type": "remove"}, options)
    return len(resource_ids)


def current_resources(inventory_data: dict, state: dict):
    return (
        rows[state["cursor"]]["resources"]
        if state["cursor"] < len(rows := rows_for_state(inventory_data, state))
        else None
    )


def _get_selected_from_state(inventory_data: dict, state: dict):
    return selected_resources(
        inventory_data, state["marked"], current_resources(inventory_data, state)
    )


def color_tool_text(value: str, tools: List[str], color: dict) -> str:
    for tool in sorted(tools, key=len, reverse=True):
        value = value.replace(tool, color["tool"](tool, tool))
    return value


def trim_cell(value: str, width: int) -> str:
    return (
        value.ljust(width)
        if len(value) <= width
        else (value[:width] if width <= 1 else f"{value[: width - 1]}…")
    )


def visible_page(
    rows: List[dict], state: dict, height: int, search_visible: bool
) -> Tuple[int, List[dict]]:
    return (
        start := max(
            0,
            min(
                state["cursor"]
                - (body_height := max(6, height - 14 - int(search_visible))) // 2,
                max(0, len(rows) - body_height),
            ),
        )
    ), rows[start : start + body_height]


def default_state(msg=""):
    return {
        "cliFilter": "all",
        "cursor": 0,
        "kindFilter": "all",
        "marked": set(),
        "message": msg,
        "searchMode": False,
        "searchQuery": "",
        "sortBy": "cli",
        "transferCursor": 0,
        "transferMode": False,
        "transferPaths": None,
        "transferCheckedTargets": set(),
    }


def make_resource_row(resources: List[dict]) -> dict:
    primary, enabled_values = resources[0], {r["enabled"] for r in resources}
    return {
        "enabled": primary["enabled"] if len(enabled_values) == 1 else "mixed",
        "id": primary["id"]
        if len(resources) == 1
        else f"{primary['kind']}:{primary['name']}:"
        + ",".join(r["id"] for r in resources),
        "kind": primary["kind"],
        "locations": sorted({compact_resource_location(r) for r in resources}),
        "name": primary["name"],
        "resources": resources,
        "tools": sorted({tool_display_name(r["cliLabel"]) for r in resources}),
    }


def resource_sort_key(resource: dict, sort_by: str):
    keys = {
        "state": lambda r: (-int(r["enabled"]), r["name"]),
        "name": lambda r: r["name"],
        "kind": lambda r: f"{r['kind']}:{r['name']}",
        "cli": lambda r: f"{r['cliLabel']}:{r['kind']}:{r['name']}",
    }
    return keys.get(sort_by, keys["cli"])(resource)


def filtered_resources(inventory_data: dict, state: dict) -> List[dict]:
    return sorted(
        [r for r in inventory_data["resources"] if resource_matches(r, state)],
        key=lambda r: resource_sort_key(r, state["sortBy"]),
    )


def resource_rows(resources: List[dict]) -> List[dict]:
    groups = {}
    for r in resources:
        groups.setdefault(f"{r['kind']}:{r['name']}", []).append(r)
    return [
        make_resource_row(sorted(items, key=lambda x: x["cliLabel"]))
        for items in groups.values()
    ]


def build_transfer_menu(
    inventory_data: dict, marked: set, fallback: Any = None
) -> List[dict]:
    selected = selected_resources(
        inventory_data,
        marked,
        fallback
        or (inventory_data["resources"][0] if inventory_data["resources"] else None),
    )
    selected_kinds, menu = {r["kind"] for r in selected}, []

    for cli in inventory_data["clis"]:
        if cli["status"] == "configured" and all(
            k in cli["accepts"] for k in selected_kinds
        ):
            exist_ids = [
                r["id"] for r in matching_resources(inventory_data, cli["id"], selected)
            ]
            menu.append(
                {
                    "existingResourceIds": exist_ids,
                    "id": cli["id"],
                    "label": cli["label"],
                }
            )
    return sorted(menu, key=lambda x: x["label"])


def refresh_transfer_paths(inventory_data: dict, state: dict, options: dict):
    fallback = current_resources(inventory_data, state)
    selected = selected_resources(inventory_data, state["marked"], fallback)
    menu = build_transfer_menu(
        inventory_data, state["marked"] or {r["id"] for r in (fallback or [])}, fallback
    )

    paths_dict = {}
    for target in menu:
        if target["existingResourceIds"]:
            paths_dict[target["id"]] = ", ".join(
                compact_path(
                    next(
                        (
                            x["sourcePath"]
                            for x in inventory_data["resources"]
                            if x["id"] == target_id
                        ),
                        target_id,
                    )
                )
                for target_id in target["existingResourceIds"]
            )
        else:
            paths = [transfer_preview_path(r, target["id"], options) for r in selected]
            paths_dict[target["id"]] = ", ".join(filter(None, paths))
    state["transferPaths"] = paths_dict


def toggle_selected_enabled(inventory_data: dict, state: dict, options: dict):
    selected = _get_selected_from_state(inventory_data, state)
    for r in selected:
        apply_action(
            {"enabled": not r["enabled"], "resourceId": r["id"], "type": "set-enabled"},
            options,
        )
    state["message"] = (
        f"Toggled {len(selected)} item(s)." if selected else "Nothing selected."
    )


def delete_selected(inventory_data: dict, state: dict, options: dict) -> bool:
    selected = _get_selected_from_state(inventory_data, state)
    if not selected:
        state.update({"message": "Nothing selected to delete."})
        return False

    remove_resources([r["id"] for r in selected], options)
    state.update({"message": f"Deleted {len(selected)} item(s)."})
    state["marked"].clear()
    return True


def handle_search_key(char: Optional[str], key_name: str, state: dict):
    if key_name == "escape" or char == "\x1b":
        state.update(
            {
                "searchMode": False,
                "searchQuery": "",
                "cursor": 0,
                "message": "Search cleared.",
            }
        )
    elif key_name == "return":
        state.update(
            {
                "searchMode": False,
                "message": f"Search: {state.get('searchQuery', '')}"
                if state.get("searchQuery")
                else "Search cleared.",
            }
        )
    elif key_name in ["backspace", "delete"]:
        state.update({"searchQuery": state.get("searchQuery", "")[:-1], "cursor": 0})
    elif char and len(char) == 1 and char >= " ":
        state.update({"searchQuery": state.get("searchQuery", "") + char, "cursor": 0})


def handle_main_key(
    key_name: str, state: dict, inventory_data: dict, options: dict
) -> bool:
    rows = rows_for_state(inventory_data, state)

    if key_name in ["up", "down"]:
        state["cursor"] = move_selection(
            state["cursor"], -1 if key_name == "up" else 1, len(rows)
        )
    elif key_name == "space" and state["cursor"] < len(rows):
        resource_ids = {r["id"] for r in rows[state["cursor"]]["resources"]}
        if resource_ids.issubset(state["marked"]):
            state["marked"] -= resource_ids
        else:
            state["marked"] |= resource_ids
    elif key_name in ["left", "right"]:
        state.update(
            {
                "cliFilter": cycle_cli_filter(
                    inventory_data, state["cliFilter"], -1 if key_name == "left" else 1
                ),
                "cursor": 0,
            }
        )
    elif key_name == "k":
        state.update(
            {"kindFilter": cycle_kind_filter(state["kindFilter"]), "cursor": 0}
        )
    elif key_name == "s":
        state.update({"sortBy": cycle_sort(state["sortBy"]), "cursor": 0})
    elif key_name == "escape":
        state["message"] = "Delete cancelled."
    elif key_name == "r":
        state["message"] = "Refreshed."
    elif key_name == "e":
        toggle_selected_enabled(inventory_data, state, options)
        return True
    elif key_name in ["d", "delete"]:
        return delete_selected(inventory_data, state, options)
    elif key_name == "return":
        selected = _get_selected_from_state(inventory_data, state)
        checked = set()
        for cli in inventory_data["clis"]:
            if matching_resources(inventory_data, cli["id"], selected):
                checked.add(cli["id"])
        state.update(
            {
                "transferCheckedTargets": checked,
                "transferCursor": 0,
                "transferMode": True,
                "message": "Use space/number keys to check targets. Enter to confirm, Escape to cancel.",
            }
        )
        refresh_transfer_paths(inventory_data, state, options)
    return False


def apply_transfer_targets(
    menu: List[dict], state: dict, selected: List[dict], options: dict
) -> Tuple[int, int]:
    removed, transferred = 0, 0
    for target in menu:
        target_id = target["id"]
        wants_checked = target_id in state["transferCheckedTargets"]

        current_inv = inventory(options)
        existing_cands = matching_resources(current_inv, target_id, selected)

        if wants_checked:
            existing_keys = logical_resource_keys(existing_cands)
            for r in selected:
                if (r["kind"], normalized_name(r["name"])) not in existing_keys:
                    apply_action(
                        {
                            "resourceId": r["id"],
                            "targetCliId": target_id,
                            "type": "transfer",
                        },
                        options,
                    )
                    transferred += 1
        else:
            if existing_cands:
                removed += remove_resources([c["id"] for c in existing_cands], options)
    return removed, transferred


def handle_transfer_key(
    key_name: str, state: dict, inventory_data: dict, options: dict
) -> bool:
    fallback = current_resources(inventory_data, state)
    selected = _get_selected_from_state(inventory_data, state)
    menu = build_transfer_menu(
        inventory_data, state["marked"] or {r["id"] for r in (fallback or [])}, fallback
    )

    if key_name == "escape":
        close_transfer(state)
        state["message"] = "Transfer cancelled."
    elif key_name in ["up", "down"]:
        state["transferCursor"] = move_selection(
            state.get("transferCursor", 0), -1 if key_name == "up" else 1, len(menu)
        )
    elif numbered_key(key_name) or key_name == "space":
        idx = (
            state.get("transferCursor", 0) if key_name == "space" else int(key_name) - 1
        )
        if idx < len(menu):
            state["transferCursor"] = idx
            target_id = menu[idx]["id"]
            if target_id in state["transferCheckedTargets"]:
                state["transferCheckedTargets"].remove(target_id)
            else:
                state["transferCheckedTargets"].add(target_id)
    elif key_name == "e":
        idx = state.get("transferCursor", 0)
        if idx < len(menu) and (
            existing := [
                r
                for r in inventory_data["resources"]
                if r["id"] in menu[idx]["existingResourceIds"]
            ]
        ):
            new_state = not all(r["enabled"] for r in existing)
            for r in existing:
                apply_action(
                    {
                        "resourceId": r["id"],
                        "type": "set-enabled",
                        "enabled": new_state,
                    },
                    options,
                )
            state["message"] = f"Toggled state in {menu[idx]['label']}."
            return True
        state["message"] = "Asset not present in target to toggle."
    elif key_name == "return":
        if not menu:
            state["message"] = "No available target CLI."
            return False
        removed, transferred = apply_transfer_targets(menu, state, selected, options)
        state["marked"].clear()
        close_transfer(state)
        state["message"] = transfer_result_message(transferred, removed, len(menu))
        return True
    return False


def get_palette(color_enabled: bool) -> dict:
    def wrap(code, val):
        return f"\x1b[{code}m{val}\x1b[0m" if color_enabled else val

    tool_codes = {"Agy": "33", "Claude": "1;38;5;214", "Codex": "32", "Pi": "36"}
    return {
        "accent": lambda v: wrap("36", v),
        "heading": lambda v: wrap("1;37", v),
        "kind": lambda v: wrap("35", v),
        "muted": lambda v: wrap("2", v),
        "ok": lambda v: wrap("32", v),
        "selected": lambda v: wrap("7", v),
        "title": lambda v: wrap("1;36", v),
        "warn": lambda v: wrap("33", v),
        "tool": lambda t, v: wrap(tool_codes.get(t, "37"), v),
    }


def render_cli_nav(inventory_data: dict, state: dict, color: dict) -> str:
    def count(cli_filter):
        return len(
            resource_rows(filtered_resources(inventory_data, {**state, "cliFilter": cli_filter}))
        )

    first = (
        color["selected"](f"All:{count('all')}")
        if state["cliFilter"] == "all"
        else f"All:{count('all')}"
    )

    def cli_display(cli):
        return cli["id"]

    labels = [
        f"{cli_display(cli)}:{count(cli['id'])}" for cli in inventory_data["clis"]
    ]
    return "  ".join(
        [first]
        + [
            color["selected"](label)
            if state["cliFilter"] == cli["id"]
            else color["muted"](label)
            for label, cli in zip(labels, inventory_data["clis"])
        ]
    )


def render_top_bar(state: dict, color: dict) -> List[str]:
    def key(value):
        return color["accent"](f"[{value}]")

    return [
        f"{color['title']('agent-share')}  {color['muted']('interactive asset manager')}",
        color["muted"](
            f"Actions {key('space')} mark {key('enter')} transfer {key('e')} toggle {key('d')} delete {key('k')} kind {key('s')} sort"
        ),
        "",
        f"{color['heading']('Filters')}  {color['accent']('Kind: ' + state['kindFilter'])}  {color['accent']('Sort: ' + state['sortBy'])}  Marked: {len(state['marked'])}",
    ]


def render_resource_row(
    row: dict, index: int, selected: bool, state: dict, layout: dict, color: dict
) -> str:
    marked_count = sum(1 for r in row["resources"] if r["id"] in state["marked"])
    checkbox = (
        color["accent"]("[x]")
        if marked_count == len(row["resources"])
        else (color["warn"]("[~]") if marked_count > 0 else "[ ]")
    )
    status = (
        color["warn"]("mixed")
        if row["enabled"] == "mixed"
        else (color["ok"]("on   ") if row["enabled"] else color["warn"]("off  "))
    )
    return " ".join(
        [
            f"{color['selected']('>') if selected else ' '} {str(index + 1).rjust(4)}.",
            checkbox,
            trim_cell(row["name"], layout["name"]),
            color["kind"](row["kind"].ljust(layout["kind"])),
            status,
            color_tool_text(
                trim_cell(", ".join(row["tools"]), layout["tools"]), row["tools"], color
            ),
            trim_cell(", ".join(row["locations"]), layout["folder"]),
        ]
    )


def render_details(row: Optional[dict], color: dict) -> str:
    if not row:
        return f"{color['heading']('Details')}\n  No selected resource."
    status = (
        color["warn"]("mixed")
        if row["enabled"] == "mixed"
        else (color["ok"]("enabled") if row["enabled"] else color["warn"]("disabled"))
    )
    return f"{color['heading']('Details')}  {row['kind']} / {row['name']}  Tools: {color_tool_text(', '.join(row['tools']), row['tools'], color)}  State: {status}\n  Location: {', '.join(row['locations'])}"


def render_selected_summary(resources: List[dict], color: dict) -> List[str]:
    if not resources:
        return ["  No selected resource."]
    rows = resource_rows(resources)
    lines = [
        f"  {color['accent'](str(len(rows)))} row(s), {color['accent'](str(len(resources)))} resource(s) selected"
    ]
    for row in rows[:8]:
        state = (
            color["warn"]("mixed")
            if row["enabled"] == "mixed"
            else (color["ok"]("on") if row["enabled"] else color["warn"]("off"))
        )
        tools = color_tool_text(", ".join(row["tools"]), row["tools"], color)
        lines.append(f"  - {row['kind']} {row['name']}  State: {state}  Tools: {tools}")
        lines.append(f"    {color['muted'](', '.join(row['locations']))}")
    if len(rows) > 8:
        lines.append(color["muted"](f"  ...and {len(rows) - 8} more"))
    return lines


def render_transfer_box(
    inventory_data: dict, state: dict, fallback: Optional[List[dict]], color: dict
) -> str:
    menu = build_transfer_menu(inventory_data, state["marked"], fallback)
    selected = selected_resources(inventory_data, state["marked"], fallback)
    lines = [
        color["heading"]("Transfer Assets"),
        color["muted"](
            "  space/number mark target, 'e' toggle status, enter confirm, escape cancel"
        ),
        "",
        color["heading"]("Selected Assets"),
        *render_selected_summary(selected, color),
        "",
        color["heading"]("Targets"),
    ]
    if not menu:
        return f"{lines[0]}\n  No available targets."
    for i, target in enumerate(menu):
        is_checked = target["id"] in state["transferCheckedTargets"]
        checkbox = color["accent"]("[x]") if is_checked else "[ ]"

        existing = [
            r
            for r in inventory_data["resources"]
            if r["id"] in target["existingResourceIds"]
        ]
        status_note = (
            f" [{color['ok']('on') if all(r['enabled'] for r in existing) else color['warn']('off')}]"
            if existing
            else ""
        )

        path_preview = state.get("transferPaths", {}).get(target["id"], "")
        path_display = f"  -> {path_preview}" if path_preview else ""

        cursor = color["selected"](">") if i == state.get("transferCursor", 0) else " "
        lines.append(
            f"{cursor} {color['muted'](str(i + 1) + '.').rjust(3)} {checkbox} {target['label']}{status_note}{color['muted'](path_display)}"
        )
    return "\n".join(lines)


def render_main_screen(
    inventory_data: dict, state: dict, options: Optional[dict] = None
) -> str:
    options = options or {}
    color = get_palette(options.get("color", False))
    rows = resource_rows(filtered_resources(inventory_data, state))
    selected = rows[state["cursor"]] if state["cursor"] < len(rows) else None
    search_visible = bool(state["searchMode"] or state.get("searchQuery"))
    start, page = visible_page(
        rows,
        state,
        options.get("height", shutil.get_terminal_size((110, 32)).lines),
        search_visible,
    )
    layout = {"name": 28, "kind": 8, "folder": 34, "tools": 18}

    lines = render_top_bar(state, color)
    if search_visible:
        query = state.get("searchQuery", "")
        lines.append(
            f"{color['heading']('Search')}  {color['selected'](query if query else ' ') if state['searchMode'] else color['accent'](query)}"
        )

    lines.extend([render_cli_nav(inventory_data, state, color), ""])
    if state["transferMode"]:
        lines.append(
            render_transfer_box(
                inventory_data,
                state,
                selected["resources"] if selected else None,
                color,
            )
        )
    else:
        lines.append(color["heading"]("Resources"))
        if not page:
            lines.append("  No resources match current filters.")
        else:
            lines.append(
                f"  {'#'.rjust(4)} {''.ljust(3)} {'Name'.ljust(layout['name'])} {'Kind'.ljust(layout['kind'])} State {'Tools'.ljust(layout['tools'])} {'Folder'.ljust(layout['folder'])}"
            )
            lines.extend(
                render_resource_row(
                    row, start + i, start + i == state["cursor"], state, layout, color
                )
                for i, row in enumerate(page)
            )
        lines.extend(["", render_details(selected, color)])

    if inventory_data["errors"]:
        lines.extend(["", color["warn"]("Warnings")])
        lines.extend(f"  {warn}" for warn in inventory_data["errors"])

    lines.extend(["", color["muted"](state["message"])])
    return "\n".join(lines) + "\n"


def run_tui(options: Optional[dict] = None):
    opts = options or {}
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        sys.stdout.write(
            render_main_screen(
                inventory(opts), default_state("Interactive mode needs a TTY.")
            )
        )
        return

    sys.stdout.write(HIDE_CURSOR)
    current_inventory, state = inventory(opts), default_state("Ready.")

    def render():
        sz = shutil.get_terminal_size()
        sys.stdout.write(
            CLEAR_SCREEN
            + render_main_screen(
                current_inventory,
                state,
                {"color": True, "height": sz.lines, "width": sz.columns},
            )
        )
        sys.stdout.flush()

    def refresh():
        clamp_cursor(state, current_inventory)
        render()

    render()
    try:
        while True:
            key_name, char = get_key()
            try:
                if key_name in ["ctrl+c", "q"]:
                    break

                if state["searchMode"]:
                    handle_search_key(char, key_name, state)
                elif key_name == "ctrl+f":
                    state.update(
                        {
                            "searchMode": True,
                            "message": "Search resources, enter keeps search, escape clears.",
                        }
                    )
                    render()
                    continue
                elif key_name == "ctrl+a":
                    rows = rows_for_state(current_inventory, state)
                    state["marked"].update(
                        x["id"] for row in rows for x in row["resources"]
                    )
                    state["message"] = f"Marked {len(rows)} visible row(s)."
                elif state["transferMode"]:
                    if handle_transfer_key(key_name, state, current_inventory, opts):
                        current_inventory = inventory(opts)
                else:
                    if (
                        handle_main_key(key_name, state, current_inventory, opts)
                        or key_name == "r"
                    ):
                        current_inventory = inventory(opts)
                refresh()
            except Exception as e:
                state["message"] = str(e)
                render()
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        run_tui({})
    except Exception as error:
        sys.stderr.write(f"{error}\n")
        sys.exit(1)
