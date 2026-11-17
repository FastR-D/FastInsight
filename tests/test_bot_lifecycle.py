import json
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import lark_oapi as lark

from fastinsight.bot import FastInsightBot
from tests.http_world import HTTPWorld


def event(identifier, text="", *, image=False, timestamp=None):
    return {"header": {"event_id": identifier}, "event": {
        "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_person"}},
        "message": {"message_id": identifier, "chat_id": "oc_fixture",
            "create_time": str(int((timestamp or time.time()) * 1000)),
            "message_type": "image" if image else "text",
            "content": json.dumps({"image_key": identifier} if image else {"text": text})},
    }}


@contextmanager
def bot_world():
    with HTTPWorld() as world:
        builder = lark.Client.builder
        # The SDK itself stays real; select its public local-server domain boundary.
        with patch.object(lark.Client, "builder", side_effect=lambda: builder().domain(world.origin)):
            bot = FastInsightBot()
            existing_threads = set(threading.enumerate())
            try:
                yield bot, world
            finally:
                world.model_release.set()
                world.verification_release.set()
                world.image_release.set()
                world.publish_release.set()
                bot.handle_event(event("cleanup", "@fastinsight cancel"))
                for thread in set(threading.enumerate()) - existing_threads:
                    thread.join(5)
                    if thread.is_alive():
                        raise AssertionError(f"Scenario-owned thread did not finish: {thread.name}")


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected lifecycle transition did not complete")


class BotLifecycleScenarios(unittest.TestCase):
    def test_cancel_during_identification_verification_or_trend_prevents_publication(self):
        for stage in ("image", "verification", "model"):
            with self.subTest(stage=stage):
                with bot_world() as (bot, world):
                    getattr(world, stage + "_release").clear()
                    bot.handle_event(event("start", "@fastinsight"))
                    if stage == "image":
                        bot.handle_event(event("payload", image=True))
                    else:
                        bot.handle_event(event("payload", "https://doi.org/10.1000/known firmware"))
                    self.assertTrue(getattr(world, stage + "_entered").wait(5))
                    bot.handle_event(event("cancel", "@fastinsight cancel"))
                    self.assertFalse(bot.capture.status("oc_fixture")["armed"])
                    getattr(world, stage + "_release").set()
                self.assertEqual(world.published, [])

    def test_cancel_drains_admitted_publish_and_prevents_the_next_recipient(self):
        with bot_world() as (bot, world):
            world.publish_release.clear()
            bot.handle_event(event("start", "@fastinsight"))
            bot.handle_event(event("payload", "https://doi.org/10.1000/known firmware"))
            self.assertTrue(world.publish_entered.wait(5))
            cancelled = threading.Event()
            def cancel():
                bot.handle_event(event("cancel", "@fastinsight cancel"))
                cancelled.set()
            thread = threading.Thread(target=cancel)
            thread.start()
            try:
                self.assertFalse(cancelled.wait(0.1), "Cancellation acknowledged an in-flight publication")
            finally:
                world.publish_release.set()
                thread.join(5)
            self.assertTrue(cancelled.is_set())
        self.assertEqual([item["person"] for item in world.published], ["A"])

    def test_window_completion_keeps_registered_work_cancellable(self):
        for cancel_after_window in (False, True):
            with self.subTest(cancel=cancel_after_window), bot_world() as (bot, world):
                original_time = time.time
                offset = [0]
                with patch("time.time", side_effect=lambda: original_time() + offset[0]):
                    world.model_release.clear()
                    bot.handle_event(event("start", "@fastinsight"))
                    bot.handle_event(event("payload", "https://doi.org/10.1000/known firmware"))
                    self.assertTrue(world.model_entered.wait(5))
                    offset[0] = 61
                    wait_until(lambda: bot.capture.status("oc_fixture")["record"].get("status") == "completed"
                        and len(world.notifications) >= 3)
                    if cancel_after_window:
                        bot.handle_event(event("cancel", "@fastinsight cancel"))
                    world.model_release.set()
                    if not cancel_after_window:
                        wait_until(lambda: len(world.published) == 2)
            self.assertEqual(len(world.published), 0 if cancel_after_window else 2)

    def test_cancel_interleaved_with_arm_leaves_no_orphan_capture(self):
        with bot_world() as (bot, world):
            entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
            observed = {"active": True, "thread": None, "blocked": False}
            def filesystem_boundary(name, arguments):
                if (observed["active"] and name == "os.mkdir"
                        and threading.get_ident() == observed["thread"]
                        and str(arguments[0]) == str(world.root) and not observed["blocked"]):
                    observed["blocked"] = True
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("Filesystem boundary was not released")
            # Python's OS audit boundary controls the actual mkdir, without replacing CaptureStore.
            sys.addaudithook(filesystem_boundary)
            def start():
                observed["thread"] = threading.get_ident()
                bot.handle_event(event("start", "@fastinsight"))
            starter = threading.Thread(target=start)
            starter.start()
            self.assertTrue(entered.wait(5))
            def cancel():
                bot.handle_event(event("cancel", "@fastinsight cancel"))
                cancelled.set()
            canceller = threading.Thread(target=cancel)
            canceller.start()
            try:
                cancelled.wait(1)
            finally:
                release.set()
                observed["active"] = False
                starter.join(5)
                canceller.join(5)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(bot.capture.status("oc_fixture")["armed"])

    def test_old_worker_cannot_complete_or_publish_into_replacement_batch(self):
        with bot_world() as (bot, world):
            world.model_release.clear()
            bot.handle_event(event("old", "@fastinsight"))
            bot.handle_event(event("payload", "https://doi.org/10.1000/known firmware"))
            self.assertTrue(world.model_entered.wait(5))
            bot.handle_event(event("new", "@fastinsight"))
            replacement = bot.capture.status("oc_fixture")["record"]["batch_id"]
            world.model_release.set()
            bot.handle_event(event("new-payload", "https://doi.org/10.1000/known firmware"))
            wait_until(lambda: len(world.published) >= 2)
            self.assertEqual(len(world.published), 2)
            state = bot.capture.status("oc_fixture")
            self.assertTrue(state["armed"])
            self.assertEqual(state["record"]["batch_id"], replacement)
        self.assertEqual(len(world.published), 2)
