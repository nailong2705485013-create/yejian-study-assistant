"""Verify the relay limit with overlapping workflow and connection requests."""
import json
import base64
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import APIStatusError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_study_assistant as baseline
from test_study_assistant import document, result_for
from engine import NoteBatch, StudyEngine
from model_connection import RequestLimiter, list_models, probe, profile
from runtime_settings import RuntimeSettings


class ModelConcurrencyTests(unittest.TestCase):
    def check_request_peak(self, limit):
        config = profile({"base_url": "https://relay.example/v1", "model": "test", "api_key": "OFFLINE-KEY"})
        limiter = RequestLimiter(limit)
        engine = StudyEngine("OFFLINE-KEY", document(), {"_connection": config, "_request_limiter": limiter}, lambda *args: None)
        lock, start = threading.Lock(), threading.Barrier(limit + 3)
        two_entered, third_entered, release = threading.Event(), threading.Event(), threading.Event()
        active = peak = seen = 0

        def handler(request):
            nonlocal active, peak, seen
            with lock:
                active += 1
                seen += 1
                peak = max(peak, active)
                if active == limit:
                    two_entered.set()
                if seen > limit:
                    third_entered.set()
            try:
                if not release.wait(5):
                    raise AssertionError("Test did not release blocked requests")
                if request.url.path.endswith("/models"):
                    # Failed calls must release their slot as well.
                    return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "offline"}})
                payload = json.loads(request.content)
                content = json.dumps({"notes": []}) if len(payload["messages"]) > 1 else "OK"
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content},
                    "finish_reason": "stop", "index": 0}]})
            finally:
                with lock:
                    active -= 1

        def transport():
            return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

        def invoke(action):
            start.wait(5)
            return action()

        actions = [lambda: engine.ask("提取", "离线测试", NoteBatch) for _ in range(limit)] + [
                   lambda: probe(config, "text", limiter=limiter), lambda: list_models(config, limiter=limiter)]
        with patch("engine.transport", transport), patch("model_connection.transport", transport), \
             ThreadPoolExecutor(max_workers=len(actions)) as pool:
            futures = [pool.submit(invoke, action) for action in actions]
            try:
                start.wait(5)
                self.assertTrue(two_entered.wait(5), f"Expected {limit} concurrent requests")
                self.assertFalse(third_entered.wait(.15), "Extra requests must wait for a free slot")
            finally:
                release.set()
            for future in futures[:limit]:
                self.assertEqual(future.result(5), {"notes": []})
            self.assertTrue(futures[limit].result(5)["ok"])
            with self.assertRaises(APIStatusError):
                futures[-1].result(5)
        self.assertEqual(seen, len(actions))
        self.assertEqual(peak, limit)
        self.assertEqual(active, 0)

    def test_selected_limit_applies_to_workflow_probes_and_model_lists(self):
        for limit in (1, 2, 4):
            with self.subTest(limit=limit):
                self.check_request_peak(limit)

    def check_graph_peak(self, limit):
        doc = document()
        source = doc["sources"][0]
        doc["sources"] = [source | {"id": f"S{index:03}"} for index in range(1, limit + 2)]
        engine = StudyEngine("OFFLINE-KEY", doc, {"focus": "复习", "questions": 8, "cards": 12,
                            "concurrency": limit}, lambda *args: None)
        lock = threading.Lock()
        two_entered, third_entered, release = threading.Event(), threading.Event(), threading.Event()
        started = []

        def extract(state):
            item = state["source"]
            with lock:
                started.append(item["id"])
                if len(started) == limit:
                    two_entered.set()
                if len(started) > limit:
                    third_entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release blocked nodes")
            note = result_for(doc)["notes"][0] | {"title": item["id"],
                "citations": [{"source_id": item["id"], "quote": item["text"][:20]}]}
            return {"notes": [note]}

        with patch.object(engine, "extract", side_effect=extract), \
             patch.object(engine, "questions", return_value={"questions": result_for(doc)["questions"]}), \
             patch.object(engine, "cards", return_value={"cards": result_for(doc)["cards"]}), \
             patch.object(engine, "review", return_value={"audit": {"issues": [], "summary": "offline"}}), \
             patch("engine.OpenAI", side_effect=AssertionError("No model calls allowed")), \
             ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine.run)
            try:
                self.assertTrue(two_entered.wait(5))
                self.assertFalse(third_entered.wait(.15), f"Only {limit} extraction nodes may start")
            finally:
                release.set()
            result = future.result(5)
        self.assertEqual(len(started), limit + 1)
        self.assertEqual(len(result["notes"]), limit + 1)

    def test_graph_honors_selected_extraction_concurrency(self):
        for limit in (1, 2, 4):
            with self.subTest(limit=limit):
                self.check_graph_peak(limit)

    def test_shrinking_capacity_waits_for_in_flight_requests(self):
        limiter = RequestLimiter(2)
        entered = threading.Event()
        limiter.__enter__()
        limiter.__enter__()
        held = 2
        def waiting_request():
            with limiter:
                entered.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(waiting_request)
            try:
                limiter.set_limit(1)
                limiter.__exit__(); held -= 1
                self.assertFalse(entered.wait(.15))
                limiter.__exit__(); held -= 1
                self.assertTrue(entered.wait(5))
            finally:
                for _ in range(held):
                    limiter.__exit__()
            future.result(5)
        self.assertEqual(limiter.active, 0)


