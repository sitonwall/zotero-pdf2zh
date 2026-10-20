import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
import pymupdf
from babeldoc.format.pdf.document_il import Box, PdfCharacter, PdfStyle
from babeldoc.format.pdf.document_il.il_version_1 import PdfParagraph
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting, TypesettingUnit


class LongIdentifierLayoutTest(unittest.TestCase):
    def test_long_model_identifier_wraps_horizontally_without_character_columns(self):
        engine = object.__new__(Typesetting)
        engine.font_mapper = SimpleNamespace(base_font=pymupdf.Font("helv"))
        text = "model sentence-transformers/distiluse-base-multilingual-cased-v2 parameters"
        units = [
            TypesettingUnit(char=PdfCharacter(
                char_unicode=c, box=Box(0, 0, 5, 10),
                pdf_style=PdfStyle(font_id="fixture", font_size=10),
            )) for c in text
        ]
        paragraph = PdfParagraph(box=Box(0, 0, 100, 1000))
        laid_out, fits = engine._layout_typesetting_units(
            units, paragraph.box, 1, 1.3, paragraph,
        )
        self.assertTrue(fits)
        self.assertEqual("".join(str(u) for u in laid_out).replace(" ", ""), text.replace(" ", ""))
        rows = {round(u.box.y, 3) for u in laid_out}
        self.assertLessEqual(len(rows), 6)
        self.assertTrue(all(u.box.x >= 0 and u.box.x2 <= 100 for u in laid_out))
