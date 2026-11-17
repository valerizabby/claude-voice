"""Claude Code hook handlers. Each takes the hook input dict and returns a dict for stdout ({} = print nothing).
Stdlib only."""
from __future__ import annotations

import fcntl, json, os, re, shlex, subprocess, time, traceback
from pathlib import Path

from . import state
from .config import CONFIRM_MAX_CHARS, WAKE_FOLLOWUP_SEC
from .policy import classify
from .speech import VOICE_INSTRUCTION, confirm_phrase, extract_speak, speak, speak_part
from .wake import collect_commands, is_hallucination, parse_yes, split_wake, yes_words

VENV_PY = str(Path("~/.claude-voice-mode/venv/bin/python").expanduser())  # fixed; does not follow VOICE_MODE_HOME
LISTEN_PY = str(Path(__file__).resolve().parent.parent / "listener" / "listen.py")
SETTLE_SEC = 1.5  # let the user finish: the newest utterance must be at least this old
CAP_MIN = 100
CAP_NOTE = ("Внимание: CLAUDE_CODE_STOP_HOOK_BLOCK_CAP не задан, лимит Stop-хука — 8 блокировок подряд: "
            "после восьми голосовых команд подряд без вызова инструментов голосовой режим перестанет слушать.")
CAP_WARNING = "Внимание: без настройки лимита после восьми команд подряд я перестану слушать."
ON_RE = re.compile(r"^/(\S+:)?voice-on\b")
OFF_RE = re.compile(r"^/(\S+:)?voice-off\b")
BUDGET_SEC = 75  # PreToolUse must answer well before its 90 s timeout: a killed hook lets the tool through
ORDINALS = ("первый", "второй", "третий", "четвёртый", "пятый", "шестой", "седьмой", "восьмой")
STOP_REASON = ("Пользователь сказал «стоп». Остановись, больше ничего не делай и в <speak> коротко скажи, "
               "где остановился.")
NOT_CONFIRMED = ("Пользователь не подтвердил голосом. Не выполняй это действие; оставь черновик и скажи "
                 "об этом в <speak>.")
FAIL_REASON = "Сбой голосового подтверждения"
TOO_LONG_BASH = "Команда слишком длинная для голосового подтверждения. Разбей её на отдельные короткие команды."
SAY_COMMANDS = "В команде есть служебные символы озвучки, подтвердить голосом нельзя."
TOO_LONG = "Слишком длинно для голосового подтверждения. Не выполняй; оставь черновик и скажи об этом в <speak>."
PLAN_QUESTION = "План готов. Утверждаешь?"
NO_MIC = "Не могу запустить микрофон, голосовой режим выключен."
SAY_WAKE_YES = "Чтобы подтвердить, скажи: Клод, да."
LISTENER_CHECK_SEC = 30  # the Stop hook re-checks the listener this often while it waits
STOPPED = object()  # _ask: a spoken stop took effect before the question


def _claude_pid():
    """The Claude Code process that runs this hook: the nearest ancestor whose command line mentions claude,
    skipping this hook's own shell (its command holds the plugin path). None if there is none."""
    pid = os.getppid()
    for _ in range(32):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)], capture_output=True,
                                 timeout=5).stdout.decode(errors="replace").split(None, 1)
            ppid, cmd = int(out[0]), (out[1] if len(out) > 1 else "")
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None
        if "claude" in cmd.lower() and "hooks/run.py" not in cmd:
            return pid
        pid = ppid
    return None


def _start_listener(sid):
    cmd = os.environ.get("VOICE_MODE_LISTENER")  # tests: replaces "<venv python> listen.py"
    argv = (shlex.split(cmd) if cmd else [VENV_PY, LISTEN_PY]) + ["--session", sid]
    parent = _claude_pid()
    if parent:
        argv += ["--parent-pid", str(parent)]  # the listener exits when Claude Code is gone
    with open(state.home() / sid / "listener.log", "ab") as log:
        p = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    state.save_listener_pid(sid, p.pid)  # the listener rewrites the same pid once its model is loaded


