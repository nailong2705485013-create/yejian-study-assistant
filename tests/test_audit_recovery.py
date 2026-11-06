"""Truncation, provider defaults, safe diagnosis and bounded audit recovery."""
import json
import base64
import re
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from documents import budget_document
from engine import Audit, NoteBatch, StudyEngine, response_stats
from model_connection import ModelConfig, structured_parameters
import test_study_assistant as baseline
from test_study_assistant import document, model_response, result_for


class AuditRecoveryTests(unittest.TestCase):
    def setUp(self):
        # OpenAI itself is mocked in these cases; avoid constructing unused real
        # HTTP transports for every simulated split of the 113-item audit.
        transport_patch = patch("engine.transport", return_value=None)
        transport_patch.start()
        self.addCleanup(transport_patch.stop)

    def make_engine(self, *, official=False, budget=120):
        doc = document()
        doc["max_calls"] = budget
        self.events = []
        config = ModelConfig("https://api.deepseek.com" if official else "https://relay.example/v1",
                             "deepseek-flash", "NOT-A-REAL-KEY")
        return StudyEngine(config.api_key, doc, {"_connection": config}, lambda *e: self.events.append(e))

    def test_extensions_only_sent_to_supported_official_roots_and_models(self):
        for root in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            for model in ("deepseek-flash", "deepseek-v4-pro"):
                config = ModelConfig(root, model, "key")
                self.assertEqual(structured_parameters(config), {"reasoning_effort":"low"})
                self.assertEqual(structured_parameters(config, probe=True), {"reasoning_effort":"none"})
        for root, model in (("https://relay.example/v1", "deepseek-flash"),
                            ("https://api.deepseek.com/custom", "deepseek-flash"),
                            ("https://api.deepseek.com", "deepseek-chat"),
                            ("https://api.deepseek.com", "future-model")):
            self.assertEqual(structured_parameters(ModelConfig(root, model, "key")), {})

    def test_official_truncation_restarts_cleanly_with_more_room(self):
        engine = self.make_engine(official=True)
        truncated = model_response("SECRET-PARTIAL-JSON", "length")
        truncated.choices[0].message.reasoning_content = "PRIVATE-REASONING"
        truncated.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=6500,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=6490))
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.side_effect = [truncated, model_response({"notes":[]})]
            self.assertEqual(engine.ask("提取", "材料", NoteBatch), {"notes":[]})
        first, retry = [c.kwargs for c in call.call_args_list]
        self.assertEqual((first["max_tokens"], retry["max_tokens"]), (6500,13000))
        self.assertEqual(first["reasoning_effort"], "low")
        self.assertFalse(any(m["role"] == "assistant" for m in retry["messages"]))
        self.assertNotIn("SECRET-PARTIAL", json.dumps(retry))
        saved = json.dumps(engine.responses) + json.dumps(self.events)
        self.assertNotIn("PRIVATE-REASONING", saved)
        self.assertNotIn("SECRET-PARTIAL", saved)
        self.assertEqual(engine.responses[0]["reasoning_tokens"], 6490)
        self.assertIn("finish_reason=length", self.events[0][1])

    def test_relay_does_not_receive_provider_extensions_or_unverified_larger_cap(self):
        engine = self.make_engine()
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.side_effect = [model_response("partial", "length"), model_response({"notes":[]})]
            engine.ask("提取", "材料", NoteBatch)
        for request in call.call_args_list:
            self.assertNotIn("reasoning_effort", request.kwargs)
            self.assertEqual(request.kwargs["max_tokens"], 6500)

    def test_non_length_finish_reasons_are_distinct_and_do_not_retry(self):
        for reason, explanation in (("content_filter", "拦截"), ("tool_calls", "工具调用"),
                                    ("aborted", "中断"), ("insufficient_system_resource", "资源不足"),
                                    ("PRIVATE-UNKNOWN-FINISH", "未知"), (None, "未知")):
            with self.subTest(reason=reason), patch("engine.OpenAI") as client:
                engine = self.make_engine()
                call = client.return_value.__enter__.return_value.chat.completions.create
                call.return_value = model_response({"notes":[]}, reason)
                with self.assertRaisesRegex(ValueError, explanation) as caught:
                    engine.ask("提取", "材料", NoteBatch)
                self.assertEqual(call.call_count, 1)
                self.assertNotIn("长度上限", str(caught.exception))
                self.assertNotIn("PRIVATE-UNKNOWN", str(caught.exception))

    def test_missing_choices_is_reported_without_format_retry(self):
        engine = self.make_engine()
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = SimpleNamespace(choices=[], usage=None)
            with self.assertRaisesRegex(ValueError,"未返回候选答案"):
                engine.ask("提取", "材料", NoteBatch)
        self.assertEqual(call.call_count, 1)

    def test_113_items_split_on_truncation_all_reviewed_and_errors_preserved(self):
        engine = self.make_engine(official=True)
        note = result_for(engine.document)["notes"][0]
        state = {"organized":[note | {"id":f"N{i:03}"} for i in range(1,94)],
                 "questions":[note | {"id":f"Q{i:03}"} for i in range(1,9)],
                 "cards":[note | {"id":f"C{i:03}"} for i in range(1,13)]}
        accepted, oversized = [], []

        def reply(**kwargs):
            self.assertEqual(kwargs["reasoning_effort"], "low")
            self.assertEqual(kwargs["max_tokens"], 13000)
            batch = json.loads(kwargs["messages"][1]["content"].splitlines()[-1])
            ids = [item["id"] for item in batch]
            self.assertLessEqual(len(batch), 5)
            if len(batch) > 2:
                oversized.append(ids)
                # Even a syntactically valid JSON must not be accepted after length.
                return model_response({"issues":[],"summary":"TRUNCATED-MUST-NOT-PASS"}, "length")
            accepted.extend(ids)
            return model_response({"issues":[{"item_id":key,"reason":"缺少证据"} for key in ids if key=="Q007"],
                                   "summary":"已核验。"})

        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.side_effect = reply
            audit = engine.review(state)["audit"]
        expected = [i["id"] for items in state.values() for i in items]
        self.assertCountEqual(accepted, expected)
        self.assertEqual(len(accepted),len(set(accepted)))
        self.assertEqual(audit["issues"], [{"item_id":"Q007","reason":"缺少证据"}])
        self.assertNotIn("TRUNCATED", audit["summary"])
        self.assertTrue(oversized)
        self.assertLessEqual(call.call_count, 120)
        self.assertTrue(any(e[0]=="review_split" for e in self.events))

    def test_one_item_failure_is_bounded_and_fails_closed(self):
        engine = self.make_engine(official=True)
        state = {"organized":result_for(engine.document)["notes"], "questions":[], "cards":[]}
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = model_response({"issues":[],"summary":"partial"}, "length")
            with self.assertRaisesRegex(ValueError,"重新生成后仍达到输出长度上限"):
                engine.review(state)
        self.assertEqual(call.call_count, 2)
        self.assertEqual([r.kwargs["max_tokens"] for r in call.call_args_list], [13000,26000])
        self.assertFalse(any(e[0]=="review_split" for e in self.events))

    def test_split_requests_still_stop_at_global_budget(self):
        engine = self.make_engine(budget=3)
        note = result_for(engine.document)["notes"][0]
        state = {"organized":[note | {"id":f"N{i:03}"} for i in range(5)], "questions":[], "cards":[]}
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = model_response("partial", "length")
            with self.assertRaisesRegex(ValueError,"请求上限"):
                engine.review(state)
        self.assertEqual(call.call_count,3)
        self.assertEqual(engine.calls,3)

    def test_review_cannot_remove_an_item_from_another_batch(self):
        engine = self.make_engine()
        note = result_for(engine.document)["notes"][0]
        state = {"organized":[note | {"id":f"N{i:03}"} for i in range(1,7)], "questions":[], "cards":[]}
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = model_response({"issues":[{"item_id":"N006","reason":"跨批次"}],"summary":"完成"})
            with self.assertRaisesRegex(ValueError,"不属于本批"):
                engine.review(state)
        self.assertEqual(call.call_count,2)

    def test_duplicate_issues_are_not_counted_as_independent_findings(self):
        engine = self.make_engine()
        state = {"organized":result_for(engine.document)["notes"], "questions":[], "cards":[]}
        issue = {"item_id":"N001","reason":"同一问题"}
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.side_effect = [model_response({"issues":[issue, issue],"summary":"重复"}),
                                model_response({"issues":[issue],"summary":"已合并"})]
            audit=engine.review(state)["audit"]
        self.assertEqual(audit["issues"],[issue])
        self.assertEqual(call.call_count,2)

    def test_budget_estimate_allows_split_tree_format_repairs_but_honors_user_cap(self):
        doc = budget_document(document(), questions=8, cards=12, budget=50)
        self.assertEqual(doc["max_calls"],50)
        self.assertGreater(doc["theoretical_max_calls"],50)


