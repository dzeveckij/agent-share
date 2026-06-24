import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, List, Optional, Tuple

from utils import (
    compact_path,
    configured_extra_dirs,
    expand_home,
    load_config,
    name_from_path,
    read_disabled,
    scandir,
    write_config,
    write_disabled,
)

RESOURCE_FILE_EXTS = [".md", ".json", ".toml", ".yaml", ".yml"]


def dedupe_resources(resources: List[dict]) -> List[dict]:
    return list({r["id"]: r for r in resources}.values())


def native_enabled(entry: Any) -> bool:
    return (
        True
        if not isinstance(entry, dict)
        else entry.get("enabled") is not False and entry.get("disabled") is not True
    )


def compact_resource_location(resource: dict) -> str:
    location = (
        os.path.dirname(resource["path"])
        if resource["kind"] in ["agent", "skill"]
        else resource["path"]
    )
    return compact_path(
        location + os.sep
        if location != resource["path"] and not location.endswith(os.sep)
        else location
    )


def require_cli(specs: List[dict], cli_id: str) -> dict:
    res = next((spec for spec in specs if spec["id"] == cli_id), None)
    if not res:
        raise ValueError(f"Unknown CLI: {cli_id}")
    return res


def accepted_kinds(spec: dict) -> List[str]:
    return [
        kind
        for kind, ok in [
            ("agent", spec["agentDirs"]),
            ("mcp", spec["mcpConfigs"]),
            ("skill", spec["skillLocations"] or spec["skillListFiles"]),
            ("plugin", spec.get("pluginLocations")),
        ]
        if ok
    ]


def make_file_resource(
    spec: dict, kind: str, path: str, disabled: set, forced_name: Optional[str] = None
) -> dict:
    real_path = os.path.realpath(path)
    details = {"path": path}
    if real_path != path:
        details["linkedTo"] = real_path
    return base_resource(
        spec,
        kind,
        forced_name or name_from_path(path),
        real_path,
        disabled,
        details=details,
    )


def skill_list_dir(line: str, list_file: dict) -> str:
    return (
        line
        if os.path.isabs(line)
        else os.path.abspath(os.path.join(os.path.dirname(list_file["file"]), line))
    )


def scan_direct_directory(
    spec: dict, kind: str, dir_path: str, disabled: set
) -> List[dict]:
    return (
        [
            make_file_resource(spec, kind, entry.path, disabled)
            for entry in scandir(dir_path)
            if not entry.name.startswith(".")
            and (
                entry.is_dir() or os.path.splitext(entry.name)[1] in RESOURCE_FILE_EXTS
            )
        ]
        if os.path.isdir(dir_path)
        else []
    )


def spec_paths(spec: dict) -> List[str]:
    return (
        spec.get("baseDirs", [])
        + [x["file"] for x in spec["mcpConfigs"]]
        + [x["dir"] for x in spec["skillLocations"]]
        + [p for x in spec["skillListFiles"] for p in [x["file"], x["fallbackDir"]]]
        + spec["agentDirs"]
        + [x["dir"] for x in spec.get("pluginLocations", [])]
    )


def skill_root_for_target(target: dict) -> Optional[str]:
    if target["id"] == "codex" and (
        codex_root := next(
            (
                x["dir"]
                for x in target["skillLocations"]
                if x["dir"].endswith(os.path.join(".codex", "skills"))
            ),
            None,
        )
    ):
        return codex_root
    return (
        target["skillLocations"][0]["dir"]
        if target["skillLocations"]
        else (
            target["skillListFiles"][0]["fallbackDir"]
            if target["skillListFiles"]
            else None
        )
    )


def plugin_root_for_target(target: dict) -> Optional[str]:
    return (
        target.get("pluginLocations", [{}])[0].get("dir")
        if target.get("pluginLocations")
        else None
    )