def _keep_listener(sid, may_restart=True):
    """'ok' (ours is running), 'restarted', or 'off': it can't run, so voice mode is now off, said aloud."""
    if state.listener_running(sid):
        return "ok"
    if may_restart:
        try:
            _start_listener(sid)
            return "restarted"
        except OSError:
            pass
    state.turn_off(sid)
    speak(NO_MIC)
    return "off"


def on_voice_on(sid):
    prev = state.turn_on(sid)  # also moves the cursor past older utterances
    if prev:
        state.turn_off(prev)  # `active` already points at sid: only prev's flag and listener go
        speak("Голосовой режим перенесён в другую сессию.")
    try:
        if not state.listener_running(sid):  # repeated /voice-on: a second listener would duplicate everything
            _start_listener(sid)
    except OSError:
        state.turn_off(sid)
        speak("Не удалось запустить слушатель голоса.")
        raise
    context = VOICE_INSTRUCTION
    cap = os.environ.get("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "")
    if not (cap.strip().isdigit() and int(cap) >= CAP_MIN):
        context += "\n" + CAP_NOTE
        speak(CAP_WARNING)
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def on_voice_off(sid):
    was_on = state.is_on(sid)
    state.turn_off(sid)
    if was_on:  # don't claim "off" while another session still listens
        speak("Голосовой режим выключен.")
    return {}


def _cursor_end(rows):
    idx, ts, text = rows[-1]
    # a fresh bare wake word ("Claude.") waits for its follow-up: keep it after the cursor
    return idx if split_wake(text) == "" and time.time() - ts < WAKE_FOLLOWUP_SEC else idx + 1


def on_stop(inp, wait_sec=7140, poll=0.3):
    # never exits early on stop_hook_active: chained voice commands rely on it being true
    sid = inp["session_id"]
    state.clear_stopped(sid)  # the turn has ended: a stop no longer blocks tools
    if not state.is_on(sid):
        return {}
    deadline = time.time() + wait_sec  # answer TTS counts too: the hook must end before its timeout
    listener = _keep_listener(sid)  # a crash or a resumed session can leave "on" with nobody listening
    if listener == "off":
        return {}
    next_check = time.time() + LISTENER_CHECK_SEC
    # the transcript may lag behind the Stop event (it would hold the previous answer): never mix the two
    if "last_assistant_message" in inp:
        speak(speak_part(inp["last_assistant_message"]))
    else:
        speak(extract_speak(inp["transcript_path"]))
    while time.time() < deadline:
        if not state.is_on(sid):  # /voice-off, or another session took voice mode
            return {}
        if time.time() >= next_check:
            next_check = time.time() + LISTENER_CHECK_SEC
            # dead again right after this hook restarted it: a broken install, don't loop silently
            listener = _keep_listener(sid, may_restart=listener != "restarted")
            if listener == "off":
                return {}
        rows = state.read_utterances(sid, state.get_cursor(sid))
        if rows:
            end = _cursor_end(rows)
            cmds = collect_commands([(ts, t) for _, ts, t in rows])
            stops = [i for i, (kind, _) in enumerate(cmds) if kind == "stop"]
            if stops:
                cmds = cmds[stops[-1] + 1:]  # a stop cancels everything said before it
                if not cmds:
                    state.set_cursor(sid, end)
            if cmds and time.time() - max(ts for _, ts, _ in rows) >= SETTLE_SEC:
                state.set_cursor(sid, end)
                return {"decision": "block",
                        "reason": "Голосовая команда пользователя: " + "; ".join(t for _, t in cmds)}
        time.sleep(poll)
    state.turn_off(sid)  # first: the harness may kill the hook while it speaks
    speak("Засыпаю. Включи снова командой voice-on.")
    return {}


def on_session_start(inp):
    if not state.is_on(inp["session_id"]) or _keep_listener(inp["session_id"]) == "off":
        return {}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": VOICE_INSTRUCTION}}


def on_session_end(inp):
    if state.is_on(inp["session_id"]):
        state.turn_off(inp["session_id"])  # release the mic and drop the transcript
    return {}


def on_prompt(inp):
    prompt = inp.get("prompt", "").strip()
    if ON_RE.match(prompt):
        return on_voice_on(inp["session_id"])
    if OFF_RE.match(prompt):
        return on_voice_off(inp["session_id"])
    return {}


def _decision(kind, reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                   "permissionDecisionReason": reason}}


