import html
import json
import io
import re

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


def citations_text(item: dict, sources: dict) -> str:
    return "；".join(f"{sources[cite['source_id']]['locator']}" +
                    ("（模型识别，需复核原图）" if sources[cite['source_id']].get('kind') == 'visual' else "") +
                    f"：『{cite['quote']}』"
                    for cite in item["citations"])


def markdown(result: dict) -> str:
    sources = {source["id"]: source for source in result["sources"]}
    lines = [f"# {result['filename']} · 学习资料", "", f"学习目标：{result['focus']}", "",
             f"生成时间：{result['completed_at']}", "",
             "出处说明：页码为文件物理页序。文字引用已做原文匹配；图像出处为模型识别，需对照原图复核。", ""]
    for title, key in (("知识笔记", "notes"), ("练习题", "questions"), ("复习卡片", "cards")):
        lines.extend([f"## {title}", ""])
        for index, item in enumerate(result[key], 1):
            if key == "notes":
                lines.extend([f"### {index}. {item['title']}", "", item["content"], ""])
            elif key == "questions":
                lines.extend([f"### {index}. {item['question']}", "", f"答案：{item['answer']}", "",
                              f"解析：{item['explanation']}", ""])
            else:
                lines.extend([f"### {index}. {item['front']}", "", item["back"], ""])
            lines.extend(["> 出处：" + citations_text(item, sources), ""])
    lines.extend(["## 审核与用量", "", result["audit"]["summary"], "",
                  f"审核剔除：{result['removed_count']}条。", ""])
    for issue in result["audit"]["issues"]:
        lines.append(f"- {issue['item_id']}：{issue['reason']}")
    for warning in result["warnings"]:
        lines.append(f"- 导入提示：{warning}")
    lines.extend(["", "用量：" + json.dumps(result["usage"], ensure_ascii=False), ""])
    return "\n".join(lines)


def anki_tsv(result: dict) -> str:
    sources = {source["id"]: source for source in result["sources"]}
    rows = []
    for card in result["cards"]:
        # HTML is escaped before Anki's optional HTML rendering; never export executable markup.
        front = html.escape(card["front"]).replace("\t", " ").replace("\n", "<br>")
        back = html.escape(card["back"] + "\n出处：" + citations_text(card, sources))
        rows.append(front + "\t" + back.replace("\t", " ").replace("\n", "<br>"))
    return "\n".join(rows) + "\n"


def word_document(result: dict) -> bytes:
    """Export saved results, including V1 histories, without contacting a model."""
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(.8)
    section.left_margin = section.right_margin = Inches(.9)
    for name, size in (("Normal", 11), ("Title", 24), ("Heading 1", 18), ("Heading 2", 13), ("Quote", 10)):
        style = doc.styles[name]
        style.font.name, style.font.size = "Microsoft YaHei", Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft YaHei")
        style.paragraph_format.line_spacing = 1.35
        style.paragraph_format.space_after = Pt(8)
        if name != "Normal":
            style.paragraph_format.keep_with_next = name != "Quote"
    doc.core_properties.author = doc.core_properties.last_modified_by = ""

    def paragraph(text, style=None):
        # XML 1.0 disallows control characters even when model JSON accepts them.
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", str(text))
        return doc.add_paragraph(text, style)

    paragraph("课程学习资料", "Title")
    paragraph(result["filename"])
    paragraph("学习目标：" + result["focus"])
    paragraph("生成时间：" + result["completed_at"])
    paragraph("包含知识笔记、练习题与答案、复习卡片。文字出处为原文摘录；图像出处为模型识别，应对照原始幻灯片复核。")
    sources = {source["id"]: source for source in result["sources"]}
    for title, key in (("知识笔记", "notes"), ("练习题与答案", "questions"), ("复习卡片", "cards")):
        if key == "questions":
            doc.add_page_break()
        paragraph(title, "Heading 1")
        for index, item in enumerate(result[key], 1):
            title_field = {"notes": "title", "questions": "question", "cards": "front"}[key]
            paragraph(f"{index}. {item[title_field]}", "Heading 2")
            if key == "notes":
                paragraph(item["content"])
            elif key == "questions":
                paragraph("答案：" + item["answer"])
                paragraph("解析：" + item["explanation"])
            else:
                paragraph(item["back"])
            paragraph("出处：" + citations_text(item, sources), "Quote")
    paragraph("审核与用量", "Heading 1")
    paragraph(result["audit"]["summary"])
    paragraph(f"审核剔除：{result['removed_count']}条。")
    for item in result["audit"]["issues"]:
        paragraph(f"{item['item_id']}：{item['reason']}")
    for warning in result["warnings"]:
        paragraph("提示：" + warning)
    paragraph("模型：" + result["model"] + "；用量：" + json.dumps(result["usage"], ensure_ascii=False))
    footer = section.footer.paragraphs[0]
    footer.alignment = 2
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()
