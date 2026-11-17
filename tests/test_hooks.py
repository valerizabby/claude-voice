import ast, json, os, shlex, shutil, stat, subprocess, sys, tempfile, threading, time, unittest, warnings
from pathlib import Path
from unittest.mock import patch

os.environ["VOICE_MODE_HOME"] = tempfile.mkdtemp()
from voicemode import hooks, speech  # noqa: E402
from voicemode.hooks import (main, on_notification, on_pre_tool_use, on_session_start, on_stop,  # noqa: E402
                             on_voice_off, on_voice_on, wait_answer)
from voicemode.state import append_utterance, get_cursor, home, is_on, turn_off, turn_on  # noqa: E402

ROOT = Path(__file__).parent.parent
FIX = str(Path(__file__).parent / "fixtures" / "transcript.jsonl")


def ctx(out):
    return out["hookSpecificOutput"]["additionalContext"]


def inp(name, **ti):
    return {"session_id": "s", "tool_name": name, "tool_input": ti}


def dec(out):
    return out["hookSpecificOutput"]["permissionDecision"]


def why(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def main_pre(i):
    return json.loads(main("pre_tool_use", json.dumps(i)))


class HooksTest(unittest.TestCase):
    def setUp(self):
        # detached stub listeners (`true`) are never waited for, by design
        warnings.filterwarnings("ignore", r"subprocess \d+ is still running", ResourceWarning)
        os.environ["VOICE_MODE_HOME"] = tempfile.mkdtemp()
        d = Path(tempfile.mkdtemp())
        self.said = d / "said"
        say = d / "say"
        say.write_text(f'#!/bin/sh\nprintf "%s\\n" "$4" >> "{self.said}"\n')
        say.chmod(say.stat().st_mode | stat.S_IEXEC)
        for k, v in {"VOICE_MODE_SAY": str(say), "VOICE_MODE_LISTENER": "true",
                     "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "1000"}.items():
            self.addCleanup(self._restore, k, os.environ.get(k))
            os.environ[k] = v

    @staticmethod
    def _restore(k, old):
        if old is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = old

    def say_later(self, text, delay=0.1):
        """Append `text` `delay` s after the next question has been spoken (answers before that don't count)."""
        n0 = len(self.spoken())

        def go():
            end = time.time() + 5
            while len(self.spoken()) <= n0 and time.time() < end:
                time.sleep(0.01)
            time.sleep(delay)
            append_utterance("s", time.time(), text)
        self.later(go)

    def later(self, fn, delay=0.0):
        t = threading.Timer(delay, fn)
        t.start()
        self.addCleanup(t.join)  # must not write into the next test's queue

    def stub_say(self, body):
        say = Path(tempfile.mkdtemp()) / "say"
        say.write_text("#!/bin/sh\n" + body)
        say.chmod(0o755)
        os.environ["VOICE_MODE_SAY"] = str(say)

    def spoken(self):
        return self.said.read_text().splitlines() if self.said.exists() else []

    def test_off_is_silent(self):
        self.assertEqual(main("stop", json.dumps({"session_id": "x", "transcript_path": FIX})), "")
        self.assertEqual(main("session_start", json.dumps({"session_id": "x"})), "")
        self.assertEqual(main("prompt", json.dumps({"session_id": "x", "prompt": "привет"})), "")
        self.assertEqual(self.spoken(), [])

    def test_stop_block_then_stop_word(self):
        turn_on("s")
        append_utterance("s", time.time() - 3, "Клод, запусти тесты")
        out = on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=2, poll=0.05)
        self.assertEqual(out, {"decision": "block", "reason": "Голосовая команда пользователя: запусти тесты"})
        self.assertEqual(get_cursor("s"), 1)
        self.assertEqual(self.spoken(), ["Тесты зелёные."])
        # a stop cancels the queued command -> timeout -> goes to sleep
        append_utterance("s", time.time() - 3, "Клод, сделай А")
        append_utterance("s", time.time() - 2, "Клод, стоп")
        self.assertEqual(on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=0.3, poll=0.05), {})
        self.assertFalse(is_on("s"))
        self.assertFalse((home() / "s" / "queue.jsonl").exists())  # turning off drops the transcript
        self.assertTrue(self.spoken()[-1].startswith("Засыпаю"))

    def test_speaks_last_assistant_message_first(self):
        # the transcript may not be flushed yet when Stop fires (seen in the headless smoke run)
        turn_on("s")
        inp = {"session_id": "s", "transcript_path": FIX, "last_assistant_message": "Ок.\n<speak>Банан.</speak>"}
        threading.Timer(0.1, lambda: hooks.state.turn_off("s")).start()
        self.assertEqual(on_stop(inp, wait_sec=2, poll=0.05), {})
        self.assertEqual(self.spoken(), ["Банан."])

    def test_empty_last_message_does_not_fall_back(self):
        turn_on("s")
        threading.Timer(0.1, lambda: hooks.state.turn_off("s")).start()
        on_stop({"session_id": "s", "transcript_path": FIX, "last_assistant_message": ""}, wait_sec=2, poll=0.05)
        self.assertEqual(self.spoken(), [])  # FIX holds an older answer: it must not be read aloud

    def test_followup_without_wake_word(self):
        turn_on("s")
        append_utterance("s", time.time() - 1, "разговор до ответа")
        threading.Timer(0.3, lambda: append_utterance("s", time.time(), "Почему биткоин падает?")).start()
        out = on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=4, poll=0.05)
        self.assertEqual(out.get("reason"), "Голосовая команда пользователя: почему биткоин падает")

    def test_no_followup_after_window(self):
        turn_on("s")
        with patch.object(hooks, "FOLLOWUP_SEC", 0.1):
            threading.Timer(0.4, lambda: append_utterance("s", time.time(), "разговор в комнате")).start()
            self.assertEqual(on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=2.5, poll=0.05), {})

    def test_stop_then_bare_wake_then_followup(self):
        turn_on("s")
        append_utterance("s", time.time() - 3, "Клод, стоп.")
        append_utterance("s", time.time() - 0.2, "Клод.")
        threading.Timer(0.5, lambda: append_utterance("s", time.time(), "Сделай Б.")).start()
        out = on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=4, poll=0.05)
        self.assertEqual(out.get("reason"), "Голосовая команда пользователя: сделай б")
        self.assertEqual(get_cursor("s"), 3)

    def test_bare_wake_after_command_kept_for_next_stop(self):
        turn_on("s")
        append_utterance("s", time.time() - 4, "Клод, сделай А.")
        append_utterance("s", time.time() - 2, "Клод.")
        out = on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=2, poll=0.05)
        self.assertEqual(out["reason"], "Голосовая команда пользователя: сделай а")
        self.assertEqual(get_cursor("s"), 1)  # the bare wake word is re-read with its follow-up
        append_utterance("s", time.time() - 1.6, "И ещё Б.")
        out = on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=2, poll=0.05)
        self.assertEqual(out["reason"], "Голосовая команда пользователя: и еще б")

    def test_sleep_turns_off_before_speaking_and_tts_counts(self):
        log = Path(tempfile.mkdtemp()) / "say.log"
        # /bin/sh stub: cheap to start, so load can't eat the gap between the two hypotheses below
        self.stub_say(f'sleep 1.2\non=False; [ -e "$VOICE_MODE_HOME/s/on" ] && on=True\n'
                      f'printf "%s on=%s\\n" "$4" "$on" >> "{log}"\n')
        turn_on("s")
        t0 = time.time()
        self.assertEqual(on_stop({"session_id": "s", "transcript_path": FIX, "last_assistant_message": "Ответ."},
                                 wait_sec=1.2, poll=0.05), {})
        # TTS inside wait_sec: ~1.2 + 1.2 = 2.4 s; TTS outside it: ~1.2 + 1.2 + 1.2 = 3.6 s
        self.assertLess(time.time() - t0, 3.0)
        self.assertEqual(log.read_text().splitlines(),
                         ["Ответ. on=True", "Засыпаю. Включи снова командой voice-on. on=False"])

    def test_command_after_stop_survives(self):
        turn_on("s")
        append_utterance("s", time.time() - 4, "Клод, сделай А")
        append_utterance("s", time.time() - 3, "Клод, стоп")
        append_utterance("s", time.time() - 2, "Клод, сделай Б")
        out = on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=1, poll=0.05)
        self.assertEqual(out["reason"], "Голосовая команда пользователя: сделай б")
        self.assertEqual(get_cursor("s"), 3)

    def test_tool_call_after_stop_keeps_later_command(self):
        turn_on("s")
        append_utterance("s", time.time() - 5, "Клод, стоп")
        append_utterance("s", time.time() - 3, "Клод, сделай Б")
        self.assertEqual(why(on_pre_tool_use(inp("Read"))), hooks.STOP_REASON)
        self.assertEqual(get_cursor("s"), 1)  # only the stop is consumed
        out = on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=1, poll=0.05)
        self.assertEqual(out["reason"], "Голосовая команда пользователя: сделай б")

    def test_tool_call_consumes_up_to_the_last_stop(self):
        turn_on("s")
        for i, t in enumerate(("Клод, сделай А", "Клод, стоп", "Клод.", "стоп", "Клод, сделай Б")):
            append_utterance("s", time.time() - 6 + i, t)
        self.assertEqual(why(on_pre_tool_use(inp("Read"))), hooks.STOP_REASON)
        self.assertEqual(get_cursor("s"), 4)  # bare wake word + its "стоп" follow-up is the last stop

    def test_waits_until_user_finishes(self):
        turn_on("s")
        append_utterance("s", time.time(), "Клод, запусти тесты")
        t0 = time.time()
        out = on_stop({"session_id": "s", "transcript_path": FIX}, wait_sec=5, poll=0.05)
        self.assertEqual(out["decision"], "block")
        self.assertGreaterEqual(time.time() - t0, hooks.SETTLE_SEC - 0.1)

    def test_other_session_takes_over(self):
        turn_on("a")
        threading.Timer(0.2, lambda: turn_on("b")).start()
        t0 = time.time()
        self.assertEqual(on_stop({"session_id": "a", "transcript_path": FIX}, wait_sec=5, poll=0.05), {})
        self.assertLess(time.time() - t0, 2)  # not the 5 s wait
        self.assertNotIn("Засыпаю. Включи снова командой voice-on.", self.spoken())

    def test_stop_hook_active_does_not_exit(self):
        turn_on("s")
        append_utterance("s", time.time() - 3, "Клод, ещё раз")
        out = on_stop({"session_id": "s", "transcript_path": FIX, "stop_hook_active": True}, wait_sec=1, poll=0.05)
        self.assertEqual(out["decision"], "block")

    def test_session_start(self):
        self.assertEqual(on_session_start({"session_id": "b"}), {})
        turn_on("b")
        out = on_session_start({"session_id": "b"})
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("<speak>", ctx(out))

    def fake_listener(self):
        """VOICE_MODE_LISTENER that stays alive and looks like listen.py to the ownership check."""
        os.environ["VOICE_MODE_LISTENER"] = shlex.join([sys.executable, "-c", "import time; time.sleep(30)",
                                                        "listen.py"])

    @staticmethod
    def running(sid):
        """The listener pid once its exec is visible to ps (right after Popen, ps still shows the parent)."""
        for _ in range(60):
            pid = hooks.state.listener_running(sid)
            if pid:
                return pid
            time.sleep(0.05)
        return None

    def kill_listener(self, sid):
        pid = hooks.state.listener_pid(sid)
        if pid:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def test_stop_restarts_dead_listener(self):
        self.fake_listener()
        turn_on("s")
        self.addCleanup(self.kill_listener, "s")
        self.assertIsNone(hooks.state.listener_running("s"))
        append_utterance("s", time.time() - 3, "Клод, привет")
        out = on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=2, poll=0.05)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(self.running("s"))
        on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=0.1, poll=0.05)
        self.assertFalse(is_on("s"))  # timed out and slept; a live listener was not replaced meanwhile
        self.assertEqual(self.spoken(), ["Засыпаю. Включи снова командой voice-on."])

    def test_stop_rechecks_listener_while_waiting(self):
        self.fake_listener()
        on_voice_on("s")
        self.addCleanup(self.kill_listener, "s")
        first = self.running("s")
        self.assertTrue(first)
        self.later(lambda: os.kill(first, 9), 0.1)
        pids = []
        real = hooks._start_listener

        def start(sid):
            real(sid)
            pids.append(hooks.state.listener_pid(sid))
        with patch.object(hooks, "LISTENER_CHECK_SEC", 0.3), patch.object(hooks, "_start_listener", start):
            self.assertEqual(on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=1, poll=0.05), {})
        self.assertEqual(len(pids), 1)  # restarted once, then found running at the later checks
        self.assertNotEqual(pids[0], first)
        self.assertEqual(self.spoken(), ["Засыпаю. Включи снова командой voice-on."])  # not given up
        self.assertFalse(hooks.state.is_listener(pids[0], "s"))  # the sleep's turn_off killed the new one

    def test_stop_gives_up_when_restarted_listener_dies_again(self):
        turn_on("s")  # VOICE_MODE_LISTENER=true: every start exits at once
        with patch.object(hooks, "LISTENER_CHECK_SEC", 0.2):
            self.assertEqual(on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=2, poll=0.05), {})
        self.assertFalse(is_on("s"))
        self.assertEqual(self.spoken(), [hooks.NO_MIC])

    def test_no_listener_possible_turns_off_aloud(self):
        os.environ["VOICE_MODE_LISTENER"] = "/nonexistent/listener"
        turn_on("s")
        append_utterance("s", time.time() - 3, "Клод, привет")
        self.assertEqual(on_stop({"session_id": "s", "last_assistant_message": "<speak>Да.</speak>"},
                                 wait_sec=1, poll=0.05), {})
        self.assertFalse(is_on("s"))
        turn_on("s")
        self.assertEqual(on_session_start({"session_id": "s"}), {})
        self.assertFalse(is_on("s"))
        self.assertEqual(self.spoken(), ["Не могу запустить микрофон, голосовой режим выключен."] * 2)

    def test_session_start_restarts_listener(self):
        self.fake_listener()
        turn_on("s")
        self.addCleanup(self.kill_listener, "s")
        self.assertIn("<speak>", ctx(on_session_start({"session_id": "s"})))
        self.assertTrue(self.running("s"))
        self.assertEqual(self.spoken(), [])

    def test_session_end(self):
        self.fake_listener()
        on_voice_on("s")
        pid = self.running("s")
        self.assertTrue(pid)
        append_utterance("s", time.time(), "что-то в комнате")
        self.assertEqual(main("session_end", json.dumps({"session_id": "s", "reason": "prompt_input_exit"})), "")
        self.assertFalse(is_on("s"))
        self.assertFalse((home() / "s" / "queue.jsonl").exists())
        for _ in range(100):  # SIGTERM was sent (a killed child stays a zombie, which no longer looks like ours)
            if not hooks.state.is_listener(pid, "s"):
                break
            time.sleep(0.05)
        else:
            self.fail("listener still running")
        turn_on("o")
        self.assertEqual(main("session_end", json.dumps({"session_id": "x"})), "")
        self.assertTrue(is_on("o"))  # another session's end leaves the listening one alone
        self.assertEqual(self.spoken(), [])

    def test_listener_gets_claude_parent_pid(self):
        tree = {100: (90, "/bin/sh -c PYTHONUTF8=1 python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/run.py\" prompt"),
                90: (80, "/bin/zsh -c source /x/shell-snapshots/a.sh && eval '/p/.claude/plugins/v/hooks/run.py'"),
                80: (1, "/Users/u/.local/share/Claude/versions/2.1.326 --resume"),
                70: (1, "/bin/zsh -l")}

        def fake_run(argv, **kw):
            pid = int(argv[-1])
            out = (f"{tree[pid][0]:>5} {tree[pid][1]}\n" if pid in tree else "").encode()
            return subprocess.CompletedProcess(argv, 0 if out else 1, out, b"")
        with patch("voicemode.hooks.subprocess.run", fake_run):
            with patch("voicemode.hooks.os.getppid", return_value=100):
                self.assertEqual(hooks._claude_pid(), 80)
            with patch("voicemode.hooks.os.getppid", return_value=70):
                self.assertIsNone(hooks._claude_pid())
        argv_file = Path(tempfile.mkdtemp()) / "argv"
        os.environ["VOICE_MODE_LISTENER"] = shlex.join(
            [sys.executable, "-c", f"import sys; open({str(argv_file)!r}, 'w').write(repr(sys.argv[1:]))"])
        for found, tail in ((4242, ["--session", "s", "--parent-pid", "4242"]), (None, ["--session", "s"])):
            if argv_file.exists():
                argv_file.unlink()
            with patch.object(hooks, "_claude_pid", return_value=found):
                on_voice_on("s")
            for _ in range(100):
                if argv_file.exists() and argv_file.read_text():
                    break
                time.sleep(0.05)
            self.assertEqual(ast.literal_eval(argv_file.read_text()), tail)
            turn_off("s")

    def test_voice_on_off(self):
        out = on_voice_on("s")
        self.assertTrue(is_on("s"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(ctx(out), speech.VOICE_INSTRUCTION)  # cap is set in setUp: no warning
        self.assertTrue((home() / "s" / "listener.log").exists())
        self.assertEqual(self.spoken(), [])
        self.assertEqual(on_voice_off("s"), {})
        self.assertFalse(is_on("s"))
        self.assertEqual(self.spoken(), ["Голосовой режим выключен."])

    def test_voice_on_moves_from_other_session(self):
        on_voice_on("a")
        on_voice_on("b")
        self.assertFalse(is_on("a"))
        self.assertFalse((home() / "a" / "on").exists())
        self.assertTrue(is_on("b"))
        self.assertIn("Голосовой режим перенесён в другую сессию.", self.spoken())

    def test_block_cap_warning(self):
        for cap in (None, "abc", "8"):
            if cap is None:
                os.environ.pop("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", None)
            else:
                os.environ["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = cap
            self.assertIn("8", ctx(on_voice_on("s")).replace(speech.VOICE_INSTRUCTION, ""), cap)
        self.assertEqual(self.spoken().count(hooks.CAP_WARNING), 3)

    def test_prompt_dispatch(self):
        for p in ("/voice-on", "/voice-mode:voice-on", "/voice-on  "):
            out = json.loads(main("prompt", json.dumps({"session_id": "p", "prompt": p})))
            self.assertIn("<speak>", ctx(out), p)
            self.assertTrue(is_on("p"))
        self.assertEqual(main("prompt", json.dumps({"session_id": "p", "prompt": "/voice-onx"})), "")
        self.assertEqual(main("prompt", json.dumps({"session_id": "p", "prompt": "скажи /voice-off"})), "")
        self.assertTrue(is_on("p"))
        self.assertEqual(main("prompt", json.dumps({"session_id": "p", "prompt": "/voice-mode:voice-off"})), "")
        self.assertFalse(is_on("p"))

    def test_main_never_raises(self):
        self.assertEqual(main("stop", "not json"), "")
        self.assertEqual(main("stop", json.dumps({"no_session": 1})), "")
        self.assertIn("Traceback", (home() / "errors.log").read_text())

    def test_listener_launch_failure_turns_off(self):
        os.environ["VOICE_MODE_LISTENER"] = "/nonexistent/listener"
        self.assertEqual(main("prompt", json.dumps({"session_id": "f", "prompt": "/voice-on"})), "")
        self.assertFalse(is_on("f"))

    def test_run_py_subprocess(self):
        turn_on("r")
        r = subprocess.run([sys.executable, str(ROOT / "hooks" / "run.py"), "session_start"],
                           input=json.dumps({"session_id": "r"}), capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertIn("<speak>", ctx(json.loads(r.stdout)))

    def test_pre_tool_use_brief(self):
        turn_on("s")
        self.assertEqual(dec(on_pre_tool_use(inp("Read"))), "allow")
        self.say_later("Клод, да.")
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5)), "allow")
        self.assertIn("Хочу выполнить команду: git push. Можно?", self.spoken())
        self.say_later("Клод, да нет")
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5)), "deny")
        out = on_pre_tool_use(inp("mcp__claude_ai_Slack__slack_send_message"), confirm_sec=0.3)
        self.assertEqual(why(out), "Пользователь не подтвердил голосом. Не выполняй это действие; "
                                   "оставь черновик и скажи об этом в <speak>.")
        self.say_later("Продолжение следует...")
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=0.5)), "deny")
        append_utterance("s", time.time(), "Клод, стоп")
        out = on_pre_tool_use(inp("Read"))
        self.assertEqual(why(out), "Пользователь сказал «стоп». Остановись, больше ничего не делай "
                                   "и в <speak> коротко скажи, где остановился.")
        self.assertEqual(why(on_pre_tool_use(inp("Read"))), hooks.STOP_REASON)  # sticky until the Stop hook
        self.assertEqual(on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=0.1, poll=0.05), {})
        turn_on("s")  # the Stop hook timed out and slept; voice-on again
        self.assertEqual(dec(on_pre_tool_use(inp("Read"))), "allow")
        turn_off("s")
        self.assertEqual(on_pre_tool_use(inp("Bash", command="git push")), {})
        self.assertEqual(main("pre_tool_use", json.dumps(inp("Bash", command="git push"))), "")
        with patch("voicemode.hooks.classify", side_effect=RuntimeError):
            turn_on("s")
            out = main_pre(inp("Bash", command="git push"))
        self.assertEqual((dec(out), why(out)), ("deny", "Сбой голосового подтверждения"))

    def test_pre_tool_use_other_session_silent(self):
        turn_on("other")
        self.assertEqual(main("pre_tool_use", json.dumps(inp("Bash", command="git push"))), "")
        self.assertEqual(self.spoken(), [])

    def test_pre_tool_use_passes_cwd(self):
        turn_on("s")
        i = inp("Write", file_path="/proj/a.go", content="x")
        i["cwd"] = "/proj"
        self.assertEqual(dec(on_pre_tool_use(i, confirm_sec=0.2)), "allow")
        self.assertEqual(self.spoken(), [])

    def test_answer_said_before_question_ignored(self):
        turn_on("s")
        append_utterance("s", time.time(), "Клод, да")  # index < queue_len before the question
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=0.3, poll=0.05)), "deny")
        append_utterance("s", time.time() - 60, "нет")  # later index, but stamped before the question
        self.later(lambda: append_utterance("s", time.time(), "да"), 0.05)
        self.assertEqual(wait_answer("s", 1, 0.5, since_ts=time.time() - 1, poll=0.02), "да")

    def test_wait_answer(self):
        turn_on("s")
        self.assertIsNone(wait_answer("s", 0, 0.1))
        append_utterance("s", time.time(), "Субтитры сделал DimaTorzok")
        append_utterance("s", time.time(), "второй")
        self.assertEqual(wait_answer("s", 0, 0.1), "второй")
        self.assertIsNone(wait_answer("s", 2, 0.1))

    def test_answer_moves_cursor_only_when_alone(self):
        turn_on("s")
        self.say_later("Клод, да")
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)), "allow")
        self.assertEqual(get_cursor("s"), 1)  # the answer (wake word + yes) must not become a Stop command
        append_utterance("s", time.time() - 5, "Клод, сделай Б")
        self.say_later("Клод, да")
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)), "allow")
        self.assertEqual(get_cursor("s"), 1)  # the second command is still waiting for Stop

    def test_budget_bounds_the_hook(self):
        self.stub_say("sleep 5\n")
        turn_on("s")
        with patch.object(hooks, "BUDGET_SEC", 0.8):
            t0 = time.time()
            out = on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=30, poll=0.05)
        self.assertEqual(dec(out), "deny")
        self.assertLess(time.time() - t0, 3)  # not 5 s of TTS + 30 s of window

    def test_budget_shrinks_answer_window(self):
        turn_on("s")
        with patch.object(hooks, "BUDGET_SEC", 0.5):
            t0 = time.time()
            self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=30, poll=0.05)),
                             "deny")
        self.assertLess(time.time() - t0, 5)  # not the 30 s confirm window

    def test_ask_user_question(self):
        turn_on("s")
        q = {"questions": [{"question": "Какую базу взять?", "header": "БД", "multiSelect": False,
                            "options": [{"label": "Postgres", "description": "a"},
                                        {"label": "Mongo", "description": "b"}]}]}
        self.say_later("Второй.")
        out = on_pre_tool_use(inp("AskUserQuestion", **q), confirm_sec=5, poll=0.05)
        self.assertEqual((dec(out), why(out)), ("deny", "Вопрос: Какую базу взять? Варианты: первый — Postgres, "
                                                        "второй — Mongo. Пользователь ответил голосом: Второй."))
        self.assertEqual(self.spoken()[-1], "Какую базу взять? Варианты: первый — Postgres, второй — Mongo.")
        out = on_pre_tool_use(inp("AskUserQuestion", **q), confirm_sec=0.2, poll=0.05)
        self.assertEqual(why(out), "Ответа голосом нет — попроси ответить на экране.")
        for odd in ({}, {"questions": "x"}, {"questions": [{"question": "Да?", "options": 5}]},
                    {"questions": [{"options": [{"label": "А"}, "мусор"]}]}):
            self.assertEqual(dec(on_pre_tool_use(inp("AskUserQuestion", **odd), confirm_sec=0.1, poll=0.05)),
                             "deny", odd)
        self.assertEqual(self.spoken()[-2:], ["Да?", "У меня вопрос. Варианты: первый — А."])

    def test_exit_plan_mode(self):
        turn_on("s")
        self.say_later("Клод, подтверждаю.")
        self.assertEqual(dec(on_pre_tool_use(inp("ExitPlanMode", plan="x"), confirm_sec=5, poll=0.05)), "allow")
        self.assertEqual(self.spoken()[-1], "План готов. Утверждаешь?")
        self.say_later("Нет, поменяй шаг два.")
        out = on_pre_tool_use(inp("ExitPlanMode", plan="x"), confirm_sec=5, poll=0.05)
        self.assertEqual(why(out), "Пользователь не утвердил план — спроси голосом, что поменять.")

    def test_too_long_denied_without_asking(self):
        turn_on("s")
        cmd = " && ".join(["git status --short"] * 25) + " && git push --force origin main"
        out = on_pre_tool_use(inp("Bash", command=cmd), confirm_sec=5, poll=0.05)
        self.assertEqual((dec(out), why(out)), ("deny", "Команда слишком длинная для голосового подтверждения. "
                                                        "Разбей её на отдельные короткие команды."))
        out = on_pre_tool_use(inp("mcp__gmail__send_email", to=["a@b.c"], body="привет " * 60), confirm_sec=5)
        self.assertEqual((dec(out), why(out)), ("deny", "Слишком длинно для голосового подтверждения. Не выполняй; "
                                                        "оставь черновик и скажи об этом в <speak>."))
        self.assertEqual(self.spoken(), [])

    def test_all_values_spoken(self):
        turn_on("s")
        ti = {"channel_id": "C1", "message": "привет", "users": ["U1", "U2"], "blocks": [{"text": "hi"}]}
        self.say_later("Клод, да")
        self.assertEqual(dec(on_pre_tool_use(inp("mcp__claude_ai_Slack__slack_send_message", **ti),
                                             confirm_sec=5, poll=0.05)), "allow")
        self.assertEqual(self.spoken(), ["Хочу вызвать slack_send_message: channel_id: C1; message: привет; "
                                         "users: U1; users: U2; text: hi. Можно?"])

    def test_stop_as_answer(self):
        turn_on("s")
        self.say_later("Клод, стоп")
        self.assertEqual(why(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)),
                         hooks.STOP_REASON)
        self.assertEqual(why(on_pre_tool_use(inp("Read"))), hooks.STOP_REASON)  # sticky
        self.assertEqual(self.spoken()[-1], "Хочу выполнить команду: git push. Можно?")  # nothing new asked
        append_utterance("s", time.time() - 3, "Клод, дальше")
        out = on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=2, poll=0.05)
        self.assertEqual(out["reason"], "Голосовая команда пользователя: дальше")
        self.assertEqual(dec(on_pre_tool_use(inp("Read"))), "allow")  # the Stop hook cleared it
        self.say_later("Клод, хватит.")
        self.assertEqual(why(on_pre_tool_use(inp("AskUserQuestion", questions=[{"question": "Что?"}]),
                                             confirm_sec=5, poll=0.05)), hooks.STOP_REASON)

    def test_non_yes_answer_in_reason(self):
        turn_on("s")
        self.say_later("Нет, сделай через PR")
        out = on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)
        self.assertEqual(why(out), "Пользователь не подтвердил голосом. Не выполняй это действие; оставь черновик "
                                   "и скажи об этом в <speak>. Пользователь сказал: Нет, сделай через PR")

    def test_yes_during_question_not_accepted(self):
        self.stub_say("sleep 1\n")  # the listener is deaf now: a row appended meanwhile was said before
        turn_on("s")
        self.later(lambda: append_utterance("s", time.time(), "Клод, да"), 0.3)
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=0.5, poll=0.05)), "deny")

    def test_unspoken_question_denied(self):
        self.stub_say("exit 1\n")
        turn_on("s")
        self.later(lambda: append_utterance("s", time.time() + 1, "Клод, да"), 0.05)
        t0 = time.time()
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=10, poll=0.05)), "deny")
        self.assertLess(time.time() - t0, 5)  # no window was opened

    def test_bare_wake_then_answer(self):
        turn_on("s")
        self.say_later("Клод.")
        self.say_later("да", 0.4)
        self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)), "allow")
        self.assertEqual(get_cursor("s"), 2)  # wake word + yes must not become a Stop command
        self.assertEqual(on_stop({"session_id": "s", "last_assistant_message": ""}, wait_sec=0.1, poll=0.05), {})

    def test_bare_wake_too_long_before_yes(self):
        turn_on("s")
        self.say_later("Клод.")
        self.say_later("да", 0.6)
        with patch("voicemode.wake.WAKE_FOLLOWUP_SEC", 0.3):  # the same follow-up window as for commands
            self.assertEqual(dec(on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)), "deny")

    def test_plain_yes_without_wake_word_denied(self):
        turn_on("s")
        self.say_later("Да.")
        out = on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)
        self.assertEqual((dec(out), why(out)), ("deny", hooks.NOT_CONFIRMED + " Пользователь сказал: Да."))
        self.assertEqual(self.spoken(), ["Хочу выполнить команду: git push. Можно?",
                                         "Чтобы подтвердить, скажи: Клод, да."])
        self.say_later("отправляй")
        out = on_pre_tool_use(inp("ExitPlanMode", plan="x"), confirm_sec=5, poll=0.05)
        self.assertEqual(why(out), "Пользователь не утвердил план — спроси голосом, что поменять.")
        self.assertEqual(self.spoken()[-1], "Чтобы подтвердить, скажи: Клод, да.")
        self.say_later("Нет.")
        on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=5, poll=0.05)
        self.assertEqual(self.spoken()[-1], "Хочу выполнить команду: git push. Можно?")  # hint only for a plain yes
        self.say_later("Да.")
        out = on_pre_tool_use(inp("AskUserQuestion", questions=[{"question": "Ок?"}]), confirm_sec=5, poll=0.05)
        self.assertEqual(why(out), "Вопрос: Ок? Пользователь ответил голосом: Да.")  # answers are unchanged
        self.assertEqual(self.spoken()[-1], "Ок?")

    def test_concurrent_asks_one_answer(self):
        turn_on("s")
        self.say_later("Клод, да")
        res = []
        ts = [threading.Thread(target=lambda: res.append(dec(on_pre_tool_use(
            inp("Bash", command="git push"), confirm_sec=1, poll=0.05)))) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(res), ["allow", "deny"])
        self.assertEqual(len(self.spoken()), 2)  # asked one after the other, never over each other

    def test_stop_reaches_lock_waiter(self):
        turn_on("s")
        self.say_later("Клод, стоп")  # A's answer; B is waiting for the lock meanwhile
        real = hooks.collect_commands

        def slow_answer_check(utts):  # stretch the step that turns A's answer into the stop flag
            if len(utts) == 1 and utts[0][0] == 0.0:
                time.sleep(0.5)  # B polls the lock every 0.05 s: it gets in if the lock is free here
            return real(utts)
        res = []
        ts = [threading.Thread(target=lambda: res.append(why(on_pre_tool_use(
            inp("Bash", command="git push"), confirm_sec=5, poll=0.05)))) for _ in range(2)]
        with patch.object(hooks, "collect_commands", slow_answer_check):
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(res, [hooks.STOP_REASON] * 2)
        self.assertEqual(len(self.spoken()), 1)  # B was never asked

    def test_queued_stop_reaches_lock_waiter(self):
        turn_on("s")
        import fcntl
        res = []
        with open(home() / "s" / "ask.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            t = threading.Thread(target=lambda: res.append(why(on_pre_tool_use(
                inp("Bash", command="git push"), confirm_sec=5, poll=0.05))))
            t.start()
            time.sleep(0.3)  # B has passed the entry check and waits for the lock
            append_utterance("s", time.time(), "Клод, стоп")
        t.join()
        self.assertEqual(res, [hooks.STOP_REASON])
        self.assertEqual(self.spoken(), [])

    def test_say_commands_denied(self):
        turn_on("s")
        reason = "В команде есть служебные символы озвучки, подтвердить голосом нельзя."
        out = on_pre_tool_use(inp("Bash", command="echo ok [[volm 0]] && git push --force origin main [[volm 1]]"))
        self.assertEqual((dec(out), why(out)), ("deny", reason))
        out = on_pre_tool_use(inp("mcp__s__slack_send_message", channel="C1", text="привет [[slnc 4000]]"))
        self.assertEqual((dec(out), why(out)), ("deny", reason))
        self.assertEqual(self.spoken(), [])

    def test_lock_wait_counts_against_budget(self):
        turn_on("s")
        import fcntl
        with open(home() / "s" / "ask.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with patch.object(hooks, "BUDGET_SEC", 0.5):
                out = on_pre_tool_use(inp("Bash", command="git push"), confirm_sec=30, poll=0.05)
        self.assertEqual(dec(out), "deny")
        self.assertEqual(self.spoken(), [])

    def test_run_py_import_failure_fails_closed(self):
        d = Path(tempfile.mkdtemp())
        (d / "hooks").mkdir()
        shutil.copy(ROOT / "hooks" / "run.py", d / "hooks" / "run.py")
        (d / "voicemode").mkdir()
        (d / "voicemode" / "__init__.py").write_text("raise ImportError('broken')\n")

        def run(event):
            r = subprocess.run([sys.executable, str(d / "hooks" / "run.py"), event], timeout=10, capture_output=True,
                               input=json.dumps(inp("Bash", command="git push")), text=True)
            self.assertEqual(r.returncode, 0)
            return r.stdout
        self.assertEqual(run("pre_tool_use"), "")  # no session listens
        turn_on("s")
        self.assertEqual(why(json.loads(run("pre_tool_use"))), "Сбой голосового подтверждения")
        self.assertEqual(run("stop"), "")

    def test_output_is_ascii(self):
        turn_on("s")
        self.stub_say("exit 1\n")  # nobody heard the question: an instant Cyrillic deny
        out = main("pre_tool_use", json.dumps(inp("Bash", command="git push")))
        self.assertTrue(out.isascii(), out)
        self.assertEqual(why(json.loads(out)), hooks.NOT_CONFIRMED)
        with patch("voicemode.hooks.classify", side_effect=RuntimeError):
            self.assertTrue(main("pre_tool_use", json.dumps(inp("Bash", command="git push"))).isascii())
        self.assertTrue(main("session_start", json.dumps({"session_id": "s"})).isascii())

    def test_pre_tool_use_command_fails_closed(self):
        """The hooks.json command itself: python failing outright blocks (exit 2) only while voice mode is on."""
        cmd = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        bins = {}
        for name, body in (("broken", "#!/bin/sh\nexit 1\n"), ("ok", f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")):
            d = Path(tempfile.mkdtemp())
            (d / "python3").write_text(body)
            (d / "python3").chmod(0o755)
            bins[name] = d

        def run(py, **tool):
            env = {"PATH": f"{bins[py]}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"),
                   "VOICE_MODE_HOME": os.environ["VOICE_MODE_HOME"], "VOICE_MODE_SAY": "/usr/bin/false",
                   "CLAUDE_PLUGIN_ROOT": str(ROOT)}
            return subprocess.run(["/bin/sh", "-c", cmd], input=json.dumps(inp(**tool)), env=env,
                                  capture_output=True, text=True, timeout=20)
        r = run("broken", name="Read")
        self.assertEqual((r.returncode, r.stdout), (0, ""))  # off: never block anything
        turn_on("s")
        r = run("broken", name="Read")
        self.assertEqual((r.returncode, r.stdout), (2, ""))  # on: exit 2 blocks the tool
        r = run("ok", name="Bash", command="git push")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(why(json.loads(r.stdout)), hooks.NOT_CONFIRMED)

    def test_notification(self):
        perm = {"session_id": "s", "notification_type": "permission_prompt", "message": "x"}
        self.assertEqual(on_notification(perm), {})
        turn_on("s")
        self.assertEqual(on_notification({"session_id": "s", "message": "Claude is waiting for your input"}), {})
        self.assertEqual(self.spoken(), [])
        self.assertEqual(on_notification(perm), {})
        self.assertEqual(main("notification", json.dumps(
            {"session_id": "s", "message": "Claude needs your permission to use Bash"})), "")
        self.assertEqual(self.spoken(), ["Нужно разрешение. Подойди к экрану."] * 2)

    def test_plugin_files(self):
        cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(cfg["Stop"][0]["hooks"][0]["timeout"], 7200)
        self.assertLess(hooks.on_stop.__defaults__[0], 7200)
        self.assertEqual(cfg["SessionStart"][0]["matcher"], "compact|resume")
        pre = cfg["PreToolUse"][0]
        self.assertEqual((pre["matcher"], pre["hooks"][0]["timeout"]), ("*", 90))
        self.assertIn("run.py\" pre_tool_use", pre["hooks"][0]["command"])
        self.assertLess(hooks.BUDGET_SEC + 10, 90)  # the hook's own timeout must never fire
        self.assertIn("run.py\" notification", cfg["Notification"][0]["hooks"][0]["command"])
        self.assertIn("run.py\" session_end", cfg["SessionEnd"][0]["hooks"][0]["command"])
        for event in cfg.values():  # a non-UTF-8 locale must not break the interpreter or the output
            self.assertTrue(event[0]["hooks"][0]["command"].startswith("PYTHONUTF8=1 python3 "))
        self.assertEqual(json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["name"], "voice-mode")
        self.assertIn(speech.VOICE_INSTRUCTION, (ROOT / "commands" / "voice-on.md").read_text())


if __name__ == "__main__":
    unittest.main()