def _answer_row(rows, after, since_ts):
    """First real answer: said after the question (index and time); not a hallucination, not a bare wake word."""
    return next((r for r in rows if r[0] >= after and r[1] >= since_ts and r[2].strip()
                 and not is_hallucination(r[2]) and split_wake(r[2]) != ""), None)


def wait_answer(sid, after, timeout, since_ts=0.0, poll=0.2):
    deadline = time.time() + timeout
    while True:
        row = _answer_row(state.read_utterances(sid, after), after, since_ts)
        if row:
            return row[2]
        left = deadline - time.time()
        if left <= 0:
            return None
        time.sleep(min(poll, left))


def _lock(sid, deadline, poll):
    f = open(state.home() / sid / "ask.lock", "a")
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() >= deadline:
                f.close()
                return None
            time.sleep(min(poll, max(0.0, deadline - time.time())))


def _said_yes(window, row):
    """«Клод, да», or a bare «Клод.» then «да» within WAKE_FOLLOWUP_SEC (the follow-up rule of commands)."""
    if parse_yes(row[2]):
        return True
    cmds = collect_commands([(ts, t) for i, ts, t in window if i <= row[0]])
    return split_wake(row[2]) is None and bool(cmds) and cmds[-1][0] == "command" and yes_words(cmds[-1][1])


def _ask(sid, phrase, t0, timeout, poll, confirm=True):
    """STOPPED, or (answer text or None, yes). `confirm`: a yes/no question, not AskUserQuestion."""
    # one question at a time per session: parallel tool calls must not share one yes
    lock = _lock(sid, t0 + BUDGET_SEC, poll)
    if lock is None:
        return None, False
    with lock:
        if _stopped(sid):  # a stop arrived while this call waited for the lock
            return STOPPED
        after = state.queue_len(sid)
        if not speak(phrase, timeout=max(0.1, t0 + BUDGET_SEC - time.time())):
            return None, False  # nobody heard the question
        since = time.time()  # the listener is deaf during TTS: anything stamped earlier predates the question
        window = min(timeout, t0 + BUDGET_SEC - since)
        if window <= 0:
            return None, False
        ans = wait_answer(sid, after, window, since_ts=since, poll=poll)
        if ans is None:
            return None, False
        heard = [r for r in state.read_utterances(sid, after) if r[1] >= since]
        row = _answer_row(heard, after, since)
        rows = state.read_utterances(sid, state.get_cursor(sid))
        # the answer (with its bare wake word) must not become a Stop command; skip it only when nothing
        # else waits after the cursor
        if rows and rows[-1] == row and all(r in heard for r in rows):
            state.set_cursor(sid, row[0] + 1)
        if any(kind == "stop" for kind, _ in collect_commands([(0.0, ans)])):
            state.set_stopped(sid)  # while still locked: a waiter that gets the lock next must see it
            return STOPPED
        yes = _said_yes(heard, row)
        if confirm and not yes and yes_words(ans):  # a plain «да»: tell how to confirm, before the next question
            speak(SAY_WAKE_YES, timeout=max(0.1, t0 + BUDGET_SEC - time.time()))
        return ans, yes


def _question_phrase(ti):
    q, labels = "", []
    try:
        first = ti["questions"][0]
        q = str(first.get("question") or "")
        labels = [str(o["label"]) for o in first["options"] if isinstance(o, dict) and o.get("label")]
    except Exception:  # odd shape: say what we have
        pass
    phrase = q or "У меня вопрос."
    if labels:
        phrase += " Варианты: " + ", ".join(
            f"{ORDINALS[i] if i < len(ORDINALS) else i + 1} — {label}" for i, label in enumerate(labels)) + "."
    return phrase


