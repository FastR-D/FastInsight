#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastinsight.capture import CaptureStore
from fastinsight.parse import (
    batch_payloads,
    capture_payload,
    interesting_payload,
    parse_event,
    parse_feishu_message,
)
from fastinsight.pipeline import confirmation_text


class ParseTests(unittest.TestCase):
    def test_command_with_mention(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_1",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"@_user_1"}',
                "mentions": [
                    {"key": "@_user_1", "name": "FastInsight", "id": {"open_id": "ou_bot"}}
                ],
                "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
            }
        )
        self.assertEqual(parsed.command, "fastinsight")
        self.assertTrue(parsed.has_fastinsight)
        self.assertIsNone(interesting_payload(parsed))

    def test_literal_at_fastinsight(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_1b",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"＠fastinsight"}',
                "sender": {"sender_type": "user"},
            }
        )
        self.assertEqual(parsed.command, "fastinsight")

    def test_slash_command_does_not_trigger(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_1c",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"/fastinsight"}',
                "sender": {"sender_type": "user"},
            }
        )
        self.assertEqual(parsed.command, "")
        self.assertFalse(parsed.has_fastinsight)
        self.assertIsNone(interesting_payload(parsed))

    def test_other_mention_does_not_trigger(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_1d",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"@_user_1 你好"}',
                "mentions": [{"key": "@_user_1", "name": "张三"}],
                "sender": {"sender_type": "user"},
            }
        )
        self.assertEqual(parsed.command, "")

    def test_command_url(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_2",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"@fastinsight https://arxiv.org/abs/1706.03762"}',
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
            }
        )
        self.assertEqual(parsed.command, "fastinsight")
        self.assertEqual(parsed.urls[0], "https://arxiv.org/abs/1706.03762")
        self.assertNotIn("fastinsight", parsed.cleaned_text.lower())

    def test_cancel(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_3",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"@_user_1 cancel"}',
                "mentions": [{"key": "@_user_1", "name": "fastinsight"}],
                "sender": {"sender_type": "user"},
            }
        )
        self.assertEqual(parsed.command, "cancel")

    def test_post_at_tag(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_post",
                "chat_id": "oc_abc",
                "msg_type": "post",
                "content": '{"title":"","content":[[{"tag":"at","user_id":"ou_bot","user_name":"FastInsight"},{"tag":"text","text":" cancel"}]]}',
                "sender": {"sender_type": "user"},
            }
        )
        self.assertEqual(parsed.command, "cancel")

    def test_next_image(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_4",
                "chat_id": "oc_abc",
                "message_type": "image",
                "content": '{"image_key":"img_v3_xxx"}',
                "sender": {"id": "ou_user", "sender_type": "user"},
            }
        )
        payload = interesting_payload(parsed)
        assert payload is not None
        self.assertEqual(payload["kind"], "image")
        self.assertEqual(payload["image_key"], "img_v3_xxx")
        self.assertEqual(parsed.text, "")

    def test_event_wrapper(self) -> None:
        parsed = parse_event(
            {
                "header": {"event_id": "evt_1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
                    "message": {
                        "message_id": "om_5",
                        "chat_id": "oc_abc",
                        "message_type": "text",
                        "content": '{"text":"see https://doi.org/10.1000/xyz"}',
                    },
                },
            }
        )
        self.assertEqual(parsed.event_id, "evt_1")
        payload = interesting_payload(parsed)
        assert payload is not None
        self.assertEqual(payload["kind"], "url")


