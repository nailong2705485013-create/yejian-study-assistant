"""Offline boundaries, protocols, export and PPTX regression checks."""
import base64
import io
import json
import re
import tempfile
import unittest
import zipfile
import time
from xml.etree import ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import httpx
from docx import Document
from openai import APIStatusError
from pypdf import PdfWriter

import test_study_assistant as baseline
from test_study_assistant import document, result_for
from documents import budget_document, extract_document, selected_pages
from engine import StudyEngine
from exports import citations_text, word_document
from model_connection import connection_error, list_models, probe, profile
from pptx_render import validate_package
from credentials import CredentialStore

FIXTURE = Path(__file__).parent / "fixtures" / "diagram.pptx"


def text_pptx(pages, *, long_first=False):
    """Small synthetic input packages; no user slides are kept in the tests."""
    from pptx import Presentation
    deck = Presentation()
    for index in range(pages):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Knowledge point " + str(index + 1)
        if index == 0 and long_first:
            slide.shapes.title.text = "A" * 3501
    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


class CredentialTests(unittest.TestCase):
    def test_encrypted_storage_reload_public_output_and_endpoint_binding(self):
        # DPAPI is replaced only here; an interactive Windows user must validate
        # the real OS encryption path. This reversible test encoding is not production encryption.
        with tempfile.TemporaryDirectory() as directory, patch("credentials.protect", lambda raw: b"TEST:" + raw[::-1]), \
             patch("credentials.unprotect", lambda blob: blob[5:][::-1]):
            store = CredentialStore(Path(directory))
            config = profile({"base_url":"https://saved.example/v1","model":"text","api_key":"SAVED-PRIVATE-KEY"})
            store.save(config, None)
            self.assertNotIn(b"SAVED-PRIVATE-KEY", store.path.read_bytes())
            loaded = CredentialStore(Path(directory))
            self.assertTrue(loaded.public()["text"]["has_saved_key"])
            self.assertNotIn("SAVED-PRIVATE-KEY", json.dumps(loaded.public()))
            self.assertEqual(loaded.resolve({"base_url":config.base_url,"model":"other"}, "text").api_key, config.api_key)
            with self.assertRaises(ValueError):
                loaded.resolve({"base_url":"https://another.example/v1"}, "text")
            with self.assertRaises(ValueError):
                loaded.resolve({"base_url":config.base_url}, "vision")
            old = store.path.read_bytes()
            with patch("credentials.protect", side_effect=ValueError("安全加密不可用")), self.assertRaises(ValueError):
                store.save(profile({"api_key":"NEW-KEY"}), None)
            self.assertEqual(store.path.read_bytes(), old)
            store.clear()
            self.assertEqual(store.public()["text"], None)


class ConnectionTests(unittest.TestCase):
    def test_model_name_is_optional_only_for_list_lookup(self):
        data = {"base_url": "https://relay.example/v1", "model": "", "api_key": "TEST-KEY"}
        self.assertEqual(profile(data, require_model=False).model, "")
        with self.assertRaisesRegex(ValueError, "模型名"):
            profile(data)

    def test_profile_validation_and_secret_isolation(self):
        official = profile({}, fallback_key="OFFICIAL-KEY")
        self.assertNotIn("OFFICIAL-KEY", repr(official))
        self.assertNotIn("OFFICIAL-KEY", json.dumps(official.public()))
        self.assertEqual(profile({"base_url": "https://api.deepseek.com/v1/"}, fallback_key="OK").api_key, "OK")
        for url in ("https://other.example/v1", "https://api.deepseek.com/custom", "https://api.deepseek.com.evil/v1"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "Key"):
                profile({"base_url": url}, fallback_key="OFFICIAL-KEY")
        for url in ("http://remote.example", "https://name:pass@service.example", "https://service.example?q=key",
                    "https://service.example/#key", "https://service.example/chat/completions", "https://service.example:bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                profile({"base_url": url, "api_key": "key"})

    def test_list_and_vision_probe_use_actual_image_and_no_redirects(self):
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"object": "list", "data": [{"id": "vision-test", "object": "model"}]})
            payload = json.loads(request.content)
            content = payload["messages"][0]["content"]
            self.assertIsInstance(content, list)
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertNotIn("1123", content[0]["text"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "1123"}, "finish_reason": "stop"}]})
        config = profile({"base_url": "https://test.example/v1", "model": "vision-test", "api_key": "local-test"})
        with patch("model_connection.transport", lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)):
            self.assertEqual(list_models(config)["models"], ["vision-test"])
            with patch("model_connection.secrets.randbelow", return_value=123):
                self.assertTrue(probe(config, "vision")["ok"])
        seen.clear()
        def redirect(request):
            seen.append(request)
            return httpx.Response(302, headers={"Location": "https://other.example/models"})
        with patch("model_connection.transport", lambda: httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=False)):
            with self.assertRaises(APIStatusError) as caught:
                list_models(config)
            self.assertIn("已阻止", connection_error(caught.exception))
        self.assertEqual(len(seen), 1)


