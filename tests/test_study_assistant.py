import base64
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import Store, StudyServer
from documents import build_evidence, check_citations, extract_document, normalized
from engine import NoteBatch, StudyEngine
from exports import anki_tsv, markdown


def document():
    return extract_document("讲义.txt", ("可靠传输需要序号确认与重传机制。\n" * 300).encode())


def result_for(doc):
    citation = {"source_id": doc["sources"][0]["id"], "quote": doc["sources"][0]["text"][:20]}
    return {"version": 1, "filename": doc["filename"], "focus": "复习", "model": "offline-test",
            "started_at": "2026-10-15T12:00:00+08:00", "completed_at": "2026-10-15T12:00:01+08:00",
            "notes": [{"id": "N001", "title": "可靠传输", "content": "序号、确认和重传。", "citations": [citation]}],
            "questions": [{"id": "Q001", "question": "如何保证可靠传输？", "answer": "序号、确认和重传。",
                           "explanation": "三者相互配合。", "citations": [citation]}],
            "cards": [{"id": "C001", "front": "可靠传输机制？", "back": "序号、确认和重传。", "citations": [citation]}],
            "audit": {"issues": [], "summary": "离线测试结果。"}, "removed_count": 0,
            "warnings": doc["warnings"], "sources": doc["sources"],
            "usage": {"calls": 4, "prompt_tokens": 100, "completion_tokens": 50}}


def model_response(data, finish_reason="stop"):
    content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
                           message=SimpleNamespace(content=content))], usage=None)