def skill_destination(resource: dict, target: dict) -> str:
    if not (target_root := skill_root_for_target(target)):
        raise ValueError(f"{target['label']} has no skill target")
    return unique_destination(
        os.path.join(target_root, os.path.basename(resource["sourcePath"]))
    )


def plugin_destination(resource: dict, target: dict) -> str:
    if not (target_root := plugin_root_for_target(target)):
        raise ValueError(f"{target['label']} has no plugin target")
    return unique_destination(
        os.path.join(target_root, os.path.basename(resource["sourcePath"]))
    )


def transfer_resource(resource: dict, target: dict):
    {"mcp": transfer_mcp, "skill": transfer_skill}.get(resource["kind"], transfer_tree)(
        resource, target
    )


def logical_resource_keys(resources: List[dict]) -> set:
    return {(r["kind"], normalized_name(r["name"])) for r in resources}


def tool_display_name(tool: str) -> str:
    return {"Agy": "Agy", "Claude Code": "Claude", "Pi Agent": "Pi"}.get(tool, tool)


def normalized_match(resource: dict, candidate: dict) -> bool:
    return resource["kind"] == candidate["kind"] and normalized_name(
        resource["name"]
    ) == normalized_name(candidate["name"])


def normalized_name(name: str) -> str:
    return re.sub(r"-\d+$", "", name)


def matching_resources(
    inventory_data: dict, cli_id: str, selected: List[dict]
) -> List[dict]:
    return [
        cand
        for cand in inventory_data["resources"]
        if cand["cliId"] == cli_id and any(normalized_match(r, cand) for r in selected)
    ]


def resource_id(cli_id: str, kind: str, name: str, source_path: str) -> str:
    return f"{cli_id}:{kind}:{name}:{source_path}"