class DocumentV2Tests(unittest.TestCase):
    def test_pptx_text_and_images_have_separate_allowances(self):
        for pages in (13, 24):
            with self.subTest(pages=pages):
                doc = extract_document("course.pptx", text_pptx(pages))
                self.assertEqual(doc["selected_pages"], list(range(1, pages + 1)))
                self.assertEqual(len(doc["sources"]), 2 * pages)
                self.assertEqual(sum(s["kind"] == "text" for s in doc["sources"]), pages)
                self.assertEqual(sum(s["kind"] == "visual" for s in doc["sources"]), pages)
                self.assertEqual(doc["normal_calls"], 3 * pages + 3)
                self.assertLessEqual(doc["max_calls"], 120)
                self.assertEqual(len({s["id"] for s in doc["sources"]}), 2 * pages)

    def test_pptx_separate_limits_still_enforce_pages_text_and_request_budget(self):
        content = text_pptx(25)
        with self.assertRaisesRegex(ValueError, "最多识别24张"):
            extract_document("course.pptx", content)
        selected = extract_document("course.pptx", content, "13-25")
        self.assertEqual(selected["vision_pages"], list(range(13, 26)))
        self.assertTrue(all(s["page"] >= 13 for s in selected["sources"]))
        with self.assertRaisesRegex(ValueError, "文字超过24段"):
            extract_document("course.pptx", text_pptx(24, long_first=True))
        with self.assertRaisesRegex(ValueError, "至少需要75次请求"):
            extract_document("course.pptx", text_pptx(24), budget=74)

    def test_ranges_preserve_original_page_numbers_and_limits(self):
        self.assertEqual(selected_pages("3,1-2,2", 4), [1, 2, 3])
        for expression in ("0", "2-1", "1-5", "a", "1,"):
            with self.assertRaises(ValueError):
                selected_pages(expression, 4)
        writer = PdfWriter()
        for _ in range(100):
            writer.add_blank_page(width=100, height=100)
        # A blank page cannot become a successful text extraction merely by selecting it.
        buffer = io.BytesIO(); writer.write(buffer)
        with self.assertRaisesRegex(ValueError, "OCR"):
            extract_document("large.pdf", buffer.getvalue(), "91-93")
        with self.assertRaisesRegex(ValueError, "页码"):
            extract_document("text.txt", b"hello there", "1")
        doc = document()
        with self.assertRaisesRegex(ValueError, "预算"):
            budget_document(doc, budget=1)

    def test_pptx_text_image_only_page_and_original_slide_numbers(self):
        data = FIXTURE.read_bytes()
        doc = extract_document("课程.pptx", data)
        self.assertEqual(doc["pages"], 2)
        self.assertEqual(doc["vision_pages"], [1, 2])
        self.assertTrue(any("Acknowledgement" in item["text"] for item in doc["sources"]))
        image_only = extract_document("课程.pptx", data, "2")
        self.assertEqual(image_only["selected_pages"], [2])
        self.assertEqual(len(image_only["sources"]), 1)
        self.assertEqual(image_only["sources"][0]["kind"], "visual")
        self.assertEqual(image_only["sources"][0]["page"], 2)

    def test_pptx_external_assets_and_embedded_objects_are_rejected(self):
        for filename, content in (("ppt/embeddings/test.bin", b"data"), ("ppt/slides/_rels/extra.rels",
              b'<Relationships><Relationship TargetMode="External" Type="image" Target="https://example.com/a.png"/></Relationships>')):
            target = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(FIXTURE.read_bytes())) as original, zipfile.ZipFile(target, "w") as changed:
                for info in original.infolist():
                    changed.writestr(info, original.read(info.filename))
                changed.writestr(filename, content)
            with self.assertRaises(ValueError):
                validate_package(target.getvalue())

    def test_grouped_shapes_are_extracted_recursively(self):
        target = io.BytesIO()
        p = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
        a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        with zipfile.ZipFile(io.BytesIO(FIXTURE.read_bytes())) as original, zipfile.ZipFile(target, "w") as changed:
            for info in original.infolist():
                data = original.read(info.filename)
                if info.filename == "ppt/slides/slide1.xml":
                    root = ET.fromstring(data); tree = root.find(f".//{p}spTree")
                    group = ET.Element(p + "grpSp")
                    props = ET.SubElement(group, p + "nvGrpSpPr")
                    ET.SubElement(props, p + "cNvPr", id="999", name="Test group")
                    ET.SubElement(props, p + "cNvGrpSpPr"); ET.SubElement(props, p + "nvPr")
                    transform = ET.SubElement(ET.SubElement(group, p + "grpSpPr"), a + "xfrm")
                    for tag, values in (("off", {"x":"0","y":"0"}), ("ext", {"cx":"12192000","cy":"6858000"}),
                                        ("chOff", {"x":"0","y":"0"}), ("chExt", {"cx":"12192000","cy":"6858000"})):
                        ET.SubElement(transform, a + tag, values)
                    for shape in list(tree):
                        if shape.tag == p + "sp":
                            tree.remove(shape); group.append(shape)
                    tree.append(group); data = ET.tostring(root)
                changed.writestr(info, data)
        doc = extract_document("group.pptx", target.getvalue())
        self.assertTrue(any("Acknowledgement" in item["text"] for item in doc["sources"]))

    def test_docx_contains_all_saved_content_and_visual_citations_are_distinct(self):
        result = result_for(document())
        result["notes"][0]["content"] += "\x01\x0b"
        exported = word_document(result)
        self.assertEqual(exported[:2], b"PK")
        text = "\n".join(p.text for p in Document(io.BytesIO(exported)).paragraphs)
        for marker in ("课程学习资料", "知识笔记", "答案：", "解析：", "复习卡片", "出处：", "离线测试结果"):
            self.assertIn(marker, text)
        result["sources"][0]["kind"] = "visual"
        self.assertIn("模型识别", citations_text(result["notes"][0], {s["id"]: s for s in result["sources"]}))


