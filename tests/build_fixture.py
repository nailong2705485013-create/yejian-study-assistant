"""Rebuild the synthetic two-slide fixture using only project dependencies.

Run from any directory: python tests/build_fixture.py
No user data, API connection, Office, or private artifact tooling is required.
"""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt


def main():
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = deck.slides.add_slide(deck.slide_layouts[6])

    def text(x, y, w, h, value, size=26):
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        paragraph = box.text_frame.paragraphs[0]
        paragraph.text = value
        paragraph.font.name = "Arial"
        paragraph.font.size = Pt(size)
        paragraph.font.color.rgb = RGBColor.from_string("263C35")

    text(.7, .5, 12, 1, "Reliable transport mechanisms", 34)
    for x, label in ((.7, "Sequence number"), (5.0, "Acknowledgement"), (9.3, "Retry")):
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(2.7), Inches(3.2), Inches(1.65))
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string("EAF0E8")
        shape.line.fill.background()
        text(x + .1, 3.15, 3, .9, label, 23)
    for x in (4.0, 8.3):
        arrow = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(3.2), Inches(.8), Inches(.5))
        arrow.fill.solid()
        arrow.fill.fore_color.rgb = RGBColor.from_string("315B49")
        arrow.line.fill.background()
    text(.7, 5.6, 12, 1, "Sequence numbers, acknowledgements and retries work together.", 24)

    picture = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.load_default(size=42)
    draw.text((80, 65), "Image only learning material", fill="#263c35", font=font)
    draw.rectangle((210, 360, 410, 540), fill="#315b49")
    draw.rectangle((680, 180, 880, 540), fill="#bc6c49")
    draw.text((240, 580), "A = 2", fill="#263c35", font=font)
    draw.text((720, 580), "B = 4", fill="#263c35", font=font)
    stream = io.BytesIO()
    picture.save(stream, format="PNG")
    stream.seek(0)
    image_slide = deck.slides.add_slide(deck.slide_layouts[6])
    image_slide.shapes.add_picture(stream, 0, 0, width=deck.slide_width, height=deck.slide_height)
    deck.core_properties.author = "Yejian contributors"
    deck.core_properties.title = "Synthetic transport and image-only test fixture"
    deck.core_properties.last_modified_by = "Yejian contributors"
    output = Path(__file__).resolve().parent / "fixtures" / "diagram.pptx"
    output.parent.mkdir(parents=True, exist_ok=True)
    deck.save(output)
    print("Wrote two synthetic slides to " + str(output))


if __name__ == "__main__":
    main()
