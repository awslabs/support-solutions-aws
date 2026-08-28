# ============================================================
# FILE_NAME: config_store.py
# AUTHOR: vsharmro
# DATE: 2026-04-11
# VERSION: 1.0
# PURPOSE: Provides load, save, get, prompt.
# DEPENDENCIES: json, os
# EXPORTS: load, save, get, prompt
# ISSUE : None
# ============================================================
import json
import os

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fis_config.json")


# ============================================================
# NAME: load
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PURPOSE: Reads file content, builds a collection from results.
# CALLED BY: get, save
# ISSUE : None
# ============================================================
def load():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


# ============================================================
# NAME: save
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PARAMETERS: updates
# PURPOSE: Writes output to file.
# CALLED BY: prompt
# ISSUE : None
# ============================================================
def save(updates):
    config = load()
    config.update(updates)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


# ============================================================
# NAME: get
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PARAMETERS: key, default
# PURPOSE: Computes and returns a result.
# CALLED BY: prompt
# ISSUE : None
# ============================================================
def get(key, default=""):
    return load().get(key, default)


# ============================================================
# NAME: prompt
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PARAMETERS: label, key, default_override
# PURPOSE: Prompts user for input.
# ISSUE : None
# ============================================================
def prompt(label, key, default_override=None):
    saved = get(key, "")
    default = default_override if default_override and not saved else saved
    suffix = f" [{default}]" if default else ""
    value = input(f"  {label}{suffix}: ").strip()
    result = value if value else default
    if result:
        save({key: result})
    return result
