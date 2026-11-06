"""Local-only web application. Start with this project's existing venv Python."""

import argparse
import base64
import binascii
import json
import os
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from documents import MAX_BYTES, extract_document
from engine import MODEL, StudyEngine, now, user_error
from exports import anki_tsv, markdown, word_document
from model_connection import DEFAULT_URL, RequestLimiter, list_models, probe, validate_concurrency
from credentials import CredentialStore
from runtime_settings import RuntimeSettings


APP_DIR = Path(__file__).resolve().parent


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "history.sqlite3"
        self.lock = threading.RLock()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        # A stopped process cannot resume an in-memory API task. Keep the original document for retry.
        for job in self.list():
            if job["status"] in {"queued", "running"}:
                self.update(job["id"], status="failed", error="上次运行被中断，请重新生成。")

    def get(self, job_id: str) -> dict:
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("没有找到该任务。")
        return json.loads(row[0])

    def list(self) -> list[dict]:
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT body FROM jobs ORDER BY rowid DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, job: dict) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO jobs(id, body) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                         (job["id"], json.dumps(job, ensure_ascii=False)))

    def update(self, job_id: str, **fields) -> dict:
        # Serialize read-modify-write updates from parallel model callbacks.
        with self.lock:
            job = self.get(job_id)
            job.update(fields)
            self.save(job)
            return job


class StudyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, directory: Path, engine_factory=StudyEngine):
        super().__init__(address, Handler)
        self.store = Store(directory)
        self.credentials = CredentialStore(directory)
        self.settings = RuntimeSettings(directory)
        self.model_limiter = RequestLimiter(self.settings.concurrency)
        self.model_lock = threading.RLock()
        self.active_job = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.slots = threading.BoundedSemaphore(3)
        self.engine_factory = engine_factory

    def configure_concurrency(self, value):
        with self.model_lock:
            settings = self.settings.save(value)
            if not self.active_job:
                self.model_limiter.set_limit(settings["concurrency"])
            return settings

    def run_job(self, job_id: str, api_key: str, options: dict) -> None:
        secrets = [config.api_key for config in (options.get("_connection"), options.get("_vision_connection"))
                   if config and config.api_key]

        def redact(text):
            for secret in secrets:
                text = text.replace(secret, "[已隐藏]")
            return text

        def notify(stage, message, progress):
            with self.store.lock:
                job = self.store.get(job_id)
                job["events"].append({"time": now(), "stage": stage, "message": redact(message)})
                job["events"] = job["events"][-150:]
                if progress is not None:
                    job["progress"] = max(job["progress"], progress)
                self.store.save(job)

        try:
            with self.model_lock:
                self.active_job = True
                self.model_limiter.set_limit(options["concurrency"])
            self.store.update(job_id, status="running", progress=5)
            notify("schedule", f"本次任务使用{options['concurrency']}路并发上限。", 5)
            document = self.store.get(job_id)["document"]
            result = self.engine_factory(api_key, document, options | {"_request_limiter": self.model_limiter}, notify).run()
            result = json.loads(redact(json.dumps(result, ensure_ascii=False)))
            folder = self.store.directory / job_id
            (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "notes.md").write_text(markdown(result), encoding="utf-8")
            (folder / "cards.tsv").write_text(anki_tsv(result), encoding="utf-8-sig")
            notify("done", "笔记、练习题、卡片已生成，结果已保存到本机。", 100)
            self.store.update(job_id, status="done", result=result)
        except Exception as exc:
            self.store.update(job_id, status="failed", error=redact(user_error(exc)))
        finally:
            with self.model_lock:
                self.active_job = False
                self.model_limiter.set_limit(self.settings.concurrency)
            self.slots.release()

    def server_close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: StudyServer

    def log_message(self, format, *args):
        # Keys, request bodies and document text must not appear in server logs.
        pass

    def respond(self, status: int, body, content_type="application/json; charset=utf-8", attachment=None):
        if content_type.startswith("application/json") and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self) -> bool:
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {"http://" + host for host in hosts}

    def body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 JSON 格式。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("请求长度无效。") from None
        if not 0 < length <= 70 * 1024 * 1024:
            raise ValueError("请求为空或文件过大。")
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("JSON 请求格式错误。") from None
        if not isinstance(data, dict):
            raise ValueError("JSON 请求必须是对象。")
        return data

    def uploaded_document(self, data: dict) -> tuple[dict, bytes]:
        filename, encoded = data.get("filename"), data.get("content")
        if not isinstance(filename, str) or not isinstance(encoded, str):
            raise ValueError("缺少文件名或文件内容。")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("文件编码无效。") from None
        questions, cards = data.get("questions", 8), data.get("cards", 12)
        if type(questions) is not int or type(cards) is not int or not 3 <= questions <= 20 or not 4 <= cards <= 24:
            raise ValueError("练习题数量需为3至20，卡片数量需为4至24。")
        return extract_document(filename, content, data.get("page_range", ""), questions=questions, cards=cards,
                                budget=data.get("request_budget", 120)), content

    def do_GET(self):
        if not self.allowed():
            self.respond(403, {"error": "此服务仅允许本机同源访问。"})
            return
        path = urlsplit(self.path).path
        try:
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/sample.txt": ("sample.txt", "text/plain; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in static:
                filename, kind = static[path]
                self.respond(200, (APP_DIR / "static" / filename).read_bytes(), kind)
            elif path == "/api/config":
                try:
                    connections, storage_error = self.server.credentials.public(), None
                except ValueError as exc:
                    connections, storage_error = {}, str(exc)
                self.respond(200, {"model": MODEL, "has_key": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
                                   "base_url": DEFAULT_URL, "max_bytes": MAX_BYTES,
                                   "settings": self.server.settings.public(), "settings_error": self.server.settings.error,
                                   "connections": connections, "storage_error": storage_error})
            elif path == "/api/jobs":
                self.respond(200, [{key: job[key] for key in ("id", "filename", "status", "created_at", "progress")}
                                   for job in self.server.store.list()])
            elif match := re.fullmatch(r"/api/jobs/([0-9a-f]{32})/slides/(slide-(\d{4})\.png)", path):
                job_id, filename, page = match.groups()
                job = self.server.store.get(job_id)
                if int(page) not in job["document"].get("vision_pages", []):
                    raise ValueError("此页不在本批资料中。")
                self.respond(200, (self.server.store.directory / job_id / "slides" / filename).read_bytes(), "image/png")
            elif match := re.fullmatch(r"/api/jobs/([0-9a-f]{32})(?:/(notes\.md|notes\.docx|cards\.tsv|result\.json|source))?", path):
                job_id, asset = match.groups()
                job = self.server.store.get(job_id)
                if asset:
                    if asset == "source":
                        suffix = Path(job["filename"]).suffix.lower()
                        kind = {".pdf": "application/pdf", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}.get(suffix, "text/plain; charset=utf-8")
                        self.respond(200, (self.server.store.directory / job_id / ("source" + suffix)).read_bytes(), kind,
                                     attachment="source" + suffix)
                    else:
                        if job["status"] != "done":
                            raise ValueError("任务尚未完成，没有可下载的结果。")
                        if asset == "notes.docx":
                            self.respond(200, word_document(job["result"]),
                                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                         attachment=asset)
                        else:
                            self.respond(200, (self.server.store.directory / job_id / asset).read_bytes(),
                                         "application/octet-stream", attachment=asset)
                else:
                    self.respond(200, job)
            else:
                self.respond(404, {"error": "没有找到页面。"})
        except (ValueError, FileNotFoundError) as exc:
            self.respond(404, {"error": str(exc)})

    def do_POST(self):
        if not self.allowed():
            self.respond(403, {"error": "此服务仅允许本机同源访问。"})
            return
        acquired = False
        try:
            path = urlsplit(self.path).path
            if path not in {"/api/inspect", "/api/jobs", "/api/models", "/api/probe", "/api/connections", "/api/connections/clear", "/api/settings"}:
                self.respond(404, {"error": "没有找到接口。"})
                return
            data = self.body()
            if path == "/api/settings":
                settings = self.server.configure_concurrency(data.get("concurrency"))
                self.respond(200, {"settings": settings, "message": f"已保存{settings['concurrency']}路；用于新任务，运行中的任务保持原设置。"})
                return
            if path == "/api/connections/clear":
                self.server.credentials.clear()
                self.respond(200, {"message": "已清除本机保存的连接和Key，正在运行的任务继续使用提交时的配置。"})
                return
            if path == "/api/connections":
                connection = self.server.credentials.resolve(data.get("connection", {}), "text",
                    fallback_key=os.getenv("DEEPSEEK_API_KEY", ""))
                vision, reuse = None, data.get("reuse_text_connection") is True
                visual_data = data.get("vision_connection", {})
                if not isinstance(visual_data, dict) or not isinstance(visual_data.get("base_url", ""), str):
                    raise ValueError("视觉连接格式无效。")
                if reuse:
                    vision = connection
                elif visual_data.get("base_url", "").strip():
                    vision = self.server.credentials.resolve(visual_data, "vision")
                self.server.credentials.save(connection, vision, reuse)
                self.respond(200, {"message": "已使用Windows当前用户加密保存。下次打开可复用，不向页面回传Key。",
                                   "connections": self.server.credentials.public()})
                return
            if path in {"/api/models", "/api/probe"}:
                fallback = os.getenv("DEEPSEEK_API_KEY", "") if (data.get("profile_kind") != "vision" or data.get("reuse_text_connection") is True) else ""
                kind = "vision" if data.get("profile_kind") == "vision" and data.get("reuse_text_connection") is not True else "text"
                connection = self.server.credentials.resolve(data.get("connection", {}), kind, fallback_key=fallback,
                                                             require_model=path != "/api/models")
                try:
                    result = (list_models(connection, limiter=self.server.model_limiter) if path == "/api/models"
                              else probe(connection, data.get("kind", "text"), limiter=self.server.model_limiter))
                except ValueError:
                    raise
                except Exception as exc:
                    self.respond(502, {"error": user_error(exc)})
                    return
                self.respond(200, result)
                return
            document, content = self.uploaded_document(data)
            if path == "/api/inspect":
                self.respond(200, {key: document[key] for key in
                                   ("filename", "pages", "characters", "warnings", "normal_calls", "max_calls",
                                    "selected_pages", "vision_pages", "request_budget", "theoretical_max_calls")} |
                             {"segments": len(document["sources"]), "preview": document["sources"][0]["text"][:600]})
                return
            concurrency = validate_concurrency(data.get("concurrency", self.server.settings.concurrency))
            api_key = data.get("api_key", "")
            focus = data.get("focus", "掌握核心概念，为课程复习做准备。")
            if not isinstance(api_key, str) or not isinstance(focus, str) or len(focus) > 500:
                raise ValueError("Key或学习目标格式无效；目标不超过500字。")
            connection = self.server.credentials.resolve(data.get("connection", {"api_key": api_key}), "text",
                                 fallback_key=os.getenv("DEEPSEEK_API_KEY", ""))
            api_key = connection.api_key
            vision = None
            if document.get("vision_pages"):
                if data.get("visual_consent") is not True:
                    raise ValueError("PPTX需要先同意向所填视觉服务发送选中页的整页图片。")
                vision = connection if data.get("reuse_text_connection") is True else self.server.credentials.resolve(data.get("vision_connection", {}), "vision")
            questions, cards = data.get("questions", 8), data.get("cards", 12)
            if type(questions) is not int or type(cards) is not int or not 3 <= questions <= 20 or not 4 <= cards <= 24:
                raise ValueError("练习题数量需为3至20，卡片数量需为4至24。")
            if not self.server.slots.acquire(blocking=False):
                self.respond(429, {"error": "当前已有3个任务正在处理或排队，请稍后再提交。"})
                return
            acquired = True
            job_id = uuid.uuid4().hex
            folder = self.server.store.directory / job_id
            folder.mkdir()
            (folder / ("source" + Path(document["filename"]).suffix.lower())).write_bytes(content)
            options = {"focus": focus.strip() or "掌握资料中的核心知识。", "questions": questions, "cards": cards,
                       "concurrency": concurrency,
                       "page_range": document["page_range"], "request_budget": document["request_budget"],
                       "text_connection": connection.public(),
                       "vision_connection": vision.public() if vision else None}
            job = {"id": job_id, "filename": document["filename"], "status": "queued", "created_at": now(),
                   "progress": 0, "events": [], "document": document, "result": None, "error": None,
                   "options": options}
            self.server.store.save(job)
            transient = options | {"_connection": connection, "_vision_connection": vision,
                                   "_visual_consent": data.get("visual_consent") is True, "_job_dir": str(folder)}
            self.server.executor.submit(self.server.run_job, job_id, api_key, transient)
            acquired = False  # Worker now owns this slot. API key is never added to persistent state.
            self.respond(202, {"id": job_id})
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except Exception:
            self.respond(500, {"error": "提交任务失败，请确认本地数据目录可写。"})
        finally:
            if acquired:
                self.server.slots.release()