class WorkflowV2Tests(unittest.TestCase):
    def test_long_context_generation_and_review_are_bounded_batches(self):
        doc = document()
        engine = StudyEngine("fake", doc, {"focus": "复习", "questions": 8, "cards": 12}, lambda *args: None)
        note = result_for(doc)["notes"][0]
        evidence = next(iter(engine.evidence.values()))
        note["citations"] = [{key: evidence[key] for key in ("source_id", "quote")}]
        notes = [note | {"id": f"N{i:03}", "title": str(i), "content": "详细概念。" * 150} for i in range(1, 25)]
        captured = []
        def ask(role, prompt, schema, validate=None, **kwargs):
            captured.append((role, len(prompt)))
            self.assertLess(len(prompt), 25_000)
            if "审核Agent" in role:
                data = {"issues": [], "summary": "本批通过。"}
            else:
                context = json.loads(prompt.splitlines()[-1])
                self.assertLessEqual(len(context["notes"]), 8)
                count = int(re.search(r"最多(\d+)", role)[1])
                key = context["evidence"][0]["evidence_id"]
                data = {"questions": [{"question": "问题", "answer": "答案", "explanation": "解释", "evidence_ids": [key]} for _ in range(count)]}
            return validate(data) or data if validate else data
        with patch.object(engine, "ask", side_effect=ask):
            generated = engine.questions({"organized": notes})["questions"]
            self.assertEqual(len(generated), 8)
            audit = engine.review({"organized": notes, "questions": generated, "cards": []})
            self.assertEqual(len(audit["audit"]["summary"].splitlines()), 7)
        self.assertEqual(sum("出题Agent" in role for role, _ in captured), 3)

    def test_image_only_source_is_recognized_before_evidence_is_built(self):
        doc = extract_document("test.pptx", FIXTURE.read_bytes(), "2")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); (folder / "slides").mkdir()
            (folder / "slides/slide-0002.png").write_bytes(b"test image")
            config = profile({"base_url": "https://vision.example", "model": "vision", "api_key": "VISION-KEY"})
            engine = StudyEngine("TEXT-KEY", doc, {"_vision_connection": config, "_visual_consent": True,
                "_job_dir": str(folder)}, lambda *args: None)
            self.assertFalse(engine.evidence)
            with patch("pptx_render.render_slides") as renderer, patch.object(engine, "ask", return_value={
                "observations": [{"description": "图表中B的数值为4，A的数值为2。"}], "limitations": ""}) as ask:
                engine.prepare_visuals({})
            renderer.assert_called_once()
            self.assertIs(ask.call_args.kwargs["connection"], config)
            self.assertTrue(all(e["kind"] == "visual" for e in engine.evidence.values()))

    def test_render_failure_is_never_silent_text_only_success(self):
        doc = extract_document("test.pptx", FIXTURE.read_bytes())
        config = profile({"api_key": "fake"})
        engine = StudyEngine("fake", doc, {"_vision_connection": config, "_visual_consent": True,
            "_job_dir": "."}, lambda *args: None)
        with patch("pptx_render.render_slides", side_effect=ValueError("渲染失败")), self.assertRaisesRegex(ValueError, "渲染失败"):
            engine.prepare_visuals({})


