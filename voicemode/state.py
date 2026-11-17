"""Per-session state and utterance queue under VOICE_MODE_HOME. Stdlib only (hooks import it)."""
from __future__ import annotations

import fcntl, json, os, signal, subprocess, time
from pathlib import Path

STALE_SPEAKING = 130


def home() -> Path:
    return Path(os.environ.get("VOICE_MODE_HOME", "~/.claude-voice-mode")).expanduser()


def _dir(sid) -> Path:
    d = home() / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(p: Path, s: str):  # atomic: Stop hook and listener run concurrently
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(s, encoding="utf-8")
    os.replace(tmp, p)


def _read(p: Path):
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _rm(p: Path):
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def active_session():
    return _read(home() / "active") or None


def turn_on(sid):
    prev = active_session()
    set_cursor(sid, queue_len(sid))  # earlier utterances must not run as commands
    clear_stopped(sid)
    (_dir(sid) / "on").touch()
    _write(home() / "active", sid)
    return prev if prev != sid else None


def turn_off(sid):
    _rm(home() / sid / "on")
    if active_session() == sid:
        _rm(home() / "active")
    pid = listener_running(sid)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for name in ("listener.pid", "queue.jsonl", "cursor"):  # the queue is a transcript of the room
        _rm(home() / sid / name)


def is_on(sid):
    return (home() / sid / "on").exists() and active_session() == sid


def set_stopped(sid):  # a spoken stop holds every tool call until the next Stop hook
    (_dir(sid) / "stopped").touch()


def clear_stopped(sid):
    _rm(home() / sid / "stopped")


def is_stopped(sid):
    return (home() / sid / "stopped").exists()


def append_utterance(sid, ts, text):
    with open(_dir(sid) / "queue.jsonl", "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"ts": ts, "text": text}, ensure_ascii=False) + "\n")


def _lines(sid):
    try:
        raw = (home() / sid / "queue.jsonl").read_text(encoding="utf-8")
    except OSError:
        return []
    return raw.split("\n")[:-1]  # last element is "" or an unterminated partial line


def read_utterances(sid, start=0):
    out = []
    for i, line in enumerate(_lines(sid)):
        if i < start:
            continue
        try:
            d = json.loads(line)
            out.append((i, float(d["ts"]), str(d["text"])))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def queue_len(sid):
    return len(_lines(sid))


def get_cursor(sid):
    try:
        return int(_read(home() / sid / "cursor"))
    except (TypeError, ValueError):
        return 0


def set_cursor(sid, idx):
    _write(_dir(sid) / "cursor", str(idx))


def set_speaking(on):
    home().mkdir(parents=True, exist_ok=True)
    _write(home() / "speaking", "1" if on else "0")  # mtime = last change


def speaking_recently(grace=0.5):
    p = home() / "speaking"
    try:
        age = time.time() - p.stat().st_mtime
        # a "1" mark older than STALE_SPEAKING is a crashed `say`: don't stay deaf forever
        return age < grace or (p.read_text(encoding="utf-8") == "1" and age < STALE_SPEAKING)
    except OSError:
        return False


def save_listener_pid(sid, pid):
    _write(_dir(sid) / "listener.pid", str(pid))


def listener_pid(sid):
    try:
        return int(_read(home() / sid / "listener.pid"))
    except (TypeError, ValueError):
        return None


def is_listener(pid, sid):
    """`pid` is a live listen.py for this session, not a dead listener's pid reused by another process."""
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                             timeout=5).stdout.decode(errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return "listen.py" in cmd and f" --session {sid} " in f" {cmd} "


def listener_running(sid):
    """This session's listener pid if it is really running, else None."""
    pid = listener_pid(sid)
    return pid if pid and pid > 1 and is_listener(pid, sid) else None
