#!/usr/bin/env python3
import os
import sys
import json
import shutil
import re
import tty
import termios
import tomllib
from pathlib import Path
from typing import Any, List, Optional, Tuple

CLEAR_SCREEN, HIDE_CURSOR, SHOW_CURSOR = "\x1B[2J\x1B[H", "\x1B[?25l", "\x1B[?25h"
ARROW_KEYS = {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left'}
CHAR_KEYS = {'\x01': 'ctrl+a', '\x03': 'ctrl+c', '\x06': 'ctrl+f', '\r': 'return', '\n': 'return', '\x08': 'backspace', '\x7f': 'backspace', ' ': 'space'}
KIND_FILTERS, SORT_MODES = ["all", "mcp", "skill", "agent", "plugin"], ["cli", "kind", "name", "state"]
RESOURCE_FILE_EXTS = [".md", ".json", ".toml", ".yaml", ".yml"]


def scandir(dir_path: str):
    try: return list(os.scandir(dir_path)) if os.path.isdir(dir_path) else []
    except OSError: return []

def numbered_key(key_name: str) -> bool: return bool(key_name and re.match(r"^[1-9]$", key_name))

def dedupe_resources(resources: List[dict]) -> List[dict]: return list({r["id"]: r for r in resources}.values())
def native_enabled(entry: Any) -> bool: return True if not isinstance(entry, dict) else entry.get("enabled") is not False and entry.get("disabled") is not True
def toml_key(key: str) -> str: return key if re.match(r"^[\w-]+$", key) else json.dumps(key)
def home_dir(options: dict = None) -> str: return (options or {}).get("homeDir") or os.path.expanduser("~")
def configured_extra_dirs(name: str, options: dict = None) -> List[str]:
    values = (options or {}).get(name) or os.environ.get(f"AGENT_SHARE_{name.upper()}", "")
    return [expand_home(x.strip(), options) for x in (values if isinstance(values, list) else values.split(os.pathsep)) if x.strip()]
def write_text(file_path: str, body: str): Path(file_path).parent.mkdir(parents=True, exist_ok=True); Path(file_path).write_text(body, encoding="utf-8")
def name_from_path(file_path: str) -> str: return path_obj.stem if (path_obj := Path(file_path)).suffix else path_obj.name

def compact_path(location: str) -> str: return "~" + location[len(home):] if (home := os.environ.get("HOME")) and (location == home or location.startswith(home + os.sep)) else location
def compact_resource_location(resource: dict) -> str:
    location = os.path.dirname(resource["path"]) if resource["kind"] in ["agent", "skill"] else resource["path"]
    return compact_path(location + os.sep if location != resource["path"] and not location.endswith(os.sep) else location)

def expand_home(input_path: str, options: dict = None) -> str: return home_dir(options) if input_path == "~" else (os.path.join(home_dir(options), input_path[2:]) if input_path.startswith("~/") else input_path)
def require_cli(specs: List[dict], cli_id: str) -> dict:
    res = next((spec for spec in specs if spec["id"] == cli_id), None)
    if not res: raise ValueError(f"Unknown CLI: {cli_id}")
    return res

def load_config(file_path: str, format_type: str) -> dict:
    try:
        val = json.loads(Path(file_path).read_text(encoding="utf-8")) if format_type == "json" else tomllib.loads(Path(file_path).read_text(encoding="utf-8"))
        return val if isinstance(val, dict) else {}
    except Exception: return {}

def write_config(file_path: str, format_type: str, data: dict): write_text(file_path, f"{json.dumps(data, indent=2)}\n" if format_type == "json" else dump_toml(data))
def disabled_path(options: dict) -> str: return os.path.join((options or {}).get("appDir") or os.path.join(home_dir(options), ".agent-share"), "disabled.json")
def read_disabled(options: dict) -> set: return {x for x in load_config(disabled_path(options), "json").get("disabled", []) if isinstance(x, str)}
def write_disabled(options: dict, disabled: set): write_text(disabled_path(options), f"{json.dumps({'disabled': sorted(disabled)}, indent=2)}\n")

def accepted_kinds(spec: dict) -> List[str]: return [kind for kind, ok in [("agent", spec["agentDirs"]), ("mcp", spec["mcpConfigs"]), ("skill", spec["skillLocations"] or spec["skillListFiles"]), ("plugin", spec.get("pluginLocations"))] if ok]

def make_file_resource(spec: dict, kind: str, path: str, disabled: set, forced_name: str = None) -> dict:
    real_path = os.path.realpath(path)
    details = {"path": path}
    if real_path != path: details["linkedTo"] = real_path
    return base_resource(spec, kind, forced_name or name_from_path(path), real_path, disabled, details=details)
def skill_list_dir(line: str, list_file: dict) -> str: return line if os.path.isabs(line) else os.path.abspath(os.path.join(os.path.dirname(list_file["file"]), line))
def scan_direct_directory(spec: dict, kind: str, dir_path: str, disabled: set) -> List[dict]: return [make_file_resource(spec, kind, entry.path, disabled) for entry in scandir(dir_path) if not entry.name.startswith(".") and (entry.is_dir() or os.path.splitext(entry.name)[1] in RESOURCE_FILE_EXTS)] if os.path.isdir(dir_path) else []
def spec_paths(spec: dict) -> List[str]: return [x["file"] for x in spec["mcpConfigs"]] + [x["dir"] for x in spec["skillLocations"]] + [p for x in spec["skillListFiles"] for p in [x["file"], x["fallbackDir"]]] + spec["agentDirs"] + [x["dir"] for x in spec.get("pluginLocations", [])]
def skill_root_for_target(target: dict) -> Optional[str]: return target["skillLocations"][0]["dir"] if target["skillLocations"] else (target["skillListFiles"][0]["fallbackDir"] if target["skillListFiles"] else None)
def plugin_root_for_target(target: dict) -> Optional[str]: return target.get("pluginLocations", [{}])[0].get("dir") if target.get("pluginLocations") else None
def skill_destination(resource: dict, target: dict) -> str:
    if not (target_root := skill_root_for_target(target)): raise ValueError(f"{target['label']} has no skill target")
    return unique_destination(os.path.join(target_root, os.path.basename(resource["sourcePath"])))
def plugin_destination(resource: dict, target: dict) -> str:
    if not (target_root := plugin_root_for_target(target)): raise ValueError(f"{target['label']} has no plugin target")
    return unique_destination(os.path.join(target_root, os.path.basename(resource["sourcePath"])))
def transfer_resource(resource: dict, target: dict): {"mcp": transfer_mcp, "skill": transfer_skill}.get(resource["kind"], transfer_tree)(resource, target)

def cycle_cli_filter(inventory_data: dict, current: str, delta=1) -> str: return (lambda ids: ids[(ids.index(current) + delta + len(ids)) % len(ids)])(["all"] + [c["id"] for c in inventory_data["clis"]])
def cycle_kind_filter(current: str) -> str: return KIND_FILTERS[(KIND_FILTERS.index(current) + 1) % len(KIND_FILTERS)]
def cycle_sort(current: str) -> str: return SORT_MODES[(SORT_MODES.index(current) + 1) % len(SORT_MODES)]
def move_selection(cursor: int, delta: int, size: int) -> int: return 0 if size <= 0 else (cursor + delta + size) % size

def logical_resource_keys(resources: List[dict]) -> set: return {(r["kind"], normalized_name(r["name"])) for r in resources}

def tool_display_name(tool: str) -> str:
    return {"Antigravity 2.0": "Agy", "Claude Code": "Claude", "Pi Agent": "Pi"}.get(tool, tool)

def resource_matches(resource: dict, state: dict) -> bool:
    query = state.get("searchQuery", "").strip().lower()
    text = ("\n".join(str(resource.get(k, "")) for k in ["cliId", "cliLabel", "kind", "name", "path", "sourcePath"]) + "\n" + json.dumps(resource.get("details", {}))).lower()
    return (state["cliFilter"] == "all" or resource["cliId"] == state["cliFilter"]) and (state["kindFilter"] == "all" or resource["kind"] == state["kindFilter"]) and (not query or query in text)
def normalized_match(resource: dict, candidate: dict) -> bool: return resource["kind"] == candidate["kind"] and normalized_name(resource["name"]) == normalized_name(candidate["name"])

def rows_for_state(inventory_data: dict, state: dict) -> List[dict]: return resource_rows(filtered_resources(inventory_data, state))
def clamp_cursor(state: dict, inventory_data: dict): state.__setitem__("cursor", max(0, min(state["cursor"], max(0, len(rows_for_state(inventory_data, state)) - 1))))
def selected_resources(inventory_data: dict, marked: set, fallback: Any) -> List[dict]: return (selected := [r for r in inventory_data["resources"] if r["id"] in marked]) or (fallback if isinstance(fallback, list) else ([fallback] if fallback else []))
def normalized_name(name: str) -> str: return re.sub(r"-\d+$", "", name)
def matching_resources(inventory_data: dict, cli_id: str, selected: List[dict]) -> List[dict]: return [cand for cand in inventory_data["resources"] if cand["cliId"] == cli_id and any(normalized_match(r, cand) for r in selected)]
def transfer_preview_path(resource: dict, target_id: str, options: dict) -> str:
    try: return compact_path(transfer_destination(resource, require_cli(cli_specs(options), target_id)))
    except Exception: return ""

def close_transfer(state: dict): state.update({"transferMode": False, "transferPaths": None, "transferCheckedTargets": set()})
def transfer_result_message(transferred: int, removed: int, target_count: int) -> str: return f"{', '.join(parts)} across {target_count} editor(s)." if (parts := ([f"transferred {transferred}"] if transferred else []) + ([f"removed {removed}"] if removed else [])) else "Nothing changed."
def remove_resources(resource_ids: List[str], options: dict) -> int:
    for r_id in resource_ids: apply_action({"resourceId": r_id, "type": "remove"}, options)
    return len(resource_ids)
def current_resources(inventory_data: dict, state: dict): return rows[state["cursor"]]["resources"] if state["cursor"] < len(rows := rows_for_state(inventory_data, state)) else None
def _get_selected_from_state(inventory_data: dict, state: dict): return selected_resources(inventory_data, state["marked"], current_resources(inventory_data, state))

def color_tool_text(value: str, tools: List[str], color: dict) -> str:
    for tool in sorted(tools, key=len, reverse=True):
        value = value.replace(tool, color["tool"](tool, tool))
    return value

def trim_cell(value: str, width: int) -> str: return value.ljust(width) if len(value) <= width else (value[:width] if width <= 1 else f"{value[:width-1]}…")
def visible_page(rows: List[dict], state: dict, height: int, search_visible: bool) -> Tuple[int, List[dict]]: return (start := max(0, min(state["cursor"] - (body_height := max(6, height - 14 - int(search_visible))) // 2, max(0, len(rows) - body_height)))), rows[start : start + body_height]

def resource_id(cli_id: str, kind: str, name: str, source_path: str) -> str:
    return f"{cli_id}:{kind}:{name}:{source_path}"

def toml_scalar(value: Any) -> str:
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, (int, float)): return str(value)
    if isinstance(value, str): return json.dumps(value)
    if isinstance(value, list): return f"[{', '.join(toml_scalar(x) for x in value)}]"
    return "{ " + ", ".join(f"{toml_key(k)} = {toml_scalar(val)}" for k, val in value.items()) + " }" if isinstance(value, dict) else '""'

def dump_toml(data: dict) -> str:
    lines = []
    def write_table(prefix: List[str], table: dict):
        for k, v in table.items():
            if not isinstance(v, dict): lines.append(f"{toml_key(k)} = {toml_scalar(v)}")
        for k, v in table.items():
            if isinstance(v, dict):
                if lines and lines[-1] != "": lines.append("")
                lines.append(f"[{'.'.join(toml_key(x) for x in prefix + [k])}]")
                write_table(prefix + [k], v)
    write_table([], data)
    return "\n".join(lines).strip() + "\n"


def cli_specs(options: dict = None) -> List[dict]:
    opts = options or {}
    def home(*parts): return expand_home(os.path.join("~", *parts), opts)
    def local(*parts): return os.path.join(opts.get("cwd") or os.getcwd(), *parts)
    def mcp(file, root_key, fmt="json"): return {"file": file, "format": fmt, "rootKeys": [root_key]}
    def skill(dir_val, rec=False, root_md=True): return {"dir": dir_val, "recursiveSkillMd": rec, "rootMarkdown": root_md}
    def plugin(dir_val, rec=False): return {"dir": dir_val, "recursiveManifests": rec}
    def skill_list(file, fallback_dir): return {"file": file, "fallbackDir": fallback_dir, "recursiveSkillMd": False, "rootMarkdown": True}

    dot_skills, local_dot_skills = home(".agents", "skills"), local(".agents", "skills")
    shared_skills = [skill(path, True, False) for path in discover_skill_roots(opts)]
    ag, ag_ide, pi = home(".gemini", "antigravity-cli"), home(".gemini", "antigravity"), home(".pi", "agent")

    specs = [
        {
            "id": "claude", "label": "Claude Code",
            "mcpConfigs": [mcp(home(".claude.json"), "mcpServers"), mcp(local(".mcp.json"), "mcpServers")],
            "skillLocations": [skill(home(".claude", "skills")), skill(local(".claude", "skills"))], "skillListFiles": [],
            "agentDirs": [home(".claude", "agents"), local(".claude", "agents")],
            "pluginLocations": [plugin(home(".claude", "plugins")), plugin(local(".claude", "plugins")), plugin(local(".claude-plugin"), True)]
        },
        {
            "id": "codex", "label": "Codex",
            "mcpConfigs": [mcp(home(".codex", "config.toml"), "mcp_servers", "toml"), mcp(local(".codex", "config.toml"), "mcp_servers", "toml")],
            "skillLocations": [skill(dot_skills, True, False), skill(local_dot_skills, True, False), *shared_skills, skill(home(".codex", "skills"), True)], "skillListFiles": [],
            "agentDirs": [home(".codex", "agents"), local(".codex", "agents")],
            "pluginLocations": [plugin(home(".codex", "plugins"), True), plugin(local(".codex", "plugins"), True), plugin(local(".codex-plugin"), True), plugin(local(".agents", "plugins"), True)]
        },
        {
            "id": "antigravity", "label": "Antigravity 2.0",
            "mcpConfigs": [mcp(os.path.join(ag, "mcp_config.json"), "mcpServers"), mcp(os.path.join(ag_ide, "mcp_config.json"), "mcpServers"), mcp(local(".agents", "mcp_config.json"), "mcpServers")],
            "skillLocations": [skill(os.path.join(ag, "skills")), skill(os.path.join(ag_ide, "skills")), skill(home(".gemini", "skills")), *shared_skills, skill(local_dot_skills, True, False), skill(local(".agent", "skills"), True, False)],
            "skillListFiles": [skill_list(os.path.join(ag, "skills.txt"), os.path.join(ag, "skills"))], "agentDirs": [os.path.join(ag, "agents")],
            "pluginLocations": [plugin(os.path.join(ag, "extensions")), plugin(os.path.join(ag_ide, "extensions")), plugin(home(".gemini", "extensions"), True), plugin(local(".gemini", "extensions"), True)]
        },
        {
            "id": "pi", "label": "Pi Agent",
            "mcpConfigs": [mcp(home(".config", "mcp", "mcp.json"), "mcpServers"), mcp(os.path.join(pi, "mcp.json"), "mcpServers"), mcp(local(".mcp.json"), "mcpServers"), mcp(local(".pi", "mcp.json"), "mcpServers")],
            "skillLocations": [skill(os.path.join(pi, "skills"), True), skill(dot_skills, True, False), *shared_skills, skill(local(".pi", "skills"), True), skill(local_dot_skills, True, False)], "skillListFiles": [],
            "agentDirs": [os.path.join(pi, "agents"), os.path.join(pi, "agent-suite", "agent-selection", "agents"), local(".pi", "agents")],
            "pluginLocations": [plugin(os.path.join(pi, "plugins"), True), plugin(os.path.join(pi, "plugins", "cache"), True), plugin(local(".pi", "plugins"), True)]
        }
    ]

    return specs


def read_frontmatter_value(text: str, key: str) -> Optional[str]:
    end = text.find("\n---", 3) if text.startswith("---") else -1
    return None if end == -1 else next((line[len(key)+1:].strip().strip("\"'") for line in text[3:end].splitlines() if line.startswith(f"{key}:")), None)


def base_resource(spec: dict, kind: str, name: str, path: str, disabled: set, **extra) -> dict:
    return (lambda res_id: {"id": res_id, "cliId": spec["id"], "cliLabel": spec["label"], "kind": kind, "name": name, "enabled": res_id not in disabled, "path": path, "sourcePath": path, **extra})(resource_id(spec["id"], kind, name, path))


def find_named_files(dir_path: str, file_name: str) -> List[str]:
    return sorted(path for entry in scandir(dir_path) for path in (find_named_files(entry.path, file_name) if entry.is_dir() else ([entry.path] if entry.is_file() and entry.name == file_name else [])))


def discover_skill_roots(options: dict = None) -> List[str]:
    return sorted(dict.fromkeys(configured_extra_dirs("skill_dirs", options)))


def scan_mcp(spec: dict, config: dict, disabled: set) -> List[dict]:
    if not os.path.exists(config["file"]): return []
    data, resources = load_config(config["file"], config["format"]), []
    for root_key in config["rootKeys"]:
        for name, server in (data.get(root_key) if isinstance(data.get(root_key), dict) else {}).items():
            resource = base_resource(spec, "mcp", name, config["file"], disabled, details=server if isinstance(server, dict) else {"value": server}, native={"file": config["file"], "format": config["format"], "rootKey": root_key, "name": name})
            resource["enabled"] = native_enabled(server) and resource["enabled"]
            resources.append(resource)
    return resources

def scan_skills(spec: dict, location: dict, disabled: set) -> List[dict]:
    if not os.path.isdir(location["dir"]): return []
    resources = []
    for entry in scandir(location["dir"]):
        if entry.name.startswith("."): continue
        if location["rootMarkdown"] and entry.is_file() and entry.name.endswith(".md"):
            resources.append(make_file_resource(spec, "skill", entry.path, disabled))
        if not location["recursiveSkillMd"] and entry.is_dir():
            resources.append(make_file_resource(spec, "skill", entry.path, disabled))
    if location["recursiveSkillMd"]:
        resources.extend(make_file_resource(spec, "skill", os.path.dirname(skill_file), disabled, skill_file_name(skill_file)) for skill_file in find_named_files(location["dir"], "SKILL.md"))
    return dedupe_resources(resources)

def skill_file_name(skill_file: str) -> Optional[str]:
    try: return read_frontmatter_value(Path(skill_file).read_text(encoding="utf-8"), "name")
    except Exception: return None


def scan_skill_list(spec: dict, list_file: dict, disabled: set) -> List[dict]:
    if not os.path.exists(list_file["file"]): return []
    try: lines = [ln.strip() for ln in Path(list_file["file"]).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    except Exception: lines = []
    owned = {loc["dir"] for loc in spec["skillLocations"]}
    return dedupe_resources([r for line in lines for dir_val in [skill_list_dir(line, list_file)] if dir_val not in owned for r in scan_skills(spec, {"dir": dir_val, "recursiveSkillMd": list_file["recursiveSkillMd"], "rootMarkdown": list_file["rootMarkdown"]}, disabled)])


def scan_configured_skills(spec: dict, disabled: set) -> List[dict]:
    resources = []
    for config in spec.get("mcpConfigs", []):
        if config.get("format") != "toml" or not os.path.exists(config["file"]): continue
        for item in load_config(config["file"], "toml").get("skills", {}).get("config", []):
            if not isinstance(item, dict) or not item.get("path"): continue
            path = item["path"]
            root = os.path.dirname(path) if os.path.basename(path).lower() == "skill.md" else path
            if os.path.exists(root):
                resource = make_file_resource(spec, "skill", root, disabled, skill_file_name(path) or name_from_path(root))
                resource["enabled"] = item.get("enabled") is not False and resource["enabled"]
                resource["native"] = {"file": config["file"], "format": "toml", "kind": "skill_config", "path": path}
                resources.append(resource)
    return dedupe_resources(resources)


def plugin_name(plugin_path: str) -> str:
    for rel in [".claude-plugin/plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json", "plugin.json", "package.json", "gemini-extension.json"]:
        manifest = os.path.join(plugin_path, rel) if os.path.isdir(plugin_path) else plugin_path
        if os.path.exists(manifest):
            try:
                data = load_config(manifest, "json")
                return str(data.get("name") or data.get("id") or name_from_path(plugin_path))
            except Exception: pass
    return name_from_path(plugin_path)


def scan_plugins(spec: dict, location: dict, disabled: set) -> List[dict]:
    root = location["dir"]
    if os.path.exists(os.path.join(root, "plugin.json")) or os.path.exists(os.path.join(root, "package.json")) or os.path.exists(os.path.join(root, "gemini-extension.json")):
        return [make_file_resource(spec, "plugin", root, disabled, plugin_name(root))]
    def direct_plugin(entry):
        return not entry.name.startswith(".") and (not location.get("recursiveManifests") and (entry.is_dir() or os.path.splitext(entry.name)[1] in RESOURCE_FILE_EXTS) or any(os.path.exists(os.path.join(entry.path, m)) for m in ["plugin.json", "package.json", "gemini-extension.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"]))
    resources = [make_file_resource(spec, "plugin", entry.path, disabled, plugin_name(entry.path)) for entry in scandir(root) if direct_plugin(entry)]
    if location.get("recursiveManifests"):
        direct_paths = {r["sourcePath"] for r in resources}
        manifest_dirs = {os.path.dirname(path) for name in ["plugin.json", "package.json", "gemini-extension.json"] for path in find_named_files(root, name)} - direct_paths
        resources.extend(make_file_resource(spec, "plugin", path, disabled, plugin_name(path)) for path in manifest_dirs)
    return dedupe_resources(resources)


def scan_spec(spec: dict, disabled: set, errors: List[str]) -> List[dict]:
    resources = []
    for config in spec["mcpConfigs"]:
        try: resources.extend(scan_mcp(spec, config, disabled))
        except Exception as e: errors.append(f"{spec['label']}: failed to scan {config['file']}: {str(e)}")
    for loc in spec["skillLocations"]: resources.extend(scan_skills(spec, loc, disabled))
    resources.extend(scan_configured_skills(spec, disabled))
    for lst in spec["skillListFiles"]: resources.extend(scan_skill_list(spec, lst, disabled))
    for dr in spec["agentDirs"]: resources.extend(scan_direct_directory(spec, "agent", dr, disabled))
    for loc in spec.get("pluginLocations", []): resources.extend(scan_plugins(spec, loc, disabled))
    return resources

def inventory(options: dict = None) -> dict:
    specs, disabled = cli_specs(options or {}), read_disabled(options or {})
    resources, errors, clis = [], [], []
    for spec in specs:
        found, paths = scan_spec(spec, disabled, errors), spec_paths(spec)
        resources.extend(found)

        clis.append({
            "accepts": accepted_kinds(spec),
            "id": spec["id"],
            "label": spec["label"],
            "status": "configured" if any(os.path.exists(p) for p in paths) else "missing",
            "count": len(found),
            "paths": paths
        })
    return {"clis": clis, "errors": errors, "resources": resources}

def resolve_action_resource(action: dict, options: dict) -> Tuple[dict, List[dict]]:
    specs = cli_specs(options)
    resource = next((item for item in inventory(options)["resources"] if item["id"] == action["resourceId"]), None)
    if not resource: raise ValueError(f"Resource not found: {action['resourceId']}")
    return resource, specs

def unique_destination(file_path: str) -> str:
    if not os.path.exists(file_path): return file_path
    path_obj = Path(file_path)
    ext, stem = ("" if path_obj.is_dir() else path_obj.suffix), (path_obj.name if path_obj.is_dir() else path_obj.stem)
    for i in range(2, 1000):
        candidate = os.path.join(path_obj.parent, f"{stem}-{i}{ext}")
        if not os.path.exists(candidate): return candidate
    raise RuntimeError(f"Could not allocate destination for {file_path}")


def transfer_destination(resource: dict, target: dict) -> str:
    if resource["kind"] == "mcp":
        if not target["mcpConfigs"]: raise ValueError(f"{target['label']} has no MCP config target")
        return target["mcpConfigs"][0]["file"]
    if resource["kind"] == "skill": return skill_destination(resource, target)
    if resource["kind"] == "plugin": return plugin_destination(resource, target)
    if not target["agentDirs"]: raise ValueError(f"{target['label']} has no {resource['kind']} target")
    return unique_destination(os.path.join(target["agentDirs"][0], os.path.basename(resource["sourcePath"])))


def apply_action(action: dict, options: dict = None):
    opts = options or {}
    resource, specs = resolve_action_resource(action, opts)

    if action["type"] == "set-enabled":
        if resource.get("native"): set_native_enabled(resource["native"], action["enabled"])
        else:
            disabled = read_disabled(opts)
            disabled.discard(resource["id"]) if action["enabled"] else disabled.add(resource["id"])
            write_disabled(opts, disabled)
    elif action["type"] == "remove": remove_resource(resource, opts)
    else: transfer_resource(resource, require_cli(specs, action["targetCliId"]))

def _get_native_root(native: dict) -> Tuple[dict, dict]:
    data = load_config(native["file"], native["format"])
    root = data.get(native["rootKey"])
    if not isinstance(root, dict): raise ValueError(f"{native['rootKey']} is not an object")
    return data, root

def set_native_enabled(native: dict, enabled: bool):
    if native.get("kind") == "skill_config":
        data = load_config(native["file"], native["format"])
        for item in data.get("skills", {}).get("config", []):
            if isinstance(item, dict) and item.get("path") == native["path"]: item["enabled"] = enabled
        write_config(native["file"], native["format"], data)
    else: set_native_mcp_enabled(native, enabled)

def set_native_mcp_enabled(native: dict, enabled: bool):
    data, root = _get_native_root(native)
    entry = root.get(native["name"])
    if not isinstance(entry, dict): raise ValueError(f"{native['name']} is not an object")
    entry["enabled"] = enabled
    if "disabled" in entry: entry["disabled"] = not enabled
    write_config(native["file"], native["format"], data)

def remove_native(native: dict):
    if native.get("kind") == "skill_config":
        data = load_config(native["file"], native["format"])
        items = data.get("skills", {}).get("config", [])
        if isinstance(items, list): data.setdefault("skills", {})["config"] = [x for x in items if not (isinstance(x, dict) and x.get("path") == native["path"])]
        write_config(native["file"], native["format"], data)
    else: remove_native_mcp(native)

def remove_native_mcp(native: dict):
    data, root = _get_native_root(native); root.pop(native["name"], None); data[native["rootKey"]] = root
    write_config(native["file"], native["format"], data)

def remove_resource(resource: dict, options: dict):
    if resource.get("native"): remove_native(resource["native"])
    else:
        if os.path.exists(resource["sourcePath"]):
            if os.path.isdir(resource["sourcePath"]): shutil.rmtree(resource["sourcePath"], ignore_errors=True)
            else: os.remove(resource["sourcePath"])

    disabled = read_disabled(options)
    if resource["id"] in disabled:
        disabled.remove(resource["id"]); write_disabled(options, disabled)

def copy_resource(source: str, destination: str):
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    if os.path.isdir(source):
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=lambda d, contents: [c for c in contents if c in [".git", ".in_use", "node_modules"]])
    else: shutil.copyfile(source, destination)

def ensure_skill_list_entry(file_path: str, dir_val: str):
    existing = [line.strip() for line in Path(file_path).read_text(encoding="utf-8").splitlines()] if os.path.exists(file_path) else []
    if dir_val not in existing:
        prefix = "\n" if existing and existing[-1] != "" else ""
        with open(file_path, "a", encoding="utf-8") as f: f.write(f"{prefix}{dir_val}\n")

def raw_mcp(resource: dict) -> dict:
    if not resource.get("native"): return {}
    native = resource["native"]
    root = load_config(native["file"], native["format"]).get(native["rootKey"])
    entry = root.get(native["name"]) if isinstance(root, dict) else None
    return json.loads(json.dumps(entry)) if isinstance(entry, dict) else {"value": entry}

def transfer_mcp(resource: dict, target: dict):
    if not target["mcpConfigs"]: raise ValueError(f"{target['label']} has no MCP config target")
    config = target["mcpConfigs"][0]
    if not config["rootKeys"]: raise ValueError(f"{target['label']} has no MCP root key")
    root_key = config["rootKeys"][0]
    data = load_config(config["file"], config["format"])
    data.setdefault(root_key, {})[resource["name"]] = raw_mcp(resource)
    write_config(config["file"], config["format"], data)

def transfer_tree(resource: dict, target: dict):
    if resource["kind"] == "plugin": destination = plugin_destination(resource, target)
    else:
        if not target["agentDirs"]: raise ValueError(f"{target['label']} has no {resource['kind']} target")
        destination = unique_destination(os.path.join(target["agentDirs"][0], os.path.basename(resource["sourcePath"])))
    copy_resource(resource["sourcePath"], destination)

def transfer_skill(resource: dict, target: dict):
    copy_resource(resource["sourcePath"], skill_destination(resource, target))
    if list_file := next((f for f in target["skillListFiles"] if f["fallbackDir"] == skill_root_for_target(target)), None):
        ensure_skill_list_entry(list_file["file"], list_file["fallbackDir"])

def default_state(msg=""):
    return {
        "cliFilter": "all", "cursor": 0, "kindFilter": "all",
        "marked": set(), "message": msg, "searchMode": False, "searchQuery": "",
        "sortBy": "cli", "transferCursor": 0, "transferMode": False, "transferPaths": None,
        "transferCheckedTargets": set()
    }

def make_resource_row(resources: List[dict]) -> dict:
    primary, enabled_values = resources[0], {r["enabled"] for r in resources}
    return {
        "enabled": primary["enabled"] if len(enabled_values) == 1 else "mixed",
        "id": primary["id"] if len(resources) == 1 else f"{primary['kind']}:{primary['name']}:" + ",".join(r["id"] for r in resources),
        "kind": primary["kind"], "locations": sorted({compact_resource_location(r) for r in resources}),
        "name": primary["name"], "resources": resources, "tools": sorted({tool_display_name(r['cliLabel']) for r in resources})
    }

def resource_sort_key(resource: dict, sort_by: str):
    keys = {"state": lambda r: (-int(r["enabled"]), r["name"]), "name": lambda r: r["name"], "kind": lambda r: f"{r['kind']}:{r['name']}", "cli": lambda r: f"{r['cliLabel']}:{r['kind']}:{r['name']}"}
    return keys.get(sort_by, keys["cli"])(resource)

def filtered_resources(inventory_data: dict, state: dict) -> List[dict]:
    return sorted(
        [r for r in inventory_data["resources"] if resource_matches(r, state)],
        key=lambda r: resource_sort_key(r, state["sortBy"])
    )

def resource_rows(resources: List[dict]) -> List[dict]:
    groups = {}
    for r in resources: groups.setdefault(f"{r['kind']}:{r['name']}", []).append(r)
    return [make_resource_row(sorted(items, key=lambda x: x["cliLabel"])) for items in groups.values()]


def build_transfer_menu(inventory_data: dict, marked: set, fallback=None) -> List[dict]:
    selected = selected_resources(inventory_data, marked, fallback or (inventory_data["resources"][0] if inventory_data["resources"] else None))
    selected_kinds, menu = {r["kind"] for r in selected}, []

    for cli in inventory_data["clis"]:
        if cli["status"] == "configured" and all(k in cli["accepts"] for k in selected_kinds):
            exist_ids = [r["id"] for r in matching_resources(inventory_data, cli["id"], selected)]
            menu.append({
                "existingResourceIds": exist_ids, "id": cli["id"], "label": cli["label"]
            })
    return sorted(menu, key=lambda x: x["label"])


def refresh_transfer_paths(inventory_data: dict, state: dict, options: dict):
    fallback = current_resources(inventory_data, state)
    selected = selected_resources(inventory_data, state["marked"], fallback)
    menu = build_transfer_menu(inventory_data, state["marked"] or {r["id"] for r in (fallback or [])}, fallback)

    paths_dict = {}
    for target in menu:
        if target["existingResourceIds"]:
            paths_dict[target["id"]] = ", ".join(compact_path(next((x["sourcePath"] for x in inventory_data["resources"] if x["id"] == target_id), target_id)) for target_id in target["existingResourceIds"])
        else:
            paths = [transfer_preview_path(r, target["id"], options) for r in selected]
            paths_dict[target["id"]] = ", ".join(filter(None, paths))
    state["transferPaths"] = paths_dict


def toggle_selected_enabled(inventory_data: dict, state: dict, options: dict):
    selected = _get_selected_from_state(inventory_data, state)
    for r in selected: apply_action({"enabled": not r["enabled"], "resourceId": r["id"], "type": "set-enabled"}, options)
    state["message"] = f"Toggled {len(selected)} item(s)." if selected else "Nothing selected."

def delete_selected(inventory_data: dict, state: dict, options: dict) -> bool:
    selected = _get_selected_from_state(inventory_data, state)
    if not selected:
        state.update({"message": "Nothing selected to delete."})
        return False

    remove_resources([r["id"] for r in selected], options)
    state.update({"message": f"Deleted {len(selected)} item(s)."})
    state["marked"].clear()
    return True

def handle_search_key(char: str, key_name: str, state: dict):
    if key_name == "escape" or char == "\x1B":
        state.update({"searchMode": False, "searchQuery": "", "cursor": 0, "message": "Search cleared."})
    elif key_name == "return":
        state.update({"searchMode": False, "message": f"Search: {state.get('searchQuery', '')}" if state.get("searchQuery") else "Search cleared."})
    elif key_name in ["backspace", "delete"]:
        state.update({"searchQuery": state.get("searchQuery", "")[:-1], "cursor": 0})
    elif char and len(char) == 1 and char >= " ":
        state.update({"searchQuery": state.get("searchQuery", "") + char, "cursor": 0})

def handle_main_key(key_name: str, state: dict, inventory_data: dict, options: dict) -> bool:
    rows = rows_for_state(inventory_data, state)

    if key_name in ["up", "down"]:
        state["cursor"] = move_selection(state["cursor"], -1 if key_name == "up" else 1, len(rows))
    elif key_name == "space" and state["cursor"] < len(rows):
        resource_ids = {r["id"] for r in rows[state["cursor"]]["resources"]}
        if resource_ids.issubset(state["marked"]): state["marked"] -= resource_ids
        else: state["marked"] |= resource_ids
    elif key_name in ["left", "right"]:
        state.update({"cliFilter": cycle_cli_filter(inventory_data, state["cliFilter"], -1 if key_name == "left" else 1), "cursor": 0})
    elif key_name == "k": state.update({"kindFilter": cycle_kind_filter(state["kindFilter"]), "cursor": 0})
    elif key_name == "s": state.update({"sortBy": cycle_sort(state["sortBy"]), "cursor": 0})
    elif key_name == "escape": state["message"] = "Delete cancelled."
    elif key_name == "r": state["message"] = "Refreshed."
    elif key_name == "e":
        toggle_selected_enabled(inventory_data, state, options)
        return True
    elif key_name in ["d", "delete"]: return delete_selected(inventory_data, state, options)
    elif key_name == "return":
        selected = _get_selected_from_state(inventory_data, state)
        checked = set()
        for cli in inventory_data["clis"]:
            if matching_resources(inventory_data, cli["id"], selected):
                checked.add(cli["id"])
        state.update({"transferCheckedTargets": checked, "transferCursor": 0, "transferMode": True, "message": "Use space/number keys to check targets. Enter to confirm, Escape to cancel."})
        refresh_transfer_paths(inventory_data, state, options)
    return False

def apply_transfer_targets(menu: List[dict], state: dict, selected: List[dict], options: dict) -> Tuple[int, int]:
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
                    apply_action({"resourceId": r["id"], "targetCliId": target_id, "type": "transfer"}, options)
                    transferred += 1
        else:
            if existing_cands:
                removed += remove_resources([c["id"] for c in existing_cands], options)
    return removed, transferred

def handle_transfer_key(key_name: str, state: dict, inventory_data: dict, options: dict) -> bool:
    fallback = current_resources(inventory_data, state)
    selected = _get_selected_from_state(inventory_data, state)
    menu = build_transfer_menu(inventory_data, state["marked"] or {r["id"] for r in (fallback or [])}, fallback)
    
    if key_name == "escape": close_transfer(state); state["message"] = "Transfer cancelled."
    elif key_name in ["up", "down"]: state["transferCursor"] = move_selection(state.get("transferCursor", 0), -1 if key_name == "up" else 1, len(menu))
    elif numbered_key(key_name) or key_name == "space":
        idx = state.get("transferCursor", 0) if key_name == "space" else int(key_name) - 1
        if idx < len(menu):
            state["transferCursor"] = idx
            target_id = menu[idx]["id"]
            if target_id in state["transferCheckedTargets"]: state["transferCheckedTargets"].remove(target_id)
            else: state["transferCheckedTargets"].add(target_id)
    elif key_name == "e":
        idx = state.get("transferCursor", 0)
        if idx < len(menu) and (existing := [r for r in inventory_data["resources"] if r["id"] in menu[idx]["existingResourceIds"]]):
            new_state = not all(r["enabled"] for r in existing)
            for r in existing: apply_action({"resourceId": r["id"], "type": "set-enabled", "enabled": new_state}, options)
            state["message"] = f"Toggled state in {menu[idx]['label']}."
            return True
        state["message"] = "Asset not present in target to toggle."
    elif key_name == "return":
        if not menu: state["message"] = "No available target CLI."; return False
        removed, transferred = apply_transfer_targets(menu, state, selected, options)
        state["marked"].clear(); close_transfer(state)
        state["message"] = transfer_result_message(transferred, removed, len(menu))
        return True
    return False

def get_palette(color_enabled: bool) -> dict:
    wrap = lambda code, val: f"\x1B[{code}m{val}\x1B[0m" if color_enabled else val
    tool_codes = {"Agy": "33", "Claude": "1;38;5;214", "Codex": "32", "Pi": "36"}
    return {
        "accent": lambda v: wrap("36", v), "heading": lambda v: wrap("1;37", v),
        "kind": lambda v: wrap("35", v), "muted": lambda v: wrap("2", v),
        "ok": lambda v: wrap("32", v), "selected": lambda v: wrap("7", v),
        "title": lambda v: wrap("1;36", v), "warn": lambda v: wrap("33", v),
        "tool": lambda t, v: wrap(tool_codes.get(t, "37"), v)
    }


def render_cli_nav(inventory_data: dict, state: dict, color: dict) -> str:
    count = lambda f: len(resource_rows(filtered_resources(inventory_data, {**state, "cliFilter": f})))
    first = color["selected"](f"All:{count('all')}") if state["cliFilter"] == "all" else f"All:{count('all')}"

    def cli_display(cli):
        if cli['id'] == 'antigravity': return 'agy'
        return cli['id']

    labels = [f"{cli_display(cli)}:{count(cli['id'])}" for cli in inventory_data["clis"]]
    return "  ".join([first] + [color["selected"](label) if state["cliFilter"] == cli["id"] else color["muted"](label) for label, cli in zip(labels, inventory_data["clis"])])


def render_top_bar(state: dict, color: dict) -> List[str]:
    key = lambda v: color["accent"](f"[{v}]")
    return [
        f"{color['title']('agent-share')}  {color['muted']('interactive asset manager')}",
        color["muted"](f"Actions {key('space')} mark {key('enter')} transfer {key('e')} toggle {key('d')} delete {key('k')} kind {key('s')} sort"),
        "",
        f"{color['heading']('Filters')}  {color['accent']('Kind: ' + state['kindFilter'])}  {color['accent']('Sort: ' + state['sortBy'])}  Marked: {len(state['marked'])}"
    ]

def render_resource_row(row: dict, index: int, selected: bool, state: dict, layout: dict, color: dict) -> str:
    marked_count = sum(1 for r in row["resources"] if r["id"] in state["marked"])
    checkbox = color["accent"]("[x]") if marked_count == len(row["resources"]) else (color["warn"]("[~]") if marked_count > 0 else "[ ]")
    status = color["warn"]("mixed") if row["enabled"] == "mixed" else (color["ok"]("on   ") if row["enabled"] else color["warn"]("off  "))
    return " ".join([
        f"{color['selected']('>') if selected else ' '} {str(index + 1).rjust(4)}.", checkbox,
        trim_cell(row["name"], layout["name"]), color["kind"](row["kind"].ljust(layout["kind"])),
        status,
        color_tool_text(trim_cell(", ".join(row["tools"]), layout["tools"]), row["tools"], color),
        trim_cell(", ".join(row["locations"]), layout["folder"])
    ])

def render_details(row: dict, color: dict) -> str:
    if not row: return f"{color['heading']('Details')}\n  No selected resource."
    status = color["warn"]("mixed") if row["enabled"] == "mixed" else (color["ok"]("enabled") if row["enabled"] else color["warn"]("disabled"))
    return f"{color['heading']('Details')}  {row['kind']} / {row['name']}  Tools: {color_tool_text(', '.join(row['tools']), row['tools'], color)}  State: {status}\n  Location: {', '.join(row['locations'])}"

def render_selected_summary(resources: List[dict], color: dict) -> List[str]:
    if not resources: return ["  No selected resource."]
    rows = resource_rows(resources)
    lines = [f"  {color['accent'](str(len(rows)))} row(s), {color['accent'](str(len(resources)))} resource(s) selected"]
    for row in rows[:8]:
        state = color["warn"]("mixed") if row["enabled"] == "mixed" else (color["ok"]("on") if row["enabled"] else color["warn"]("off"))
        tools = color_tool_text(", ".join(row["tools"]), row["tools"], color)
        lines.append(f"  - {row['kind']} {row['name']}  State: {state}  Tools: {tools}")
        lines.append(f"    {color['muted'](', '.join(row['locations']))}")
    if len(rows) > 8: lines.append(color["muted"](f"  ...and {len(rows) - 8} more"))
    return lines

def render_transfer_box(inventory_data: dict, state: dict, fallback: List[dict], color: dict) -> str:
    menu = build_transfer_menu(inventory_data, state["marked"], fallback)
    selected = selected_resources(inventory_data, state["marked"], fallback)
    lines = [
        color["heading"]("Transfer Assets"),
        color["muted"]("  space/number mark target, 'e' toggle status, enter confirm, escape cancel"),
        "",
        color["heading"]("Selected Assets"),
        *render_selected_summary(selected, color),
        "",
        color["heading"]("Targets")
    ]
    if not menu: return f"{lines[0]}\n  No available targets."
    for i, target in enumerate(menu):
        is_checked = target["id"] in state["transferCheckedTargets"]
        checkbox = color["accent"]("[x]") if is_checked else "[ ]"
        
        existing = [r for r in inventory_data["resources"] if r["id"] in target["existingResourceIds"]]
        status_note = f" [{color['ok']('on') if all(r['enabled'] for r in existing) else color['warn']('off')}]" if existing else ""
        
        path_preview = state.get('transferPaths', {}).get(target['id'], '')
        path_display = f"  -> {path_preview}" if path_preview else ""
        
        cursor = color['selected']('>') if i == state.get('transferCursor', 0) else ' '
        lines.append(f"{cursor} {color['muted'](str(i+1) + '.').rjust(3)} {checkbox} {target['label']}{status_note}{color['muted'](path_display)}")
    return "\n".join(lines)


def render_main_screen(inventory_data: dict, state: dict, options: dict = None) -> str:
    options = options or {}
    color = get_palette(options.get("color", False))
    rows = resource_rows(filtered_resources(inventory_data, state))
    selected = rows[state["cursor"]] if state["cursor"] < len(rows) else None
    search_visible = bool(state["searchMode"] or state.get("searchQuery"))
    start, page = visible_page(rows, state, options.get("height", shutil.get_terminal_size((110, 32)).lines), search_visible)
    layout = {"name": 28, "kind": 8, "folder": 34, "tools": 18}

    lines = render_top_bar(state, color)
    if search_visible:
        query = state.get("searchQuery", "")
        lines.append(f"{color['heading']('Search')}  {color['selected'](query if query else ' ') if state['searchMode'] else color['accent'](query)}")

    lines.extend([render_cli_nav(inventory_data, state, color), ""])
    if state["transferMode"]:
        lines.append(render_transfer_box(inventory_data, state, selected["resources"] if selected else None, color))
    else:
        lines.append(color["heading"]("Resources"))
        if not page: lines.append("  No resources match current filters.")
        else:
            lines.append(f"  {'#'.rjust(4)} {''.ljust(3)} {'Name'.ljust(layout['name'])} {'Kind'.ljust(layout['kind'])} State {'Tools'.ljust(layout['tools'])} {'Folder'.ljust(layout['folder'])}")
            lines.extend(render_resource_row(row, start + i, start + i == state["cursor"], state, layout, color) for i, row in enumerate(page))
        lines.extend(["", render_details(selected, color)])

    if inventory_data["errors"]:
        lines.extend(["", color["warn"]("Warnings")])
        lines.extend(f"  {warn}" for warn in inventory_data["errors"])

    lines.extend(["", color["muted"](state["message"])])
    return "\n".join(lines) + "\n"

def get_key() -> Tuple[str, Optional[str]]:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch in CHAR_KEYS: return CHAR_KEYS[ch], None
        if ch == '\x1b':
            if sys.stdin.read(1) == '[':
                c3 = sys.stdin.read(1)
                if c3 in ARROW_KEYS: return ARROW_KEYS[c3], None
                if c3 == '3' and sys.stdin.read(1) == '~': return 'delete', None
            return 'escape', None
        return ch, ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

def run_tui(options: dict = None):
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        sys.stdout.write(render_main_screen(inventory(options), default_state("Interactive mode needs a TTY.")))
        return

    sys.stdout.write(HIDE_CURSOR)
    current_inventory, state = inventory(options), default_state("Ready.")

    def render():
        sz = shutil.get_terminal_size()
        sys.stdout.write(CLEAR_SCREEN + render_main_screen(current_inventory, state, {"color": True, "height": sz.lines, "width": sz.columns}))
        sys.stdout.flush()

    def refresh():
        clamp_cursor(state, current_inventory)
        render()

    render()
    try:
        while True:
            key_name, char = get_key()
            try:
                if key_name in ["ctrl+c", "q"]: break

                if state["searchMode"]:
                    handle_search_key(char, key_name, state)
                elif key_name == "ctrl+f":
                    state.update({"searchMode": True, "message": "Search resources, enter keeps search, escape clears."})
                    render()
                    continue
                elif key_name == "ctrl+a":
                    rows = rows_for_state(current_inventory, state)
                    state["marked"].update(x["id"] for row in rows for x in row["resources"])
                    state["message"] = f"Marked {len(rows)} visible row(s)."
                elif state["transferMode"]:
                    if handle_transfer_key(key_name, state, current_inventory, options):
                        current_inventory = inventory(options)
                else:
                    if handle_main_key(key_name, state, current_inventory, options) or key_name == "r":
                        current_inventory = inventory(options)
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