def _stopped(sid):
    """A spoken stop is in effect: the sticky flag, or a queued stop (consumed into the flag)."""
    if state.is_stopped(sid):
        return True
    rows = state.read_utterances(sid, state.get_cursor(sid))
    stops = _stop_count(rows)
    if stops:
        # consume up to the row that completes the last stop: what was said after it waits for the Stop hook
        state.set_cursor(sid, next(r[0] + 1 for j, r in enumerate(rows) if _stop_count(rows[:j + 1]) == stops))
        state.set_stopped(sid)
        return True
    return False


def _stop_count(rows):
    return sum(kind == "stop" for kind, _ in collect_commands([(ts, t) for _, ts, t in rows]))


def on_pre_tool_use(inp, confirm_sec=30, poll=0.2):
    t0 = time.time()
    sid = inp["session_id"]
    if not state.is_on(sid):
        return {}
    if _stopped(sid):
        return _decision("deny", STOP_REASON)
    name, ti = inp["tool_name"], inp.get("tool_input") or {}
    if name == "AskUserQuestion":
        phrase = _question_phrase(ti)
    elif name == "ExitPlanMode":
        phrase = PLAN_QUESTION
    elif classify(name, ti, inp.get("cwd")) == "allow":
        return _decision("allow", "Голосовой режим: безопасное действие.")
    else:
        phrase = confirm_phrase(name, ti)
        if "[[" in phrase:  # `say` would run it as a speech command (e.g. mute the dangerous part)
            return _decision("deny", SAY_COMMANDS)
        if len(phrase) > CONFIRM_MAX_CHARS:
            return _decision("deny", TOO_LONG_BASH if name == "Bash" else TOO_LONG)
    res = _ask(sid, phrase, t0, confirm_sec, poll, confirm=name != "AskUserQuestion")
    if res is STOPPED:  # the sticky flag is already set, under the ask lock
        return _decision("deny", STOP_REASON)
    ans, yes = res
    if name == "AskUserQuestion":
        return _decision("deny", f"Вопрос: {phrase} Пользователь ответил голосом: {ans}" if ans
                         else "Ответа голосом нет — попроси ответить на экране.")
    if yes:
        return _decision("allow", "План утверждён голосом." if name == "ExitPlanMode" else "Подтверждено голосом.")
    if name == "ExitPlanMode":
        return _decision("deny", "Пользователь не утвердил план — спроси голосом, что поменять.")
    return _decision("deny", NOT_CONFIRMED + (f" Пользователь сказал: {ans}" if ans else ""))


def on_notification(inp):
    if state.is_on(inp["session_id"]) and (inp.get("notification_type") == "permission_prompt"
                                           or "permission" in str(inp.get("message", "")).lower()):
        speak("Нужно разрешение. Подойди к экрану.")
    return {}


def _fail_closed(stdin):
    try:
        try:
            sid = json.loads(stdin)["session_id"]
        except Exception:  # unknown session: deny while any session listens
            return state.active_session() is not None
        return state.is_on(sid)
    except Exception:
        return True


HANDLERS = {"prompt": on_prompt, "stop": on_stop, "session_start": on_session_start,
            "session_end": on_session_end, "pre_tool_use": on_pre_tool_use, "notification": on_notification}


def main(event, stdin):
    try:
        out = HANDLERS[event](json.loads(stdin))
        return json.dumps(out, ensure_ascii=True) if out else ""  # ASCII: any stdout encoding can print it
    except Exception:  # a hook must never break the session
        try:
            state.home().mkdir(parents=True, exist_ok=True)
            with open(state.home() / "errors.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {event}\n{traceback.format_exc()}\n")
        except Exception:
            pass
        if event == "pre_tool_use" and _fail_closed(stdin):  # never let an error turn into allow
            return json.dumps(_decision("deny", FAIL_REASON), ensure_ascii=True)
        return ""