def main():
    parser = argparse.ArgumentParser(description="资料学习助手：本地网页 + 可配置模型 + LangGraph")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=APP_DIR / "data")
    parser.add_argument("--render-self-test", action="store_true", help="用合成课件检查PowerPoint，不调用模型")
    parser.add_argument("--encryption-self-test", action="store_true", help="检查Windows用户加密，不保存Key")
    args = parser.parse_args()
    if args.encryption_self_test:
        from credentials import protect, unprotect
        try:
            sample = os.urandom(32)
            if unprotect(protect(sample)) != sample:
                raise ValueError("Windows加密回读不一致。")
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        print("Windows用户加密自检通过，未保存任何Key或调用模型。")
        return
    if args.render_self_test:
        from pptx_render import render_slides
        try:
            images = render_slides(APP_DIR / "tests/fixtures/diagram.pptx", APP_DIR / ".qa/local-render/slides", [1, 2])
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        print(f"PowerPoint渲染自检通过：{len(images)}页。未调用模型，图片位于本项目.qa/local-render/slides。")
        return
    try:
        server = StudyServer(("127.0.0.1", args.port), args.data_dir)
    except OSError as exc:
        raise SystemExit(f"启动失败：{exc}。如端口被占用，可增加 --port 8766。") from None
    print(f"资料学习助手已启动：http://127.0.0.1:{server.server_port}", flush=True)
    print(f"资料和生成结果保存在：{server.store.directory}", flush=True)
    print("保持此终端运行，按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止；已开始的模型请求可能需要短暂收尾。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