def cli_specs(options: Optional[dict] = None) -> List[dict]:
    opts = options or {}

    def home(*parts):
        return expand_home(os.path.join("~", *parts), opts)

    def local(*parts):
        return os.path.join(opts.get("cwd") or os.getcwd(), *parts)

    def mcp(file, root_key, fmt="json"):
        return {"file": file, "format": fmt, "rootKeys": [root_key]}

    def skill(dir_val, rec=False, root_md=True):
        return {"dir": dir_val, "recursiveSkillMd": rec, "rootMarkdown": root_md}

    def plugin(dir_val, rec=False):
        return {"dir": dir_val, "recursiveManifests": rec}

    def skill_list(file, fallback_dir):
        return {
            "file": file,
            "fallbackDir": fallback_dir,
            "recursiveSkillMd": False,
            "rootMarkdown": True,
        }

    dot_skills, local_dot_skills = home(".agents", "skills"), local(".agents", "skills")
    shared_skills = [skill(path, True, False) for path in discover_skill_roots(opts)]
    ag_global, pi = home(".gemini", "config"), home(".pi", "agent")
    ag_global_mcp = mcp(os.path.join(ag_global, "mcp_config.json"), "mcpServers")
    ag_global_skill_lists = [
        skill_list(
            os.path.join(ag_global, "skills.txt"), os.path.join(ag_global, "skills")
        )
    ]

    specs = [
        {
            "id": "claude",
            "label": "Claude Code",
            "baseDirs": [home(".claude")],
            "mcpConfigs": [
                mcp(home(".claude.json"), "mcpServers"),
                mcp(local(".mcp.json"), "mcpServers"),
            ],
            "skillLocations": [
                skill(home(".claude", "skills")),
                skill(local(".claude", "skills")),
            ],
            "skillListFiles": [],
            "agentDirs": [home(".claude", "agents"), local(".claude", "agents")],
            "pluginLocations": [
                plugin(home(".claude", "plugins")),
                plugin(local(".claude", "plugins")),
                plugin(local(".claude-plugin"), True),
            ],
        },
        {
            "id": "codex",
            "label": "Codex",
            "baseDirs": [home(".codex")],
            "mcpConfigs": [
                mcp(home(".codex", "config.toml"), "mcp_servers", "toml"),
                mcp(local(".codex", "config.toml"), "mcp_servers", "toml"),
            ],
            "skillLocations": [
                skill(dot_skills, True, False),
                skill(local_dot_skills, True, False),
                *shared_skills,
                skill(home(".codex", "skills"), True),
            ],
            "skillListFiles": [],
            "agentDirs": [home(".codex", "agents"), local(".codex", "agents")],
            "pluginLocations": [
                plugin(home(".codex", "plugins"), True),
                plugin(local(".codex", "plugins"), True),
                plugin(local(".codex-plugin"), True),
                plugin(local(".agents", "plugins"), True),
            ],
        },
        {
            "id": "agy",
            "label": "Agy",
            "baseDirs": [ag_global],
            "mcpConfigs": [ag_global_mcp],
            "skillLocations": [skill(os.path.join(ag_global, "skills"), True, False)],
            "skillListFiles": ag_global_skill_lists,
            "agentDirs": [os.path.join(ag_global, "agents")],
            "pluginLocations": [plugin(os.path.join(ag_global, "plugins"), True)],
        },
        {
            "id": "pi",
            "label": "Pi Agent",
            "baseDirs": [pi],
            "mcpConfigs": [
                mcp(home(".config", "mcp", "mcp.json"), "mcpServers"),
                mcp(os.path.join(pi, "mcp.json"), "mcpServers"),
                mcp(local(".mcp.json"), "mcpServers"),
                mcp(local(".pi", "mcp.json"), "mcpServers"),
            ],
            "skillLocations": [
                skill(os.path.join(pi, "skills"), True),
                skill(dot_skills, True, False),
                *shared_skills,
                skill(local(".pi", "skills"), True),
                skill(local_dot_skills, True, False),
            ],
            "skillListFiles": [],
            "agentDirs": [
                os.path.join(pi, "agents"),
                os.path.join(pi, "agent-suite", "agent-selection", "agents"),
                local(".pi", "agents"),
            ],
            "pluginLocations": [
                plugin(os.path.join(pi, "plugins"), True),
                plugin(os.path.join(pi, "plugins", "cache"), True),
                plugin(local(".pi", "plugins"), True),
            ],
        },
    ]

    return specs


def read_frontmatter_value(text: str, key: str) -> Optional[str]:
    end = text.find("\n---", 3) if text.startswith("---") else -1
    return (
        None
        if end == -1
        else next(
            (
                line[len(key) + 1 :].strip().strip("\"'")
                for line in text[3:end].splitlines()
                if line.startswith(f"{key}:")
            ),
            None,
        )
    )


def base_resource(
    spec: dict, kind: str, name: str, path: str, disabled: set, **extra
) -> dict:
    return (
        lambda res_id: {
            "id": res_id,
            "cliId": spec["id"],
            "cliLabel": spec["label"],
            "kind": kind,
            "name": name,
            "enabled": res_id not in disabled,
            "path": path,
            "sourcePath": path,
            **extra,
        }
    )(resource_id(spec["id"], kind, name, path))


def find_named_files(dir_path: str, file_name: str) -> List[str]:
    return sorted(
        path
        for entry in scandir(dir_path)
        for path in (
            find_named_files(entry.path, file_name)
            if entry.is_dir()
            else ([entry.path] if entry.is_file() and entry.name == file_name else [])
        )
    )


def discover_skill_roots(options: Optional[dict] = None) -> List[str]:
    return sorted(dict.fromkeys(configured_extra_dirs("skill_dirs", options)))