class HttpV2Tests(unittest.TestCase):
    setUp, tearDown, request = baseline.ServerTests.setUp, baseline.ServerTests.tearDown, baseline.ServerTests.request

    def test_inspect_thirteen_slide_pptx_without_model_or_credentials(self):
        payload = {"filename": "course.pptx", "content": base64.b64encode(text_pptx(13)).decode()}
        with patch("engine.OpenAI", side_effect=AssertionError("Inspection must stay local")):
            status, body = self.request("POST", "/api/inspect", payload)
        self.assertEqual(status, 200)
        preview = json.loads(body)
        self.assertEqual(preview["pages"], 13)
        self.assertEqual(preview["segments"], 26)
        self.assertEqual(preview["vision_pages"], list(range(1, 14)))
        self.assertEqual(preview["normal_calls"], 42)
        self.assertFalse(self.server.store.list())

    def test_list_lookup_accepts_empty_model_with_explicit_or_saved_key(self):
        payload = {"connection": {"base_url": "https://relay.example/v1", "model": "", "api_key": "RELAY-TEST-KEY"}}
        with patch("app.list_models", return_value={"models": ["model-a", "model-b"], "message": "offline"}) as models:
            status, body = self.request("POST", "/api/models", payload)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["models"], ["model-a", "model-b"])
            self.assertEqual(models.call_args.args[0].api_key, "RELAY-TEST-KEY")
            self.assertEqual(models.call_args.args[0].model, "")
        with patch("credentials.protect", lambda raw: b"TEST:" + raw[::-1]), patch("credentials.unprotect", lambda blob: blob[5:][::-1]):
            self.server.credentials.save(profile(payload["connection"] | {"model": "saved-model"}), None)
            payload["connection"]["api_key"] = ""
            with patch("app.list_models", return_value={"models": [], "message": "empty"}) as models:
                self.assertEqual(self.request("POST", "/api/models", payload)[0], 200)
                self.assertEqual(models.call_args.args[0].api_key, "RELAY-TEST-KEY")
            with patch("app.probe") as call:
                self.assertEqual(self.request("POST", "/api/probe", payload)[0], 400)
                call.assert_not_called()

    def test_old_history_exports_docx_without_any_engine_call(self):
        doc = document(); result = result_for(doc); job_id = "a" * 32
        self.server.store.save({"id": job_id, "status": "done", "result": result, "filename": doc["filename"]})
        self.server.engine_factory = lambda *args: self.fail("DOCX must not call a model")
        status, body = self.request("GET", f"/api/jobs/{job_id}/notes.docx")
        self.assertEqual(status, 200)
        self.assertEqual(body[:2], b"PK")

    def test_save_and_reload_connection_never_returns_key_to_browser(self):
        payload={"connection":{"base_url":"https://saved.example/v1","model":"saved-model","api_key":"SAVED-TEST-KEY"}}
        with patch("credentials.protect", lambda raw:b"TEST:"+raw[::-1]), patch("credentials.unprotect", lambda blob:blob[5:][::-1]):
            status, body=self.request("POST","/api/connections",payload)
            self.assertEqual(status,200)
            self.assertNotIn(b"SAVED-TEST-KEY",body)
            self.server.credentials=CredentialStore(Path(self.directory.name))
            status, body=self.request("GET","/api/config")
            self.assertEqual(status,200)
            config=json.loads(body)["connections"]["text"]
            self.assertEqual(config["model"],"saved-model")
            self.assertTrue(config["has_saved_key"])
            self.assertNotIn(b"SAVED-TEST-KEY",body)
            with patch("app.list_models",return_value={"models":[],"message":"offline"}) as models:
                status,_=self.request("POST","/api/models",{"connection":{"base_url":config["base_url"],"model":config["model"]}})
                self.assertEqual(status,200)
                self.assertEqual(models.call_args.args[0].api_key,"SAVED-TEST-KEY")
            self.assertEqual(self.request("POST","/api/connections/clear",{})[0],200)
            self.assertFalse(self.server.credentials.path.exists())

    def test_custom_provider_must_not_borrow_environment_key(self):
        payload = {"filename": "test.txt", "content": base64.b64encode(b"valid text content").decode(),
            "connection": {"base_url": "https://other.example/v1", "model": "other"}}
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "NEVER-FORWARD"}):
            self.assertEqual(self.request("POST", "/api/jobs", payload)[0], 400)
        self.assertFalse(self.server.store.list())

    def test_pptx_requires_consent_and_separate_vision_key(self):
        payload = {"filename": "test.pptx", "content": base64.b64encode(FIXTURE.read_bytes()).decode(), "api_key": "TEXT-KEY"}
        self.assertEqual(self.request("POST", "/api/jobs", payload)[0], 400)
        payload["visual_consent"] = True
        payload["vision_connection"] = {"base_url": "https://vision.example", "model": "vision"}
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "NEVER-FORWARD"}):
            self.assertEqual(self.request("POST", "/api/jobs", payload)[0], 400)
        self.assertFalse(self.server.store.list())

    def test_model_api_errors_do_not_expose_keys(self):
        payload = {"connection": {"api_key": "SECRET-TEST"}}
        with patch("app.list_models", return_value={"models": ["text"], "message": "test"}):
            status, body = self.request("POST", "/api/models", payload)
            self.assertEqual(status, 200)
            self.assertNotIn(b"SECRET-TEST", body)
        with patch("app.probe", side_effect=RuntimeError("SECRET-TEST")):
            status, body = self.request("POST", "/api/probe", payload)
            self.assertEqual(status, 502)
            self.assertNotIn(b"SECRET-TEST", body)

    def test_text_and_vision_keys_are_snapshotted_but_never_saved(self):
        seen = []
        class CaptureEngine:
            def __init__(self, key, doc, options, notify):
                self.doc = doc
                seen.append((options["_connection"], options["_vision_connection"]))
            def run(self):
                return result_for(self.doc)
        self.server.engine_factory = CaptureEngine
        payload = {"filename":"test.pptx", "content":base64.b64encode(FIXTURE.read_bytes()).decode(),
            "connection":{"base_url":"https://text.example/v1","model":"text","api_key":"TEXT-ONLY-SECRET"},
            "vision_connection":{"base_url":"https://vision.example/v1","model":"vision","api_key":"VISION-ONLY-SECRET"},
            "visual_consent":True}
        status, body = self.request("POST", "/api/jobs", payload)
        self.assertEqual(status, 202)
        job_id = json.loads(body)["id"]
        for _ in range(100):
            job = self.server.store.get(job_id)
            if job["status"] not in {"queued","running"}:
                break
            time.sleep(.02)
        self.assertEqual(job["status"], "done")
        self.assertEqual(seen[0][0].api_key, "TEXT-ONLY-SECRET")
        self.assertEqual(seen[0][1].api_key, "VISION-ONLY-SECRET")
        self.assertEqual(job["options"]["text_connection"]["model"], "text")
        for path in Path(self.directory.name).rglob("*"):
            if path.is_file():
                for secret in (b"TEXT-ONLY-SECRET",b"VISION-ONLY-SECRET"):
                    self.assertNotIn(secret, path.read_bytes())
