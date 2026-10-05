"""mojibake_repair 单元测试：字节拼写 UTF-8 的还原与合法文本的恒等（issue #162）。"""
import unittest

from mojibake_repair import repair_history, repair_reply, repair_text


class RepairTextTests(unittest.TestCase):
    def test_issue_signature_runs(self):
        cases = {
            "æ\x96\x87å\xad\x97å\x8c\x96ã\x81\x91": "文字化け",
            "â\x80\x94": "—",
            "â\x86\x92": "→",
            "å¥½æ¶\x88æ\x81¯": "好消息",
            "ã\x80\x82": "。",
            "ï¼\x81": "！",
            "ð\x9f\x99\x82": "🙂",
        }
        for corrupt, want in cases.items():
            got, fixes = repair_text(corrupt)
            self.assertEqual((got, fixes), (want, 1), corrupt)

    def test_mixed_sentence(self):
        got, fixes = repair_text("错的 â\x80\x94â\x80\x94 我犯了想当然的错误")
        self.assertEqual(got, "错的 —— 我犯了想当然的错误")
        self.assertEqual(fixes, 1)  # the two em-dashes form one maximal run

    def test_plain_text_untouched(self):
        for text in (
            "你好，世界",
            "2 × 3 = 6",
            "温度 ±3°C",
            "café naïve",
            "æøå Danish",
            "·中点·",
            "— → ✅ ✓ 🙂",
            "æ\x80\nç",          # broken runs fail strict decode and stay
        ):
            got, fixes = repair_text(text)
            self.assertEqual((got, fixes), (text, 0), text)

    def test_legit_then_corrupt(self):
        got, fixes = repair_text("×2 的结果是 â\x80\x94â\x80\x94 六")
        self.assertEqual(got, "×2 的结果是 —— 六")
        self.assertEqual(fixes, 1)

    def test_valid_run_repair_is_identity_for_text(self):
        # a repaired run re-encodes to exactly the bytes the engine meant to send
        got, fixes = repair_text("è§£å¯\x86æ\x88\x90å\x8a\x9fï¼\x81")
        self.assertEqual(got, "解密成功！")
        self.assertEqual(fixes, 1)

    def test_empty(self):
        self.assertEqual(repair_text(""), ("", 0))


class RepairReplyTests(unittest.TestCase):
    def test_streaming_chunk(self):
        chunk = {"choices": [{"index": 0, "delta": {"reasoning_content": "è§£å¯\x86æ\x88\x90å\x8a\x9f"}}]}
        fixes = repair_reply(chunk)
        self.assertEqual(fixes, 1)
        self.assertEqual(chunk["choices"][0]["delta"]["reasoning_content"], "解密成功")

    def test_final_message_with_blocks(self):
        body = {"choices": [{"message": {"content": [{"type": "text", "text": "å¥½"}]}}]}
        self.assertEqual(repair_reply(body), 1)
        self.assertEqual(body["choices"][0]["message"]["content"][0]["text"], "好")

    def test_non_string_and_missing(self):
        self.assertEqual(repair_reply({"choices": [{"delta": {"role": "assistant"}}]}), 0)
        self.assertEqual(repair_reply({}), 0)
        self.assertEqual(repair_reply(None), 0)


class RepairHistoryTests(unittest.TestCase):
    def test_assistant_and_tool_repaired_user_kept(self):
        body = {
            "messages": [
                {"role": "system", "content": "规则：â\x80\x94 保持原样"},
                {"role": "user", "content": "我贴的 â\x80\x94 别动"},
                {"role": "assistant", "content": "å¥½ç\x9a\x84"},
                {"role": "tool", "content": "output: â\x86\x92 done"},
                {"role": "assistant", "reasoning_content": "æ\x80\x9dè\x80\x83", "content": None},
            ]
        }
        fixes = repair_history(body)
        msgs = body["messages"]
        self.assertEqual(msgs[0]["content"], "规则：â\x80\x94 保持原样")   # system untouched
        self.assertEqual(msgs[1]["content"], "我贴的 â\x80\x94 别动")      # user untouched
        self.assertEqual(msgs[2]["content"], "好的")
        self.assertEqual(msgs[3]["content"], "output: → done")
        self.assertEqual(msgs[4]["reasoning_content"], "思考")
        self.assertEqual(fixes, 3)

    def test_tool_call_arguments_untouched(self):
        body = {"messages": [{"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": "{\"q\": \"å¥½\"}"}}]}]}
        self.assertEqual(repair_history(body), 0)
        self.assertEqual(body["messages"][0]["tool_calls"][0]["function"]["arguments"], '{"q": "å¥½"}')

    def test_no_messages(self):
        self.assertEqual(repair_history({"model": "x"}), 0)
        self.assertEqual(repair_history(None), 0)


if __name__ == "__main__":
    unittest.main()
