"""Local text extraction. PDF page numbers always mean physical file pages."""

import io
import re
import math
from pathlib import Path

from pypdf import PdfReader


MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 200
MAX_CHARS = 60_000
MAX_TEXT_UNITS = 24
MAX_VISION_PAGES = 24
UNIT_CHARS = 3_500


def clean_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    if not name or len(name) > 180 or Path(name).suffix.lower() not in {".pdf", ".pptx", ".txt", ".md"}:
        raise ValueError("请选择 PDF、PPTX、TXT 或 Markdown 文件，文件名不超过180个字符。")
    return name


def _split(text: str) -> list[str]:
    pieces = []
    while text:
        if len(text) <= UNIT_CHARS:
            pieces.append(text)
            break
        end = text.rfind("\n", UNIT_CHARS // 2, UNIT_CHARS)
        if end < 0:
            end = UNIT_CHARS
        piece, text = text[:end].strip(), text[end:].strip()
        if piece:
            pieces.append(piece)
    return pieces


def selected_pages(expression: str, total: int) -> list[int]:
    if not isinstance(expression, str) or len(expression) > 500:
        raise ValueError("页码范围格式无效。")
    if not expression.strip():
        return list(range(1, total + 1))
    pages = set()
    for piece in expression.replace("，", ",").split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", piece)
        if not match:
            raise ValueError("页码请填写如1-10,15；使用文件物理页序。")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= total:
            raise ValueError(f"页码必须在1至{total}之间，且起点不大于终点。")
        pages.update(range(start, end + 1))
    return sorted(pages)


def budget_document(document: dict, questions=8, cards=12, budget=120) -> dict:
    if type(budget) is not int or not 1 <= budget <= 200:
        raise ValueError("请求预算应为1至200之间的整数。")
    n = len(document["sources"])
    vision = len(document.get("vision_pages", []))
    # Each source yields at most 5 notes. All later prompts are bounded batches.
    normal = n + vision + 3
    groups = math.ceil(5 * n / 8)
    audit_items = 5 * n + questions + cards
    # Five-item audit batches can split down to single items on truncation.
    # A full binary tree has 2B-1 nodes per batch. Allow two calls at each
    # node (a format repair may precede a truncation), still capped by budget.
    audit_maximum = 4 * audit_items - 2 * math.ceil(audit_items / 5)
    maximum = 2 * (n + vision + min(groups, questions) + min(groups, cards)) + audit_maximum
    if budget < normal:
        raise ValueError(f"此范围至少需要{normal}次请求，超过预算{budget}；请缩小页码范围或提高预算。")
    document.update(normal_calls=normal, max_calls=min(budget, maximum), request_budget=budget,
                    theoretical_max_calls=maximum)
    return document


def extract_document(filename: str, content: bytes, page_range="", *, questions=8, cards=12, budget=120) -> dict:
    filename = clean_filename(filename)
    if not content or len(content) > MAX_BYTES:
        raise ValueError("文件不能为空，且大小不能超过50 MiB。")
    pages = []
    warnings = []
    total, chosen, vision_pages = None, [], []
    if filename.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("此 PDF 已加密，请先在本机解密后导入。")
            if len(reader.pages) > MAX_PAGES:
                raise ValueError(f"支持最多{MAX_PAGES}页 PDF，请先拆分文件。")
            total = len(reader.pages)
            chosen = selected_pages(page_range, total)
            for index in chosen:
                page = reader.pages[index - 1]
                text = (page.extract_text() or "").replace("\x00", "").strip()
                pages.append((index, text))
                if not text:
                    warnings.append(f"PDF第{index}页未提取到文字，可能是空白页或图片页，未纳入内容生成。")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法解析此 PDF，请确认文件完整、可打开，且包含可复制的文字。") from exc
    elif filename.lower().endswith(".pptx"):
        from pptx import Presentation
        from pptx_render import validate_package
        validate_package(content)
        try:
            deck = Presentation(io.BytesIO(content))
            total = len(deck.slides)
            if not 1 <= total <= MAX_PAGES:
                raise ValueError(f"支持1至{MAX_PAGES}页PPTX，请先拆分文件。")
            chosen = selected_pages(page_range, total)
            if len(chosen) > MAX_VISION_PAGES:
                raise ValueError(f"一次最多识别{MAX_VISION_PAGES}张幻灯片；请填写页码范围分批生成。")

            def shape_text(shape):
                if shape.shape_type == 6:  # group shapes
                    return "\n".join(shape_text(child) for child in shape.shapes)
                if shape.has_table:
                    return "\n".join(" | ".join(cell.text for cell in row.cells) for row in shape.table.rows)
                if getattr(shape, "has_chart", False):
                    try:
                        rows = []
                        for plot in shape.chart.plots:
                            labels = [category.label for category in plot.categories]
                            for series in plot.series:
                                rows.append(f"系列{series.name}：" + "; ".join(
                                    f"{labels[i] if i < len(labels) else i + 1}={value}"
                                    for i, value in enumerate(series.values)))
                        return "图表内嵌数据：\n" + "\n".join(rows)
                    except Exception:
                        warnings.append(f"幻灯片第{index}页的图表数据无法直接提取，请对照整页识别图复核。")
                        return ""
                return shape.text if shape.has_text_frame else ""

            for index in chosen:
                text = "\n".join(shape_text(shape) for shape in deck.slides[index - 1].shapes).replace("\x00", "").strip()
                pages.append((index, text))
                vision_pages.append(index)
            warnings.append("PPTX将逐页渲染整页图片并交给视觉模型，覆盖图片、图表和布局；视觉识别不等于原文核验。演讲备注、动画过程及视频不纳入。")
        except ValueError:
            raise
        except Exception:
            raise ValueError("无法解析PPTX，请确认它是完整的普通.pptx文件。") from None
    else:
        if page_range.strip():
            raise ValueError("TXT和Markdown没有物理页码，请清空页码范围。")
        text = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("文本编码无法识别，请另存为 UTF-8 再导入。")
        pages = [(None, text.replace("\x00", "").strip())]

    characters = sum(len(text) for _, text in pages)
    if not characters and not vision_pages:
        raise ValueError("未找到可提取的文字。扫描版 PDF 需要先做 OCR；本版尚未提供 OCR。")
    if characters > MAX_CHARS:
        raise ValueError(f"文字超过{MAX_CHARS:,}字，请按章节拆分；程序不会悄悄截掉后面的内容。")

    sources = []
    for page_number, text in pages:
        for part, piece in enumerate(_split(text), 1):
            prefix = "幻灯片" if vision_pages else "PDF"
            locator = f"{prefix}第{page_number}页·片段{part}" if page_number else f"文本片段{part}"
            sources.append({"id": f"S{len(sources) + 1:03}", "page": page_number,
                            "locator": locator, "text": piece, "kind": "text"})
    # Text chunks and whole-slide images have separate bounds. Counting an image
    # against the text allowance would reject ordinary 13-slide decks as 26 units.
    if len(sources) > MAX_TEXT_UNITS:
        raise ValueError(f"本批文字超过{MAX_TEXT_UNITS}段，请缩小页码范围后分批生成。")
    for page_number in vision_pages:
        sources.append({"id": f"V{page_number:03}", "page": page_number, "kind": "visual",
                        "locator": f"幻灯片第{page_number}页·图像识别", "text": "",
                        "image": f"slides/slide-{page_number:04}.png"})
    if total and len(chosen) < total:
        warnings.append(f"本批仅处理原文件第{','.join(map(str, chosen))}页；未选中的页不会发送或纳入生成。")
    return budget_document({"filename": filename, "pages": total, "selected_pages": chosen,
            "page_range": page_range, "vision_pages": vision_pages,
            "characters": characters, "sources": sources, "warnings": warnings}, questions, cards, budget)


def normalized(text: str) -> str:
    # Only ignore whitespace; don't accept paraphrases as original quotations.
    return re.sub(r"\s+", "", text)


def build_evidence(sources: list[dict]) -> list[dict]:
    """Create selectable, overlapping original-text windows, never model-written quotes."""
    evidence = []
    for source in sources:
        text = source["text"]
        part, start = 1, 0
        while start < len(text):
            end = min(start + 180, len(text))
            quote = text[start:end].strip()
            if len(normalized(quote)) >= 5:
                evidence.append({"evidence_id": f"{source['id']}-E{part:03}",
                                 "source_id": source["id"], "quote": quote,
                                 "kind": source.get("kind", "text")})
                part += 1
            if end == len(text):
                break
            start = end - 60
    return evidence


def check_citations(items: list[dict], sources: list[dict]) -> None:
    source_map = {source["id"]: source for source in sources}
    for index, item in enumerate(items):
        for cite_index, cite in enumerate(item["citations"]):
            field = f"条目[{index + 1}].citations[{cite_index + 1}]"
            source = source_map.get(cite["source_id"])
            quote = normalized(cite["quote"])
            if source is None:
                raise ValueError(f"{field}.source_id不存在于本次允许的来源中。")
            if len(quote) < 5:
                raise ValueError(f"{field}.quote不足5个非空白字符。")
            if quote not in normalized(source["text"]):
                raise ValueError(f"{field}.quote不是{source['id']}原文中的连续摘录，不能改写或拼接。")
