#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config loader — reads config.yaml next to this file (copy from config.example.yaml).

Everything institution- or person-specific lives in config.yaml (git-ignored), NOT in
source. Credentials are never here — they stay in the DPAPI secret store (see README).
"""
import pathlib
import sys

_CFG_PATH = pathlib.Path(__file__).with_name("config.yaml")
_EXAMPLE = pathlib.Path(__file__).with_name("config.example.yaml")

_DEFAULTS = {
    "unpaywall_email": "",
    "paper_radar_db": "",
    "institution": {"sfx_base": "", "remote_auth_base": "", "proxy_suffix": ""},
    "rate": {"min_interval_s": 15, "contact": ""},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load():
    if not _CFG_PATH.exists():
        sys.stderr.write(
            f"⚠ config.yaml not found. Copy {_EXAMPLE.name} → config.yaml and fill in "
            f"your own values (your library's endpoints, your email).\n"
        )
        return dict(_DEFAULTS)
    try:
        import yaml
    except ImportError:
        sys.exit("Missing dependency: pip install pyyaml")
    with _CFG_PATH.open(encoding="utf-8") as fh:
        user = yaml.safe_load(fh) or {}
    return _deep_merge(_DEFAULTS, user)


CFG = _load()


def require(dotted_key: str) -> str:
    """Fetch a config value; exit with a clear message if it's blank (forces the user to
    fill config.yaml rather than silently falling back to someone else's institution)."""
    node = CFG
    for part in dotted_key.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    if not node:
        sys.exit(f"config.yaml is missing '{dotted_key}'. Fill it in (see config.example.yaml).")
    return node