class DocumentTests(unittest.TestCase):
    def test_text_roundtrip_and_limits(self):
        doc = document()
        self.assertGreater(len(doc["sources"]), 1)
        self.assertEqual(doc["normal_calls"], len(doc["sources"]) + 3)
        with self.assertRaises(ValueError):
            extract_document("讲义.exe", b"text")
        with self.assertRaises(ValueError):
            extract_document("讲义.txt", b"")
        with self.assertRaises(ValueError):
            extract_document("讲义.txt", ("中" * 60_001).encode())

    def test_pdf_page_numbers_and_empty_pages(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (Reliable transport requires acknowledgements.) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_blank_page(width=300, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)
        doc = extract_document("source.pdf", buffer.getvalue())
        self.assertEqual(doc["pages"], 2)
        self.assertEqual(doc["sources"][0]["page"], 1)
        self.assertIn("acknowledgements", doc["sources"][0]["text"])
        self.assertTrue(doc["warnings"])

    def test_scan_and_encrypted_pdf_are_explicit(self):
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaisesRegex(ValueError, "OCR"):
            extract_document("scan.pdf", buffer.getvalue())
        writer.encrypt("example-password")
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaisesRegex(ValueError, "加密"):
            extract_document("locked.pdf", buffer.getvalue())

    def test_citations_reject_invention_and_unknown_sources(self):
        doc = document()
        good = result_for(doc)["notes"]
        check_citations(good, doc["sources"])
        for cite in ({"source_id": "S999", "quote": "可靠传输需要序号"},
                     {"source_id": "S001", "quote": "完全不在原文里的话"},
                     {"source_id": "S001", "quote": "一 二 三 四"}):
            with self.assertRaises(ValueError):
                check_citations([{"citations": [cite]}], doc["sources"])

    def test_evidence_windows_preserve_original_text_and_cover_boundaries(self):
        text = "\n".join("".join(chr(0x4E00 + index) for index in range(start, start + 100))
                         for start in range(0, 600, 100))
        sources = [{"id": "S001", "text": text}, {"id": "S002", "text": "短文不足"},
                   {"id": "S003", "text": "五个汉字了"}]
        evidence = build_evidence(sources)
        self.assertEqual(len({item["evidence_id"] for item in evidence}), len(evidence))
        self.assertNotIn("S002", {item["source_id"] for item in evidence})
        self.assertIn("S003", {item["source_id"] for item in evidence})
        covered = set()
        for item in evidence:
            self.assertLessEqual(len(item["quote"]), 180)
            self.assertGreaterEqual(len(normalized(item["quote"])), 5)
            check_citations([{"citations": [item]}], sources)
            if item["source_id"] == "S001":
                start = text.index(item["quote"])
                covered.update(range(start, start + len(item["quote"])))
        self.assertTrue(all(index in covered for index, char in enumerate(text) if not char.isspace()))


class WorkflowTests(unittest.TestCase):
    def test_real_graph_runs_parallel_and_audits_before_delivery(self):
        doc = document()
        sources = doc["sources"]
        extraction_barrier = threading.Barrier(len(sources))
        generation_barrier = threading.Barrier(2)
        completed = set()
        lock = threading.Lock()
        events = []
        opts = {"focus": "复习", "questions": 8, "cards": 12}

        def reply(**kwargs):
            system, prompt = [m["content"] for m in kwargs["messages"]]
            if "知识提取Agent" in system:
                source = next(s for s in sources if f"来源{s['id']}（" in prompt)
                evidence_id = json.loads(prompt.splitlines()[-1])[0]["evidence_id"]
                extraction_barrier.wait(timeout=8)
                data = {"notes": [{"title": source["id"], "content": "可靠传输的机制。",
                                   "evidence_ids": [evidence_id]}]}
            elif "出题Agent" in system:
                generation_barrier.wait(timeout=8)
                data = {"questions": result_for(doc)["questions"]}
                for item in data["questions"]:
                    item.pop("id")
                    item.pop("citations")
                    item["evidence_ids"] = [json.loads(prompt.splitlines()[-1])["evidence"][0]["evidence_id"]]
                with lock:
                    completed.add("questions")
            elif "卡片Agent" in system:
                generation_barrier.wait(timeout=8)
                data = {"cards": result_for(doc)["cards"]}
                for item in data["cards"]:
                    item.pop("id")
                    item.pop("citations")
                    item["evidence_ids"] = [json.loads(prompt.splitlines()[-1])["evidence"][0]["evidence_id"]]
                with lock:
                    completed.add("cards")
            else:
                self.assertEqual(completed, {"questions", "cards"})
                data = {"issues": [{"item_id": "N001", "reason": "测试用审核剔除。"}], "summary": "审核完成。"}
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(data)))],
                                   usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))

        with patch("engine.OpenAI") as client:
            client.return_value.__enter__.return_value.chat.completions.create.side_effect = reply
            engine = StudyEngine("offline-key", doc, opts, lambda *event: events.append(event))
            result = engine.run()
        self.assertEqual(result["usage"]["calls"], len(sources) + 3)
        self.assertEqual(len(result["notes"]), len(sources) - 1)
        self.assertEqual(result["removed_count"], 1)
        self.assertTrue(any(event[0] == "review" for event in events))
        for kind in ("notes", "questions", "cards"):
            check_citations(result[kind], sources)
            self.assertTrue(all("evidence_ids" not in item for item in result[kind]))

    def test_invalid_model_quote_stops_after_bounded_retry(self):
        doc = document()
        bad = {"notes": [{"title": "无法核验", "content": "内容", "citations": [
            {"source_id": "S001", "quote": "完全不在原文的文字"}]}]}
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(bad)))], usage=None)
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = response
            engine = StudyEngine("offline-key", doc, {}, lambda *args: None)
            with self.assertRaisesRegex(ValueError, r"条目\[1\].citations\[1\].quote"):
                engine.ask("提取", "材料", NoteBatch, lambda data: check_citations(data["notes"], doc["sources"]))
            self.assertEqual(call.call_count, 2)

    def test_repair_receives_exact_field_and_previous_output(self):
        doc = document()
        events, requests = [], []
        engine = StudyEngine("offline-key", doc, {"focus": "复习"}, lambda *args: events.append(args))
        evidence = next(iter(engine.evidence.values()))
        good = {"notes": [{"title": "可靠传输", "content": "传输机制", "evidence_ids": [evidence["evidence_id"]]}]}
        bad = {"notes": [good["notes"][0] | {"title": "标题" * 60}]}
        replies = iter([model_response(bad), model_response(good)])

        def reply(**kwargs):
            requests.append(json.loads(json.dumps(kwargs["messages"])))
            return next(replies)

        with patch("engine.OpenAI") as client:
            client.return_value.__enter__.return_value.chat.completions.create.side_effect = reply
            notes = engine.extract({"source": doc["sources"][0]})["notes"]
        self.assertEqual(engine.calls, 2)
        self.assertEqual(len(requests[0]), 2)
        self.assertEqual(len(requests[1]), 4)
        self.assertEqual(json.loads(requests[1][2]["content"]), bad)
        self.assertIn("notes.0.title", requests[1][-1]["content"])
        self.assertIn("S001", events[1][1])
        self.assertEqual(notes[0]["citations"], [{"source_id": "S001", "quote": evidence["quote"]}])

    def test_cross_source_evidence_is_rejected_with_source_location(self):
        doc = document()
        events = []
        engine = StudyEngine("offline-key", doc, {"focus": "复习"}, lambda *args: events.append(args))
        wrong_id = next(key for key, value in engine.evidence.items() if value["source_id"] == "S002")
        bad = {"notes": [{"title": "传输", "content": "机制", "evidence_ids": [wrong_id]}]}
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = model_response(bad)
            with self.assertRaisesRegex(ValueError, r"S001（文本片段1）.*notes.0.evidence_ids.0"):
                engine.extract({"source": doc["sources"][0]})
            self.assertEqual(call.call_count, 2)
        self.assertEqual(events[-1][0], "validation_failed")

    def test_generators_cannot_cite_evidence_not_in_handoff(self):
        doc = document()
        engine = StudyEngine("offline-key", doc, {"focus": "复习", "questions": 8}, lambda *args: None)
        first, unused = list(engine.evidence.values())[:2]
        state = {"organized": [{"id": "N001", "title": "传输", "content": "机制", "citations": [
            {"source_id": first["source_id"], "quote": first["quote"]}]}]}
        context = json.loads(engine.generation_context(state).splitlines()[-1])
        self.assertEqual(len(context["evidence"]), 1)
        bad = {"questions": [{"question": "机制？", "answer": "确认", "explanation": "原文内容",
                              "evidence_ids": [unused["evidence_id"]]}]}
        # Repeated source text may give identical windows; choose an ID not actually handed off.
        unused_id = next(key for key in engine.evidence if key not in engine.generation_evidence(state))
        bad["questions"][0]["evidence_ids"] = [unused_id]
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = model_response(bad)
            with self.assertRaisesRegex(ValueError, "questions.0.evidence_ids.0"):
                engine.questions(state)
            self.assertEqual(call.call_count, 2)

    def test_empty_extraction_is_valid_and_does_not_retry(self):
        doc = document()
        engine = StudyEngine("offline-key", doc, {"focus": "复习"}, lambda *args: None)
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = model_response({"notes": []})
            self.assertEqual(engine.extract({"source": doc["sources"][0]}), {"notes": []})
            self.assertEqual(call.call_count, 1)

    def test_malformed_json_and_truncated_output_are_diagnosed(self):
        doc = document()
        for data, finish, expected in [("```json\n{}\n```", "stop", "JSON根对象"),
                                       ({"notes": []}, "length", "输出未完整返回"),
                                       ({"wrong": []}, "stop", "notes")]:
            with self.subTest(data=data, finish=finish), patch("engine.OpenAI") as client:
                events = []
                engine = StudyEngine("offline-key", doc, {}, lambda *args: events.append(args))
                call = client.return_value.__enter__.return_value.chat.completions.create
                call.return_value = model_response(data, finish)
                with self.assertRaisesRegex(ValueError, expected):
                    engine.ask("格式检查", "材料", NoteBatch)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(events[-1][0], "validation_failed")

    def test_exports_preserve_sources_and_escape_anki_markup(self):
        result = result_for(document())
        result["cards"][0]["front"] = "<script>test</script>\t问题"
        text = markdown(result)
        self.assertIn("出处", text)
        self.assertIn("文本片段1", text)
        tsv = anki_tsv(result)
        self.assertNotIn("<script>", tsv)
        self.assertEqual(tsv.count("\t"), 1)
        self.assertIn("&lt;script&gt;", tsv)


