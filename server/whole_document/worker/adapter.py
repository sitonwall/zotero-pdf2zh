import re
from dataclasses import asdict

from babeldoc.format.pdf.document_il.midend.il_translator import (
    ILTranslator,
    ParagraphTranslateTracker,
)
from babeldoc.translator.translator import BaseTranslator, OpenAITranslator

from translation.document import Paragraph


class NoNetworkTranslator(BaseTranslator):
    """Only supplies engine placeholder syntax. Any legacy translation is a hard error."""

    name = "whole_document"
    model = "external_gateway"

    def __init__(self, lang_out):
        self.lang_in, self.lang_out = "en", lang_out
        self.translate_call_count = self.translate_cache_call_count = 0

    def do_llm_translate(self, text, rate_limit_params=None):
        if text is None:  # Upstream local capability check, never an HTTP request.
            return
        raise RuntimeError("legacy_translation_forbidden")

    def do_translate(self, text, rate_limit_params=None):
        raise RuntimeError("legacy_translation_forbidden")

    def translate(self, *args, **kwargs):
        raise RuntimeError("legacy_translation_forbidden")

    def llm_translate(self, *args, **kwargs):
        raise RuntimeError("legacy_translation_forbidden")

    get_formular_placeholder = OpenAITranslator.get_formular_placeholder
    get_rich_text_left_placeholder = OpenAITranslator.get_rich_text_left_placeholder
    get_rich_text_right_placeholder = OpenAITranslator.get_rich_text_right_placeholder


def preserve_content(text, label, preserve_terms=()):
    # Retain numeric labels, code, and bibliographic entries without model requests.
    # Exact operator-reviewed names only; a sentence mentioning a name still translates.
    if text.strip() in preserve_terms:
        return True
    if label in ("code", "reference", "references"):
        return True
    return not bool(re.search("[A-Za-z\u3400-\u9fff]", text))


class DocumentAdapter:
    def __init__(self, coordinator, resume=False):
        self.coordinator, self.resume = coordinator, resume
        self.state = None

    def __call__(self, docs, config):
        if config.split_strategy is not None:
            raise ValueError("Engine page splitting would break whole-document context")
        il = ILTranslator(config.translator, config)
        il.docs = docs
        paragraphs, bindings = [], {}
        section = "document"
        for page_index, page in enumerate(docs.page):
            fonts = {f.font_id: f for f in page.pdf_font}
            xfonts = {x.xobj_id: {**fonts, **{f.font_id: f for f in x.pdf_font}} for x in page.pdf_xobject}
            for order, paragraph in enumerate(page.pdf_paragraph):
                config.raise_if_cancelled()
                if paragraph.layout_label in ("title", "section_header", "section_title"):
                    section = paragraph.unicode
                tracker = ParagraphTranslateTracker()
                # Direct extraction bypasses upstream's min_text_length omission of short labels.
                if paragraph.vertical:
                    source, prepared = paragraph.unicode, None
                else:
                    prepared = il.get_translate_input(
                        paragraph, xfonts.get(paragraph.xobj_id, fonts), config.disable_rich_text_translate
                    )
                    source = prepared.unicode if prepared else paragraph.unicode
                if not source or not source.strip():
                    continue
                pure_formula = bool(paragraph.pdf_paragraph_composition) and all(
                    c.pdf_formula is not None for c in paragraph.pdf_paragraph_composition
                )
                preserve = pure_formula or preserve_content(
                    source, paragraph.layout_label, self.coordinator.config.get("preserve_terms", ())
                )
                layout = {
                    "xobj_id": paragraph.xobj_id,
                    "box": asdict(paragraph.box) if paragraph.box else None,
                    "style": asdict(paragraph.pdf_style) if paragraph.pdf_style else None,
                    "protected_tokens": re.findall(r"\{v\d+\}|<style\s+id='\d+'>|</style>", source),
                }
                p = Paragraph.create(page_index + 1, order, section, source, preserve, layout)
                paragraphs.append(p)
                bindings[p.paragraph_id] = (paragraph, tracker, prepared)
        self.state = self.coordinator.run(paragraphs, self.resume)
        self.state["unmappable"] = []
        for p in paragraphs:
            if p.preserve or p.paragraph_id not in self.state["valid"]:
                continue
            paragraph, tracker, prepared = bindings[p.paragraph_id]
            if prepared is None:
                self.state["unmappable"].append(p.paragraph_id)
                continue
            il.post_translate_paragraph(paragraph, tracker, prepared, self.state["valid"][p.paragraph_id])
        if self.state["unmappable"]:
            self.state["errors"].append("unmappable_layout")
            self.state["status"] = "partial" if self.state["valid"] else "failed"
        self.coordinator.store.save(self.state)
        if self.state["status"] in ("failed", "cancelled"):
            raise RuntimeError(self.state["status"])
