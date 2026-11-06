"""Opt-in live regression: saved connection + original inputs, no desktop control.

Run in the Windows user's own PowerShell so DPAPI can decrypt the saved Key.
This sends the failed task's selected text/images to its saved API service.
It reuses that task's already rendered slide PNGs; it does not launch Office.
The main server, history, connection settings and original files are unchanged.
"""
import argparse
import io
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from credentials import CredentialStore
from documents import check_citations
from engine import Audit, COMMON, StudyEngine, now, response_stats, user_error
from exports import anki_tsv, markdown, word_document
from model_connection import transport
from openai import OpenAI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=APP / "data")
    args = parser.parse_args()
    if len(args.job_id) != 32 or any(c not in "0123456789abcdef" for c in args.job_id):
        parser.error("invalid job id")
    qa = APP / ".qa" / ("audit-live-" + now().replace(":", "").replace("+", "_") + "-" + args.job_id[:8])
    qa.mkdir(parents=True, exist_ok=False)
    report = {"started_at": now(), "job_id": args.job_id, "status": "running",
              "rendering": "Reused previously rendered slides; Office rendering was not tested.",
              "events": []}

    def save():
        (qa / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def notify(stage, message, progress):
        print(f"[{stage}] {message}", flush=True)
        report["events"].append({"time": now(), "stage": stage, "message": message})
        save()

    engine = None
    secrets = []
    try:
        # Do not construct Store: its startup recovery would mutate active jobs.
        with sqlite3.connect((args.data_dir / "history.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT body FROM jobs WHERE id=?", (args.job_id,)).fetchone()
        if not row:
            raise ValueError("找不到原任务。")
        job = json.loads(row[0])
        original = job["options"]
        text = CredentialStore(args.data_dir).resolve(original["text_connection"], "text")
        # Resolve each original role/address; a later preference change must not
        # redirect the original task's images to another saved text connection.
        vision = CredentialStore(args.data_dir).resolve(original["vision_connection"], "vision") \
            if job["document"].get("vision_pages") else None
        secrets = [c.api_key for c in (text, vision) if c]
        report["text_connection"] = text.public()
        report["vision_connection"] = vision.public() if vision else None
        document = job["document"]
        document["max_calls"] = min(document.get("request_budget", 120), 120)
        source_dir = args.data_dir / args.job_id
        for source in document["sources"]:
            if source.get("kind") == "visual":
                path = Path(source["image"])
                if path.is_absolute() or path.drive or ".." in path.parts:
                    raise ValueError("原任务图片路径无效。")
                (qa / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_dir / path, qa / path)
        opts = {key: original[key] for key in ("focus", "questions", "cards", "concurrency")}
        opts.update(_connection=text, _vision_connection=vision, _visual_consent=True, _job_dir=str(qa))
        engine = StudyEngine(text.api_key, document, opts, notify)

        def cached_render(_source, _dest, pages):
            if list(pages) != document["vision_pages"]:
                raise ValueError("缓存页码与任务不一致。")
            for source in document["sources"]:
                if source.get("kind") == "visual" and not (qa / source["image"]).is_file():
                    raise ValueError("缓存的幻灯片图片缺失。")

        print("真实模型测试开始；复用既有渲染图，不操作桌面，不修改原历史。", flush=True)
        with patch("pptx_render.render_slides", side_effect=cached_render):
            result = engine.run()
        for kind in ("notes", "questions", "cards"):
            if not result[kind]:
                raise ValueError("真实生成有一类结果为空。")
            check_citations(result[kind], result["sources"])
        (qa / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (qa / "notes.md").write_text(markdown(result), encoding="utf-8")
        (qa / "cards.tsv").write_text(anki_tsv(result), encoding="utf-8-sig")
        docx = word_document(result)
        from docx import Document
        Document(io.BytesIO(docx))
        (qa / "notes.docx").write_bytes(docx)
        report.update(status="comparison_running", counts={k:len(result[k]) for k in ("notes", "questions", "cards")},
                      removed_count=result["removed_count"], usage=result["usage"],
                      responses=engine.responses,
                      exports="JSON, Markdown, Anki and DOCX generated; DOCX structurally reopened, no visual QA")
        save()

        # One diagnostic call reproduces the PREVIOUS parameters on the first ten
        # freshly generated entries. Original failed intermediate outputs weren't
        # persisted, so this is representative, not an exact replay of that output.
        batch = (result["notes"] + result["questions"] + result["cards"])[:10]
        role = ("审核Agent：逐项检查内容与所附证据是否一致，尤其是答案、数值、条件及因果关系。"
                "visual来源是模型图像识别，未经过独立图像复核；不得把它说成逐字核验的原文。"
                "issues只列需要删除的错误或证据不足条目，使用条目id并解释原因。正确条目不列出。"
                "不要因没有覆盖所有知识点而删除条目。有充分依据时issues为空。")
        provenance = {s["id"]:s.get("kind","text") for s in document["sources"]}
        messages = [{"role":"system", "content":COMMON + role + "\nJSON Schema：" + json.dumps(Audit.model_json_schema(), ensure_ascii=False)},
                    {"role":"user", "content":"来源类型：" + json.dumps(provenance,ensure_ascii=False) + "\n待审核条目及证据：\n" + json.dumps(batch,ensure_ascii=False)}]
        print("正在做一次旧参数对照（10条、6500 tokens、服务默认思考强度）……", flush=True)
        try:
            with OpenAI(api_key=text.api_key, base_url=text.base_url, timeout=90, max_retries=0, http_client=transport()) as client:
                response = client.chat.completions.create(model=text.model, messages=messages, max_tokens=6500,
                    **({"response_format":{"type":"json_object"}} if text.json_mode else {}))
            report["old_parameters_comparison"] = response_stats(response, 6500)
            report["old_parameters_comparison"]["scope"] = "Representative new first-ten items, not original unsaved outputs"
        except Exception as exc:
            report["old_parameters_comparison"] = {"error":user_error(exc)}
        report.update(status="passed", completed_at=now())
        save()
        print("测试通过：" + json.dumps(report["counts"], ensure_ascii=False), flush=True)
        print("验证报告：" + str(qa / "report.json"), flush=True)
        return 0
    except Exception as exc:
        detail = user_error(exc)
        for secret in secrets:
            detail = detail.replace(secret, "[已隐藏]")
        report.update(status="failed", error=detail, completed_at=now())
        if engine:
            report["responses"] = engine.responses
            report["calls"] = engine.calls
        save()
        print("测试未通过：" + detail, flush=True)
        print("验证报告：" + str(qa / "report.json"), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