class FakeEngine:
    def __init__(self, key, doc, options, notify):
        self.doc, self.notify = doc, notify

    def run(self):
        self.notify("extract", "离线端到端测试", 50)
        return result_for(self.doc)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="study-assistant-test-")
        self.server = StudyServer(("127.0.0.1", 0), Path(self.directory.name), FakeEngine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def request(self, method, path, data=None, origin=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        status = response.status
        conn.close()
        return status, payload

    def test_upload_job_history_downloads_and_key_is_not_persisted(self):
        payload = {"filename": "真实讲义.txt", "content": base64.b64encode("可靠传输需要序号确认与重传机制。".encode()).decode(),
                   "api_key": "DO-NOT-PERSIST-THIS-KEY", "focus": "理解概念"}
        status, data = self.request("POST", "/api/inspect", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["normal_calls"], 4)
        status, data = self.request("POST", "/api/jobs", payload)
        self.assertEqual(status, 202)
        job_id = json.loads(data)["id"]
        for _ in range(100):
            _, data = self.request("GET", "/api/jobs/" + job_id)
            job = json.loads(data)
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["progress"], 100)
        for asset in ("notes.md", "cards.tsv", "result.json", "source"):
            status, body = self.request("GET", f"/api/jobs/{job_id}/{asset}")
            self.assertEqual(status, 200)
            self.assertTrue(body)
        _, data = self.request("GET", "/api/jobs")
        self.assertEqual(json.loads(data)[0]["filename"], payload["filename"])
        for path in Path(self.directory.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(payload["api_key"].encode(), path.read_bytes())

    def test_cross_origin_and_bad_inputs_are_rejected(self):
        self.assertEqual(self.request("GET", "/api/config", origin="https://unrelated.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/inspect", {"filename": "bad.txt", "content": "not base64!"})[0], 400)
        self.assertEqual(self.request("GET", "/api/jobs/../../app.py")[0], 404)
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_failed_job_persists_specific_diagnostics_without_raw_output_or_key(self):
        self.server.engine_factory = StudyEngine
        key, raw_marker = "SECRET-NOT-SAVED-KEY", "RAW-MODEL-OUTPUT-NOT-SAVED"
        bad = {"notes": [{"title": "测试", "content": raw_marker, "evidence_ids": ["invented"]}]}
        payload = {"filename": "讲义.txt", "content": base64.b64encode("可靠传输需要序号确认与重传机制。".encode()).decode(),
                   "api_key": key}
        with patch("engine.OpenAI") as client:
            call = client.return_value.__enter__.return_value.chat.completions.create
            call.return_value = model_response(bad)
            status, data = self.request("POST", "/api/jobs", payload)
            self.assertEqual(status, 202)
            job_id = json.loads(data)["id"]
            for _ in range(100):
                _, data = self.request("GET", "/api/jobs/" + job_id)
                job = json.loads(data)
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(call.call_count, 2)
        self.assertEqual(job["status"], "failed")
        self.assertIn("S001（文本片段1）", job["error"])
        self.assertIn("notes.0.evidence_ids.0", job["error"])
        self.assertEqual(job["events"][-1]["stage"], "validation_failed")
        for path in Path(self.directory.name).rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(key.encode(), content)
                self.assertNotIn(raw_marker.encode(), content)

    def test_interrupted_job_is_marked_failed_after_restart(self):
        self.server.store.save({"id": "a" * 32, "status": "running"})
        store = Store(Path(self.directory.name))
        self.assertEqual(store.get("a" * 32)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
