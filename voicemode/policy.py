"""classify(): which tool calls run silently ("allow") and which need a spoken yes ("confirm").
Anything unknown or unparseable is "confirm"; never raises."""
from __future__ import annotations

import os
import re
import shlex

from voicemode import config


# Any of these anywhere (even in quotes) -> confirm: with no expansion/redirect/comment/glob
# characters present, shlex.split sees the command the way the shell does.
_FORBIDDEN = set("`$(){}<>\\#;!*?[]\n\r")


def _starts(tokens, prefixes):
    return any(tuple(tokens[:len(p)]) == p for p in prefixes)


def _bad_flag(prefix, tok, flags):
    base = tok.split("=", 1)[0]
    if base.startswith("--") and len(base) >= 3 and any(f.startswith(base) for f in flags):
        return True  # git/clap long-option abbreviation
    return base in flags or (base.startswith("-") and not base.startswith("--") and len(base) > 2
                             and any(f"-{ch}" in flags for ch in base[1:]))


def _go_ok(args):
    """Allowlisted flags only; package args must be . or ./... inside the project."""
    it = iter(args)
    for tok in it:
        if tok.startswith("-"):
            name, eq, _ = tok.lstrip("-").partition("=")
            if tok.startswith("---") or name not in config.GO_FLAGS:
                return False
            if name in config.GO_VALUE_FLAGS and not eq:
                next(it, None)
        elif not (tok == "." or tok.startswith("./")) or ".." in tok.split("/"):
            return False  # bare import paths (std, all, rsc.io/x) run code outside the project
    return True


def _bash_ok(cmd):
    if _FORBIDDEN & set(cmd) or "&" in cmd.replace("&&", ""):
        return False
    segs = [x for x in re.split(r"&&|\|\|?", cmd) if x.strip()]
    if not segs:
        return False
    for seg in segs:
        toks = shlex.split(seg)
        # env-var prefixes (GIT_EXTERNAL_DIFF=..., PATH=...) can run code -> confirm
        if (not toks or re.match(r"^[A-Za-z_]\w*=", toks[0]) or toks[0] in ("make", "npm") and len(toks) != 2
                or not _starts(toks, config.BASH_ALLOW) or _starts(toks, config.BASH_DESTRUCTIVE)):
            return False
        if toks[0] == "go" and not _go_ok(toks[2:]):
            return False
        for prefix, flags in config.BASH_DESTRUCTIVE_FLAGS:
            if _starts(toks, [prefix]) and any(_bad_flag(prefix, t, flags) for t in toks[len(prefix):]):
                return False
    return True


def _mcp_ok(name):
    parts = name.split("__", 2)
    if len(parts) < 3:
        return False
    tool = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", parts[2])
    tool = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", tool)
    words = {w.lower() for w in re.split(r"[_\-\s]+", tool) if w}
    return bool(words & config.MCP_READ_WORDS) and not words & config.MCP_WRITE_WORDS


_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _parts(path):
    return [c.casefold() for c in path.split(os.sep) if c]


def _write_ok(path, cwd):
    """Allow only inside the project (realpath, case-insensitive), never under .git/.claude or .mcp.json.
    No ~/$ expansion: the tools take paths literally, so anything needing it is confirm."""
    if not cwd or not isinstance(path, str) or "$" in path or "~" in path or ".." in path.split(os.sep):
        return False
    root = os.path.realpath(cwd)
    r = _parts(root)
    if _parts(os.path.realpath(os.path.expanduser("~")))[:len(r)] == r:
        return False  # cwd is /, home, or an ancestor of home
    p = _parts(os.path.realpath(os.path.join(root, path)))
    # .claude/ and .mcp.json hold hooks/permissions/MCP config: persistence, could disable voice mode
    return p[:len(r)] == r and ".git" not in p and ".claude" not in p and p[-1] != ".mcp.json"


def classify(tool_name, tool_input, cwd=None):
    try:
        if tool_name in _WRITE_TOOLS:
            inp = tool_input or {}
            paths = [inp[k] for k in ("file_path", "notebook_path") if k in inp]
            return "allow" if paths and all(_write_ok(p, cwd) for p in paths) else "confirm"
        if tool_name in config.SAFE_TOOLS:
            return "allow"
        if tool_name == "Bash":
            cmd = (tool_input or {}).get("command")
            return "allow" if isinstance(cmd, str) and _bash_ok(cmd) else "confirm"
        if isinstance(tool_name, str) and tool_name.startswith("mcp__"):
            return "allow" if _mcp_ok(tool_name) else "confirm"
    except Exception:
        pass
    return "confirm"