def scan_mcp(spec: dict, config: dict, disabled: set) -> List[dict]:
    if not os.path.exists(config["file"]):
        return []
    data, resources = load_config(config["file"], config["format"]), []
    for root_key in config["rootKeys"]:
        root = data.get(root_key)
        if not isinstance(root, dict):
            continue
        for name, server in root.items():
            resource = base_resource(
                spec,
                "mcp",
                name,
                config["file"],
                disabled,
                details=server if isinstance(server, dict) else {"value": server},
                native={
                    "file": config["file"],
                    "format": config["format"],
                    "rootKey": root_key,
                    "name": name,
                },
            )
            resource["enabled"] = native_enabled(server) and resource["enabled"]
            resources.append(resource)
    return resources


def scan_skills(spec: dict, location: dict, disabled: set) -> List[dict]:
    if not os.path.isdir(location["dir"]):
        return []
    resources = []
    for entry in scandir(location["dir"]):
        if entry.name.startswith("."):
            continue
        if location["rootMarkdown"] and entry.is_file() and entry.name.endswith(".md"):
            resources.append(make_file_resource(spec, "skill", entry.path, disabled))
        if not location["recursiveSkillMd"] and entry.is_dir():
            resources.append(make_file_resource(spec, "skill", entry.path, disabled))
    if location["recursiveSkillMd"]:
        resources.extend(
            make_file_resource(
                spec,
                "skill",
                os.path.dirname(skill_file),
                disabled,
                skill_file_name(skill_file),
            )
            for skill_file in find_named_files(location["dir"], "SKILL.md")
        )
    return dedupe_resources(resources)


def skill_file_name(skill_file: str) -> Optional[str]:
    try:
        return read_frontmatter_value(
            Path(skill_file).read_text(encoding="utf-8"), "name"
        )
    except Exception:
        return None