class ConcurrencyHttpTests(unittest.TestCase):
    setUp, tearDown, request = baseline.ServerTests.setUp, baseline.ServerTests.tearDown, baseline.ServerTests.request

    def test_settings_are_validated_saved_and_reloaded_without_credentials(self):
        for limit in (1, 3, 4, 8):
            self.assertEqual(self.request("POST", "/api/settings", {"concurrency": limit})[0], 200)
            self.assertEqual(RuntimeSettings(Path(self.directory.name)).concurrency, limit)
            self.assertEqual(self.server.model_limiter.limit, limit)
        for limit in (0, 9, True, 2.5, "4", None):
            with self.subTest(invalid=limit):
                self.assertEqual(self.request("POST", "/api/settings", {"concurrency": limit})[0], 400)
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["settings"]["concurrency"], 8)
        self.assertEqual(json.loads((Path(self.directory.name) / "settings.json").read_text()), {"concurrency": 8})
        self.assertFalse(self.server.credentials.path.exists())
        with patch.object(Path, "replace", side_effect=OSError("offline write failure")):
            self.assertEqual(self.request("POST", "/api/settings", {"concurrency": 3})[0], 400)
        self.assertEqual(self.server.settings.concurrency, 8)
        self.assertEqual(RuntimeSettings(Path(self.directory.name)).concurrency, 8)

    def test_active_job_retains_snapshot_while_new_preference_is_saved(self):
        entered, release, seen = threading.Event(), threading.Event(), []
        class CaptureEngine:
            def __init__(inner, key, doc, options, notify):
                inner.doc = doc
                seen.append(options)
            def run(inner):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release active job")
                return result_for(inner.doc)
        self.server.engine_factory = CaptureEngine
        self.request("POST", "/api/settings", {"concurrency": 4})
        payload = {"filename": "offline.txt", "content": base64.b64encode(b"Offline learning material.").decode(),
                   "api_key": "OFFLINE-KEY"}
        try:
            status, body = self.request("POST", "/api/jobs", payload)
            self.assertEqual(status, 202)
            job_id = json.loads(body)["id"]
            self.assertTrue(entered.wait(5))
            self.assertEqual(seen[0]["concurrency"], 4)
            self.assertIs(seen[0]["_request_limiter"], self.server.model_limiter)
            self.assertEqual(self.request("POST", "/api/settings", {"concurrency": 1})[0], 200)
            self.assertEqual(self.server.model_limiter.limit, 4)
            self.assertEqual(self.server.store.get(job_id)["options"]["concurrency"], 4)
            self.assertEqual(self.server.settings.concurrency, 1)
        finally:
            release.set()
        self.server.executor.submit(lambda: None).result(5)
        self.assertEqual(self.server.model_limiter.limit, 1)
        self.assertEqual(self.server.store.get(job_id)["status"], "done")

    def test_invalid_task_concurrency_is_rejected_before_job_creation(self):
        payload = {"filename": "offline.txt", "content": base64.b64encode(b"Offline learning material.").decode(),
                   "api_key": "OFFLINE-KEY", "concurrency": 20}
        self.assertEqual(self.request("POST", "/api/jobs", payload)[0], 400)
        self.assertFalse(self.server.store.list())