class CaptureTests(unittest.TestCase):
    def test_arm_consume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CaptureStore(Path(tmp) / ".pending-capture.json")
            armed = store.arm("agent:main:feishu:group:oc_abc")
            self.assertTrue(armed["armed"])
            self.assertEqual(armed["chat_id"], "oc_abc")
            self.assertTrue(store.status("oc_abc")["armed"])
            consumed = store.consume("oc_abc")
            self.assertTrue(consumed["consumed"])
            self.assertFalse(store.status("oc_abc")["armed"])

    def test_image_batch_claims_each_image_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CaptureStore(Path(tmp) / ".pending-capture.json")
            now = time.time()
            store.arm(
                "oc_abc",
                chat_id="oc_abc",
                mode="image_batch",
                batch_id="batch_1",
                command_message_id="om_cmd",
                window_start=now - 60,
                window_end=now + 60,
            )
            first = store.claim_payload(
                "oc_abc",
                batch_id="batch_1",
                payload_id="img_1",
                message_id="om_1",
                image_key="img_1",
                message_time=now - 30,
            )
            duplicate = store.claim_payload(
                "oc_abc",
                batch_id="batch_1",
                payload_id="img_1",
                message_id="om_1",
                image_key="img_1",
                message_time=now - 30,
            )
            second = store.claim_payload(
                "oc_abc",
                batch_id="batch_1",
                payload_id="img_2",
                message_id="om_1",
                image_key="img_2",
                message_time=now - 30,
            )
            outside = store.claim_payload(
                "oc_abc",
                batch_id="batch_1",
                payload_id="img_3",
                message_id="om_3",
                image_key="img_3",
                message_time=now - 61,
            )
            self.assertTrue(first["claimed"])
            self.assertFalse(duplicate["claimed"])
            self.assertEqual(duplicate["reason"], "already_processed")
            self.assertTrue(second["claimed"])
            self.assertFalse(outside["claimed"])
            self.assertEqual(outside["reason"], "outside_window")

    def test_image_batch_rejects_messages_after_window_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CaptureStore(Path(tmp) / ".pending-capture.json")
            now = time.time()
            store.arm(
                "oc_abc",
                chat_id="oc_abc",
                mode="image_batch",
                batch_id="batch_expired",
                window_start=now - 120,
                window_end=now - 60,
            )
            claim = store.claim_payload(
                "oc_abc",
                batch_id="batch_expired",
                payload_id="img_old",
                message_id="om_old",
                image_key="img_old",
                message_time=now - 90,
            )
            self.assertFalse(claim["claimed"])
            self.assertEqual(claim["reason"], "window_expired")

    def test_processed_images_stay_deduplicated_across_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CaptureStore(Path(tmp) / ".pending-capture.json")
            now = time.time()
            store.arm(
                "oc_abc",
                chat_id="oc_abc",
                mode="image_batch",
                batch_id="batch_first",
                window_start=now - 60,
                window_end=now + 60,
            )
            first_claim = store.claim_payload(
                "oc_abc",
                batch_id="batch_first",
                payload_id="img_once",
                message_id="om_once",
                image_key="img_once",
                message_time=now,
            )
            self.assertTrue(first_claim["claimed"])
            first_result = store.finish_batch("oc_abc", batch_id="batch_first")
            self.assertEqual(first_result["processed_count"], 1)
            store.arm(
                "oc_abc",
                chat_id="oc_abc",
                mode="image_batch",
                batch_id="batch_second",
                window_start=now - 60,
                window_end=now + 60,
            )
            duplicate = store.claim_payload(
                "oc_abc",
                batch_id="batch_second",
                payload_id="img_once",
                message_id="om_once",
                image_key="img_once",
                message_time=now,
            )
            self.assertFalse(duplicate["claimed"])
            self.assertEqual(duplicate["reason"], "already_processed")
            self.assertTrue(
                store.claim_payload(
                    "oc_abc",
                    batch_id="batch_second",
                    payload_id="img_new",
                    message_id="om_new",
                    image_key="img_new",
                    message_time=now,
                )["claimed"]
            )
            second_result = store.finish_batch("oc_abc", batch_id="batch_second")
            self.assertEqual(second_result["processed_count"], 1)


    def test_plain_title_only_when_armed_for_title(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_6",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"Attention Is All You Need"}',
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
            }
        )
        self.assertIsNone(interesting_payload(parsed))
        self.assertIsNone(capture_payload(parsed, accept_title=False))
        payload = capture_payload(parsed, accept_title=True)
        assert payload is not None
        self.assertEqual(payload["kind"], "url")
        self.assertEqual(payload["url"], "")
        self.assertIn("Attention", payload["text"])

    def test_command_image_is_not_the_paper(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_7",
                "chat_id": "oc_abc",
                "msg_type": "post",
                "content": '{"text":"@fastinsight","image_key":"img_v3_cmd"}',
                "sender": {"sender_type": "user"},
            }
        )
        self.assertEqual(parsed.command, "fastinsight")
        self.assertIsNone(interesting_payload(parsed))
        self.assertIsNone(capture_payload(parsed, accept_title=True))
        payloads = batch_payloads(parsed)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["image_key"], "img_v3_cmd")

    def test_batch_payloads_include_every_image(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_8",
                "chat_id": "oc_abc",
                "msg_type": "post",
                "content": '{"content":[[{"tag":"img","image_key":"img_1"},{"tag":"img","image_key":"img_2"}]]}',
                "sender": {"sender_type": "user"},
            }
        )
        payloads = batch_payloads(parsed)
        self.assertEqual([payload["image_key"] for payload in payloads], ["img_1", "img_2"])

    def test_batch_payloads_include_preferred_link(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_9",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"https://mp.weixin.qq.com/s/abc https://arxiv.org/abs/1706.03762"}',
                "sender": {"sender_type": "user"},
            }
        )
        payloads = batch_payloads(parsed)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["kind"], "url")
        self.assertEqual(payloads[0]["url"], "https://arxiv.org/abs/1706.03762")

    def test_image_with_link_is_one_item_carrying_link_metadata(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_10",
                "chat_id": "oc_abc",
                "msg_type": "post",
                "content": '{"content":[[{"tag":"img","image_key":"img_link"},{"tag":"a","href":"https://arxiv.org/abs/1706.03762"}]]}',
                "sender": {"sender_type": "user"},
            }
        )
        payloads = batch_payloads(parsed)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["kind"], "image")
        self.assertEqual(payloads[0]["urls"], ["https://arxiv.org/abs/1706.03762"])


class ConfirmationTests(unittest.TestCase):
    def test_success_lists_all_matched_directions(self) -> None:
        text = confirmation_text(
            {
                "paper": {"title": "IoT Patch Correctness"},
                "route": {
                    "matched_direction": "IoT",
                    "matched_directions": [
                        {"name": "IoT"},
                        {"name": "Patch Correctness"},
                    ],
                    "owners": [{"name": "何艺"}, {"name": "谢子洋"}],
                    "fallback": False,
                },
                "delivered": [{"ok": True, "deliveredTo": ["何艺", "谢子洋"]}],
                "errors": [],
            }
        )
        self.assertIn("IoT / Patch Correctness", text)
        self.assertIn("@何艺、@谢子洋", text)

    def test_success_mentions_owners_and_trend(self) -> None:
        text = confirmation_text(
            {
                "paper": {"title": "Attention Is All You Need"},
                "route": {
                    "matched_direction": "NLP",
                    "owners": [{"name": "张三"}, {"name": "李四"}],
                    "fallback": False,
                },
                "delivered": [{"ok": True, "deliveredTo": ["张三", "李四"]}],
                "errors": [],
                "trend_text": "Transformer 成为后续大模型的基础架构。",
            }
        )
        self.assertIn("Attention Is All You Need", text)
        self.assertIn("NLP", text)
        self.assertIn("@张三", text)
        self.assertIn("FastResearch", text)
        self.assertIn("Transformer", text)
        self.assertTrue(text.startswith("✅"))


if __name__ == "__main__":
    unittest.main()