def scan_skill_list(spec: dict, list_file: dict, disabled: set) -> List[dict]:
    if not os.path.exists(list_file["file"]):
        return []
    try:
        lines = [
            ln.strip()
            for ln in Path(list_file["file"]).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    except Exception:
        lines = []
    owned = {loc["dir"] for loc in spec["skillLocations"]}
    return dedupe_resources(
        [
            r
            for line in lines
            for dir_val in [skill_list_dir(line, list_file)]
            if dir_val not in owned
            for r in scan_skills(
                spec,
                {
                    "dir": dir_val,
                    "recursiveSkillMd": list_file["recursiveSkillMd"],
                    "rootMarkdown": list_file["rootMarkdown"],
                },
                disabled,
            )
        ]
    )


def scan_configured_skills(spec: dict, disabled: set) -> List[dict]:
    resources = []
    for config in spec.get("mcpConfigs", []):
        if config.get("format") != "toml" or not os.path.exists(config["file"]):
            continue
        for item in (
            load_config(config["file"], "toml").get("skills", {}).get("config", [])
        ):
            if not isinstance(item, dict) or not item.get("path"):
                continue
            path = item["path"]
            root = (
                os.path.dirname(path)
                if os.path.basename(path).lower() == "skill.md"
                else path
            )
            if os.path.exists(root):
                resource = make_file_resource(
                    spec,
                    "skill",
                    root,
                    disabled,
                    skill_file_name(path) or name_from_path(root),
                )
                resource["enabled"] = (
                    item.get("enabled") is not False and resource["enabled"]
                )
                resource["native"] = {
                    "file": config["file"],
                    "format": "toml",
                    "kind": "skill_config",
                    "path": path,
                }
                resources.append(resource)
    return dedupe_resources(resources)


def codex_plugin_config_file(spec: dict) -> Optional[str]:
    if spec["id"] != "codex":
        return None
    return next(
        (
            cfg["file"]
            for cfg in spec.get("mcpConfigs", [])
            if cfg.get("format") == "toml"
            and os.path.basename(cfg["file"]) == "config.toml"
        ),
        None,
    )


def codex_plugin_key(plugin_path: str, name: str) -> Optional[str]:
    parts = Path(plugin_path).parts
    try:
        cache_idx = len(parts) - 1 - parts[::-1].index("cache")
    except ValueError:
        return None
    if cache_idx + 2 >= len(parts):
        return None
    return f"{name}@{parts[cache_idx + 1]}"


def apply_codex_plugin_state(spec: dict, resource: dict):
    config_file = codex_plugin_config_file(spec)
    if not config_file or not os.path.exists(config_file):
        return
    key = codex_plugin_key(resource["sourcePath"], resource["name"])
    if not key:
        return
    entry = load_config(config_file, "toml").get("plugins", {}).get(key)
    if not isinstance(entry, dict):
        return
    resource["enabled"] = entry.get("enabled") is not False and resource["enabled"]
    resource["native"] = {
        "file": config_file,
        "format": "toml",
        "kind": "plugin_config",
        "key": key,
    }


def plugin_name(plugin_path: str) -> str:
    for rel in [
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        "plugin.json",
        "package.json",
        "gemini-extension.json",
    ]:
        manifest = (
            os.path.join(plugin_path, rel)
            if os.path.isdir(plugin_path)
            else plugin_path
        )
        if os.path.exists(manifest):
            try:
                data = load_config(manifest, "json")
                return str(
                    data.get("name") or data.get("id") or name_from_path(plugin_path)
                )
            except Exception:
                pass
    return name_from_path(plugin_path)


def scan_plugins(spec: dict, location: dict, disabled: set) -> List[dict]:
    root = location["dir"]
    if (
        os.path.exists(os.path.join(root, "plugin.json"))
        or os.path.exists(os.path.join(root, "package.json"))
        or os.path.exists(os.path.join(root, "gemini-extension.json"))
    ):
        resources = [
            make_file_resource(spec, "plugin", root, disabled, plugin_name(root))
        ]
        for resource in resources:
            apply_codex_plugin_state(spec, resource)
        return resources

    def direct_plugin(entry):
        return not entry.name.startswith(".") and (
            not location.get("recursiveManifests")
            and (
                entry.is_dir() or os.path.splitext(entry.name)[1] in RESOURCE_FILE_EXTS
            )
            or any(
                os.path.exists(os.path.join(entry.path, m))
                for m in [
                    "plugin.json",
                    "package.json",
                    "gemini-extension.json",
                    ".codex-plugin/plugin.json",
                    ".claude-plugin/plugin.json",
                ]
            )
        )

    resources = [
        make_file_resource(
            spec, "plugin", entry.path, disabled, plugin_name(entry.path)
        )
        for entry in scandir(root)
        if direct_plugin(entry)
    ]
    if location.get("recursiveManifests"):
        direct_paths = {r["sourcePath"] for r in resources}
        manifest_dirs = {
            os.path.dirname(path)
            for name in ["plugin.json", "package.json", "gemini-extension.json"]
            for path in find_named_files(root, name)
        } - direct_paths
        resources.extend(
            make_file_resource(spec, "plugin", path, disabled, plugin_name(path))
            for path in manifest_dirs
        )
    for resource in resources:
        apply_codex_plugin_state(spec, resource)
    return dedupe_resources(resources)


def scan_spec(spec: dict, disabled: set, errors: List[str]) -> List[dict]:
    resources = []
    for config in spec["mcpConfigs"]:
        try:
            resources.extend(scan_mcp(spec, config, disabled))
        except Exception as e:
            errors.append(f"{spec['label']}: failed to scan {config['file']}: {str(e)}")
    for loc in spec["skillLocations"]:
        resources.extend(scan_skills(spec, loc, disabled))
    resources.extend(scan_configured_skills(spec, disabled))
    for lst in spec["skillListFiles"]:
        resources.extend(scan_skill_list(spec, lst, disabled))
    for dr in spec["agentDirs"]:
        resources.extend(scan_direct_directory(spec, "agent", dr, disabled))
    for loc in spec.get("pluginLocations", []):
        resources.extend(scan_plugins(spec, loc, disabled))
    return resources


def inventory(options: Optional[dict] = None) -> dict:
    specs, disabled = cli_specs(options or {}), read_disabled(options or {})
    resources, errors, clis = [], [], []
    for spec in specs:
        found, paths = scan_spec(spec, disabled, errors), spec_paths(spec)
        resources.extend(found)

        clis.append(
            {
                "accepts": accepted_kinds(spec),
                "id": spec["id"],
                "label": spec["label"],
                "status": "configured"
                if any(os.path.exists(p) for p in paths)
                else "missing",
                "count": len(found),
                "paths": paths,
            }
        )
    return {"clis": clis, "errors": errors, "resources": resources}


def resolve_action_resource(action: dict, options: dict) -> Tuple[dict, List[dict]]:
    specs = cli_specs(options)
    resource = next(
        (
            item
            for item in inventory(options)["resources"]
            if item["id"] == action["resourceId"]
        ),
        None,
    )
    if not resource:
        raise ValueError(f"Resource not found: {action['resourceId']}")
    return resource, specs


def unique_destination(file_path: str) -> str:
    if not os.path.exists(file_path):
        return file_path
    path_obj = Path(file_path)
    ext, stem = (
        ("" if path_obj.is_dir() else path_obj.suffix),
        (path_obj.name if path_obj.is_dir() else path_obj.stem),
    )
    for i in range(2, 1000):
        candidate = os.path.join(path_obj.parent, f"{stem}-{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"Could not allocate destination for {file_path}")


def transfer_destination(resource: dict, target: dict) -> str:
    if resource["kind"] == "mcp":
        if not target["mcpConfigs"]:
            raise ValueError(f"{target['label']} has no MCP config target")
        return target["mcpConfigs"][0]["file"]
    if resource["kind"] == "skill":
        return skill_destination(resource, target)
    if resource["kind"] == "plugin":
        return plugin_destination(resource, target)
    if not target["agentDirs"]:
        raise ValueError(f"{target['label']} has no {resource['kind']} target")
    return unique_destination(
        os.path.join(target["agentDirs"][0], os.path.basename(resource["sourcePath"]))
    )


def apply_action(action: dict, options: Optional[dict] = None):
    opts = options or {}
    resource, specs = resolve_action_resource(action, opts)

    if action["type"] == "set-enabled":
        if resource.get("native"):
            set_native_enabled(resource["native"], action["enabled"])
        else:
            disabled = read_disabled(opts)
            disabled.discard(resource["id"]) if action["enabled"] else disabled.add(
                resource["id"]
            )
            write_disabled(opts, disabled)
    elif action["type"] == "remove":
        remove_resource(resource, opts)
    else:
        transfer_resource(resource, require_cli(specs, action["targetCliId"]))


def _get_native_root(native: dict) -> Tuple[dict, dict]:
    data = load_config(native["file"], native["format"])
    root = data.get(native["rootKey"])
    if not isinstance(root, dict):
        raise ValueError(f"{native['rootKey']} is not an object")
    return data, root


def set_native_enabled(native: dict, enabled: bool):
    if native.get("kind") == "skill_config":
        data = load_config(native["file"], native["format"])
        for item in data.get("skills", {}).get("config", []):
            if isinstance(item, dict) and item.get("path") == native["path"]:
                item["enabled"] = enabled
        write_config(native["file"], native["format"], data)
    elif native.get("kind") == "plugin_config":
        data = load_config(native["file"], native["format"])
        data.setdefault("plugins", {}).setdefault(native["key"], {})["enabled"] = (
            enabled
        )
        write_config(native["file"], native["format"], data)
    else:
        set_native_mcp_enabled(native, enabled)


def set_native_mcp_enabled(native: dict, enabled: bool):
    data, root = _get_native_root(native)
    entry = root.get(native["name"])
    if not isinstance(entry, dict):
        raise ValueError(f"{native['name']} is not an object")
    entry["enabled"] = enabled
    if "disabled" in entry:
        entry["disabled"] = not enabled
    write_config(native["file"], native["format"], data)


def remove_native(native: dict):
    if native.get("kind") == "skill_config":
        data = load_config(native["file"], native["format"])
        items = data.get("skills", {}).get("config", [])
        if isinstance(items, list):
            data.setdefault("skills", {})["config"] = [
                x
                for x in items
                if not (isinstance(x, dict) and x.get("path") == native["path"])
            ]
        write_config(native["file"], native["format"], data)
    elif native.get("kind") == "plugin_config":
        data = load_config(native["file"], native["format"])
        if isinstance(data.get("plugins"), dict):
            data["plugins"].pop(native["key"], None)
        write_config(native["file"], native["format"], data)
    else:
        remove_native_mcp(native)


def remove_native_mcp(native: dict):
    data, root = _get_native_root(native)
    root.pop(native["name"], None)
    data[native["rootKey"]] = root
    write_config(native["file"], native["format"], data)


def remove_resource(resource: dict, options: dict):
    if resource.get("native"):
        remove_native(resource["native"])
    if (
        not resource.get("native")
        or resource.get("native", {}).get("kind") == "plugin_config"
    ):
        if os.path.exists(resource["sourcePath"]):
            if os.path.isdir(resource["sourcePath"]):
                shutil.rmtree(resource["sourcePath"], ignore_errors=True)
            else:
                os.remove(resource["sourcePath"])

    disabled = read_disabled(options)
    if resource["id"] in disabled:
        disabled.remove(resource["id"])
        write_disabled(options, disabled)


def copy_resource(source: str, destination: str):
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    if os.path.isdir(source):
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=lambda d, contents: [
                c for c in contents if c in [".git", ".in_use", "node_modules"]
            ],
        )
    else:
        shutil.copyfile(source, destination)


def ensure_skill_list_entry(file_path: str, dir_val: str):
    existing = (
        [
            line.strip()
            for line in Path(file_path).read_text(encoding="utf-8").splitlines()
        ]
        if os.path.exists(file_path)
        else []
    )
    if dir_val not in existing:
        prefix = "\n" if existing and existing[-1] != "" else ""
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(f"{prefix}{dir_val}\n")


def raw_mcp(resource: dict) -> dict:
    if not resource.get("native"):
        return {}
    native = resource["native"]
    root = load_config(native["file"], native["format"]).get(native["rootKey"])
    entry = root.get(native["name"]) if isinstance(root, dict) else None
    return (
        json.loads(json.dumps(entry)) if isinstance(entry, dict) else {"value": entry}
    )


def transfer_mcp(resource: dict, target: dict):
    if not target["mcpConfigs"]:
        raise ValueError(f"{target['label']} has no MCP config target")
    config = target["mcpConfigs"][0]
    if not config["rootKeys"]:
        raise ValueError(f"{target['label']} has no MCP root key")
    root_key = config["rootKeys"][0]
    data = load_config(config["file"], config["format"])
    data.setdefault(root_key, {})[resource["name"]] = raw_mcp(resource)
    write_config(config["file"], config["format"], data)


def transfer_tree(resource: dict, target: dict):
    if resource["kind"] == "plugin":
        destination = plugin_destination(resource, target)
    else:
        if not target["agentDirs"]:
            raise ValueError(f"{target['label']} has no {resource['kind']} target")
        destination = unique_destination(
            os.path.join(
                target["agentDirs"][0], os.path.basename(resource["sourcePath"])
            )
        )
    copy_resource(resource["sourcePath"], destination)


def transfer_skill(resource: dict, target: dict):
    copy_resource(resource["sourcePath"], skill_destination(resource, target))
    if list_file := next(
        (
            f
            for f in target["skillListFiles"]
            if f["fallbackDir"] == skill_root_for_target(target)
        ),
        None,
    ):
        ensure_skill_list_entry(list_file["file"], list_file["fallbackDir"])
