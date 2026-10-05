import unittest

from mojibake_repair import StreamRepairer


class StreamRepairerTests(unittest.TestCase):
    def test_split_across_events(self):
        r = StreamRepairer()
        self.assertEqual(r.feed("content", "好的 â"), "好的 ")       # 'â' held: could continue
        self.assertEqual(r.feed("content", "\x80\x94 我"), "— 我")   # completes → repaired

    def test_per_char_deltas(self):
        r = StreamRepairer()
        out = [r.feed("content", piece) for piece in
               ["è", "§", "£", "å", "¯", "\x86", "æ", "\x88", "\x90", "å", "\x8a", "\x9f"]]
        self.assertEqual("".join(out), "解密成功")

    def test_complete_run_in_one_event(self):
        r = StreamRepairer()
        self.assertEqual(r.feed("content", "å¥½æ¶\x88æ\x81¯"), "好消息")

    def test_legit_latin1_mid_text_untouched(self):
        r = StreamRepairer()
        # '±' sits between ASCII, its run cannot decode, so it passes through as written
        self.assertEqual(r.feed("content", "温度 ±3°C"), "温度 ±3°C")

    def test_resolve_emits_held_tail(self):
        r = StreamRepairer()
        r.feed("content", "å")
        self.assertEqual(r.pending().get("content"), "å")
        self.assertEqual(r.resolve(), {"content": "å"})
        self.assertEqual(r.pending(), {})

    def test_unresolvable_tail_emitted_when_next_is_ascii(self):
        r = StreamRepairer()
        self.assertEqual(r.feed("content", "é"), "")                 # held
        self.assertEqual(r.feed("content", "9"), "é9")               # cannot continue → emitted

    def test_fields_are_independent(self):
        r = StreamRepairer()
        a = r.feed("reasoning_content", "â")
        b = r.feed("content", "ok")
        self.assertEqual((a, b), ("", "ok"))
        self.assertEqual(r.feed("reasoning_content", "\x80\x94"), "—")

    def test_pending_bounded(self):
        r = StreamRepairer()
        r.feed("content", "ç\x8b¯ç\x8b¯ç\x8b¯ç\x8b¯ç\x8b¯ç\x8b¯ç\x8b¯ç\x8b¯ç\x8b¯")
        self.assertLessEqual(len(r.pending().get("content", "")), 8)

    def test_empty(self):
        r = StreamRepairer()
        self.assertEqual(r.feed("content", ""), "")


if __name__ == "__main__":
    unittest.main()
