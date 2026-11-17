import os, subprocess, sys, tempfile, time, unittest

os.environ["VOICE_MODE_HOME"] = tempfile.mkdtemp()
from voicemode.state import *  # noqa: E402,F403
from voicemode import state  # noqa: E402


class StateTest(unittest.TestCase):
    def setUp(self):
        os.environ["VOICE_MODE_HOME"] = tempfile.mkdtemp()

    def test_on_off_active(self):
        self.assertIsNone(turn_on("a")); self.assertTrue(is_on("a"))
        self.assertEqual(turn_on("b"), "a"); self.assertFalse(is_on("a")); self.assertTrue(is_on("b"))
        turn_off("b"); self.assertFalse(is_on("b")); self.assertIsNone(active_session())

    def test_stale_active_is_not_a_previous_session(self):
        turn_on("a"); (home() / "a" / "on").unlink()  # `active` left behind, "a" is no longer on
        self.assertIsNone(turn_on("b"))

    def test_turn_off_other_keeps_active(self):
        turn_on("a"); turn_on("b"); turn_off("a")
        self.assertEqual(active_session(), "b")

    def test_queue(self):
        append_utterance("a", 1.0, "Клод, привет"); append_utterance("a", 2.0, "да")
        self.assertEqual(read_utterances("a", 1), [(1, 2.0, "да")])
        self.assertEqual(get_cursor("a"), 0)
        set_cursor("a", 2); self.assertEqual(get_cursor("a"), 2); self.assertEqual(queue_len("a"), 2)

    def test_partial_last_line_skipped(self):
        append_utterance("a", 1.0, "x")
        with open(state.home() / "a" / "queue.jsonl", "a") as f:
            f.write('{"ts": 2.0, "te')
        self.assertEqual(read_utterances("a"), [(0, 1.0, "x")])
        self.assertEqual(read_utterances("nope"), [])

    def test_stopped_flag(self):
        self.assertFalse(is_stopped("a"))
        set_stopped("a")
        self.assertTrue(is_stopped("a"))
        clear_stopped("a")
        self.assertFalse(is_stopped("a"))
        set_stopped("a")
        turn_on("a")
        self.assertFalse(is_stopped("a"))

    def test_turn_on_skips_old_queue(self):
        append_utterance("a", 1.0, "old"); turn_on("a")
        self.assertEqual(get_cursor("a"), 1)

    def test_turn_off_drops_transcript(self):
        turn_on("a"); append_utterance("a", 1.0, "разговор в комнате"); set_cursor("a", 1)
        turn_off("a")
        self.assertEqual((queue_len("a"), get_cursor("a")), (0, 0))
        self.assertFalse((state.home() / "a" / "queue.jsonl").exists())
        self.assertFalse((state.home() / "a" / "cursor").exists())

    def test_stale_speaking(self):
        set_speaking(True)
        old = time.time() - 200
        os.utime(state.home() / "speaking", (old, old))
        self.assertFalse(speaking_recently())

    def test_turn_off_ignores_pid_1(self):
        turn_on("a"); save_listener_pid("a", 1); turn_off("a")
        self.assertIsNone(listener_pid("a"))

    def test_speaking(self):
        self.assertFalse(speaking_recently())
        set_speaking(True); self.assertTrue(speaking_recently()); set_speaking(False)
        time.sleep(0.6); self.assertFalse(speaking_recently())

    def test_turn_off_kills_listener(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "listen.py", "--session", "a"])
        self.addCleanup(p.kill)
        turn_on("a"); save_listener_pid("a", p.pid)
        for _ in range(60):  # right after Popen, ps may still show the parent's command (before exec)
            if listener_running("a"):
                break
            time.sleep(0.05)
        self.assertEqual(listener_running("a"), p.pid)
        turn_off("a")
        self.assertEqual(p.wait(timeout=3), -15)
        self.assertIsNone(listener_pid("a"))

    def test_turn_off_spares_a_reused_pid(self):
        # a dead listener's pid now belongs to something else, or to another session's listener
        for argv in (["sleep", "30"], [sys.executable, "-c", "import time; time.sleep(30)", "listen.py",
                                       "--session", "ab"]):
            p = subprocess.Popen(argv)
            self.addCleanup(p.kill)
            turn_on("a"); save_listener_pid("a", p.pid)
            self.assertIsNone(listener_running("a"))
            turn_off("a")
            self.assertIsNone(p.poll(), argv)
            self.assertIsNone(listener_pid("a"))

    def test_turn_off_stale_pid(self):
        turn_on("a"); save_listener_pid("a", 99999999)
        turn_off("a"); self.assertIsNone(listener_pid("a"))


if __name__ == "__main__":
    unittest.main()