class RecoveryHttpTests(unittest.TestCase):
    setUp, tearDown, request = baseline.ServerTests.setUp, baseline.ServerTests.tearDown, baseline.ServerTests.request

    def test_upload_graph_audit_split_save_and_download_without_partial_or_secret_leaks(self):
        self.server.engine_factory = StudyEngine
        secret, partial = "HTTP-PRIVATE-KEY", "TRUNCATED-PRIVATE-ANSWER"
        payload = {"filename":"回归讲义.txt", "content":base64.b64encode(
            ("可靠传输需要序号确认与重传机制。\n" * 300).encode()).decode(), "api_key":secret}
        audited = []

        def reply(**kwargs):
            system, prompt = [m["content"] for m in kwargs["messages"]]
            if "知识提取Agent" in system:
                evidence = json.loads(prompt.splitlines()[-1])[0]
                data = {"notes":[{"title":evidence["source_id"]+str(i),"content":"使用确认与重传保证可靠传输。",
                                  "evidence_ids":[evidence["evidence_id"]]} for i in range(5)]}
            elif "出题Agent" in system or "卡片Agent" in system:
                context = json.loads(prompt.splitlines()[-1])
                count = int(re.search(r"本批生成最多(\d+)",system)[1])
                evidence_id = context["evidence"][0]["evidence_id"]
                if "出题Agent" in system:
                    data = {"questions":[{"question":"可靠传输需要什么？","answer":"确认和重传等机制。",
                        "explanation":"原文给出这些机制。","evidence_ids":[evidence_id]} for _ in range(count)]}
                else:
                    data = {"cards":[{"front":"可靠传输机制？","back":"确认和重传等。",
                                      "evidence_ids":[evidence_id]} for _ in range(count)]}
            else:
                batch = json.loads(prompt.splitlines()[-1])
                if len(batch)>3:
                    return model_response(partial,"length")
                audited.extend(item["id"] for item in batch)
                data = {"issues":[{"item_id":item["id"],"reason":"用来验证审核删除功能。"}
                                  for item in batch if item["id"]=="N001"],"summary":"本批已审核。"}
            return model_response(data)

        with patch("engine.OpenAI") as client, patch("engine.transport",return_value=None):
            client.return_value.__enter__.return_value.chat.completions.create.side_effect=reply
            status, body = self.request("POST","/api/jobs",payload)
            self.assertEqual(status,202)
            job_id=json.loads(body)["id"]
            for _ in range(200):
                job=self.server.store.get(job_id)
                if job["status"] not in {"running","queued"}:
                    break
                time.sleep(.02)
        self.assertEqual(job["status"],"done",job.get("error"))
        result=job["result"]
        self.assertEqual([len(result[k]) for k in ("notes","questions","cards")],[9,8,12])
        self.assertEqual(len(audited),30)
        self.assertEqual(len(set(audited)),30)
        self.assertTrue(any(e["stage"]=="review_split" for e in job["events"]))
        self.assertTrue(any(r["finish_reason"]=="length" for r in result["diagnostics"]["responses"]))
        for asset in ("notes.md","notes.docx","cards.tsv","result.json"):
            status, body = self.request("GET",f"/api/jobs/{job_id}/{asset}")
            self.assertEqual(status,200)
            self.assertTrue(body)
        status, body = self.request("GET",f"/api/jobs/{job_id}/notes.md")
        self.assertNotIn(b"finish_reason",body)  # Technical traces stay in JSON, not teacher exports.
        for path in Path(self.directory.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(),path.read_bytes())
                self.assertNotIn(partial.encode(),path.read_bytes())


if __name__ == "__main__":
    unittest.main()
