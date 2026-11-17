import unittest

from voicemode.wake import collect_commands, is_hallucination, parse_yes, split_wake


class SplitWake(unittest.TestCase):
    def test_hallucination(self):  # Whisper's stock phrases on silence or noise
        self.assertTrue(is_hallucination("Смотрите продолжение в следующей серии."))
        self.assertFalse(is_hallucination("Клод, почему биткоин падает?"))

    def test_split(self):
        self.assertEqual(split_wake("Клод, запусти тесты."), "запусти тесты")
        self.assertEqual(split_wake("Claude запусти тесты"), "запусти тесты")
        self.assertEqual(split_wake("КЛАУД — что упало?"), "что упало")
        self.assertEqual(split_wake("Клод"), "")
        self.assertIsNone(split_wake("я сказала клод уже"))


class Collect(unittest.TestCase):
    def test_collect(self):
        c = collect_commands
        self.assertEqual(c([(0, "Клод, стоп")]), [("stop", "")])
        self.assertEqual(c([(0, "Клод, хватит!")]), [("stop", "")])
        self.assertEqual(c([(0, "Клод"), (3, "запусти тесты")]), [("command", "запусти тесты")])
        self.assertEqual(c([(0, "Клод"), (12, "запусти тесты")]), [])
        self.assertEqual(c([(0, "просто разговор"), (1, "Клод, привет")]), [("command", "привет")])
        self.assertEqual(c([(0, "Продолжение следует...")]), [])
        self.assertEqual(c([(0, "Клод"), (2, "Субтитры сделал DimaTorzok")]), [])
        self.assertEqual(c([(0, "Клод"), (2, "Клод, стоп")]), [("stop", "")])
        self.assertEqual(c([(0, "Клод"), (2, "Клод, запусти тесты")]), [("command", "запусти тесты")])
        self.assertEqual(c([(0, "Клод"), (2, "стоп")]), [("stop", "")])
        self.assertEqual(c([(3, "Клод, б"), (0, "Клод, а")]), [("command", "а"), ("command", "б")])

    def test_followup_window(self):  # right after an answer the wake word is optional
        c, w = collect_commands, (100.0, 110.0)
        self.assertEqual(c([(105, "Почему биткоин падает?")], w), [("command", "почему биткоин падает")])
        self.assertEqual(c([(105, "стоп")], w), [("stop", "")])
        self.assertEqual(c([(105, "Клод, а")], w), [("command", "а")])
        self.assertEqual(c([(99, "сказано до ответа")], w), [])
        self.assertEqual(c([(111, "поздно")], w), [])
        self.assertEqual(c([(105, "Смотрите продолжение в следующей серии.")], w), [])
        self.assertEqual(c([(105, "без окна")]), [])


class Yes(unittest.TestCase):
    def test_yes(self):
        for t in ["Клод, да.", "Клод, да, да", "Claude, отправляй!", "Клод, да"]:
            self.assertTrue(parse_yes(t), t)
        for t in ["да", "Да.", "Отправляй!", "Клод.", "Клод, да нет", "да нет", "не отправляй", "да, но подожди",
                  "нет", "", "Спасибо за просмотр"]:
            self.assertFalse(parse_yes(t), t)


if __name__ == "__main__":
    unittest.main()
