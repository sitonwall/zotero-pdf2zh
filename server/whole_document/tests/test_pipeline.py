import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
from translation.coordinator import Coordinator, task_identity
from translation.document import Paragraph, capacity, plan, validate
from translation.gateway import Circuit, Gateway, GatewayError, classify
from translation.store import Store


def config(**overrides):
    return dict(
        {
            "base_url": "https://example.invalid/v1",
            "api_key": "local-fixture",
            "model": "fixture",
            "target_language": "zh-CN",
            "context_limit": 128000,
            "output_limit": 32000,
            "max_requests": 12,
            "max_task_tokens": 300000,
            "timeout_seconds": 10,
            "capacity_verified": True,
        },
        **overrides,
    )


def response(items, finish="stop"):
    return (
        200,
        {"x-request-id": "test-request"},
        {
            "choices": [
                {"finish_reason": finish, "message": {"content": json.dumps(items, ensure_ascii=False)}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    )


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ps = [
            Paragraph.create(1, i, "intro", "The system translates scientific documents correctly. " * 10)
            for i in range(10)
        ]
        self.calls = []

    def run_case(self, transport, cfg=None, resume=False):
        cfg = cfg or config()
        store = Store(self.tmp.name, task_identity("fixture-document", cfg))

        def counted(*args):
            self.calls.append(args)
            return transport(*args)

        gateway = Gateway(cfg, store.ledger, transport=counted, sleep=lambda _: None)
        return Coordinator(cfg, store, gateway).run(self.ps, resume), store

    def good(self, url, headers, body, timeout):
        inputs = json.loads(body["messages"][1]["content"])
        return response(
            [
                {"paragraph_id": p["paragraph_id"], "translated_text": "系统正确翻译科学文档。" * 20}
                for p in inputs
            ]
        )

    def test_whole_once_and_validated_cache_no_calls(self):
        state, store = self.run_case(self.good)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(self.calls), 1)
        again, _ = self.run_case(self.good)
        self.assertEqual(again["valid"], state["valid"])
        self.assertEqual(len(self.calls), 1)
        events = store.events()
        self.assertEqual([e["event"] for e in events], ["dispatch", "result"])
        self.assertEqual(events[1]["usage"]["total_tokens"], 30)
        self.assertNotIn("local-fixture", (store.path / "ledger.jsonl").read_text())

    def test_no_channel_stops_no_fallback(self):
        def error(*args):
            raise GatewayError("fatal", 503)

        state, _ = self.run_case(error)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(classify(503, "No available channel for this model"), "fatal")

    def test_transient_maximum_three_attempts(self):
        def error(*args):
            raise GatewayError("transient", 429)

        state, store = self.run_case(error)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len([e for e in store.events() if e["event"] == "dispatch"]), 3)

    def test_timeouts_are_unknown_not_zero_cost(self):
        def error(*args):
            raise GatewayError("outcome_unknown")

        state, store = self.run_case(error)
        self.assertEqual(len(self.calls), 3)
        self.assertIsNone(store.events()[1]["usage"])
        self.assertIn("outcome_unknown", state["errors"])

    def test_shared_fatal_circuit(self):
        circuit = Circuit()
        circuit.record("fatal")
        with self.assertRaisesRegex(GatewayError, "circuit_open"):
            circuit.check()

    def test_budget_counts_attempts(self):
        def error(*args):
            raise GatewayError("transient", 429)

        state, _ = self.run_case(error, config(max_requests=1))
        self.assertEqual(len(self.calls), 1)
        self.assertIn("budget_exhausted", state["errors"])

    def test_money_cap_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Trusted channel prices"):
            self.run_case(self.good, config(max_cost=0.2))
        state, _ = self.run_case(
            self.good, config(max_cost=0.000001, input_price_per_million=2, output_price_per_million=10)
        )
        self.assertEqual(len(self.calls), 0)
        self.assertIn("budget_exhausted", state["errors"])

    def test_repair_only_invalid_twice_and_twenty_percent(self):
        bad = self.ps[0].paragraph_id

        def transport(url, headers, body, timeout):
            inputs = json.loads(body["messages"][1]["content"])
            return response(
                [
                    {
                        "paragraph_id": p["paragraph_id"],
                        "translated_text": p["source"] if p["paragraph_id"] == bad else "科学文档译文。" * 30,
                    }
                    for p in inputs
                ]
            )

        state, _ = self.run_case(transport)
        self.assertEqual(state["status"], "partial")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(state["repair_counts"], {bad: 2})
        self.assertLessEqual(state["repair_source"], sum(len(p.source.encode()) for p in self.ps) * 0.2)
        for call in self.calls[1:]:
            self.assertEqual(
                [p["paragraph_id"] for p in json.loads(call[2]["messages"][1]["content"])], [bad]
            )

    def test_corrupt_all_no_full_retranslation(self):
        state, _ = self.run_case(lambda *args: response([]))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state["status"], "failed")

    def test_repair_success(self):
        def transport(*args):
            result = self.good(*args)
            if len(self.calls) == 1:
                body = json.loads(result[2]["choices"][0]["message"]["content"])
                return response(body[1:])
            return result

        state, _ = self.run_case(transport)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(self.calls), 2)

    def test_duplicate_missing_extra_and_formula(self):
        p = Paragraph.create(1, 0, "s", "This formula {v1} is important.")
        for items, reason in [
            ([], "missing_id"),
            ([{"paragraph_id": p.paragraph_id, "translated_text": "公式"}] * 2, "duplicate_id"),
            ([{"paragraph_id": p.paragraph_id, "translated_text": "公式"}], "protected_tokens_changed"),
        ]:
            valid, invalid, _ = validate([p], json.dumps(items))
            self.assertEqual(invalid[p.paragraph_id], reason)
            self.assertFalse(valid)
        valid, invalid, errors = validate(
            [p],
            json.dumps(
                [
                    {"paragraph_id": p.paragraph_id, "translated_text": "公式 {v1} 很重要。"},
                    {"paragraph_id": "unknown", "translated_text": "错误"},
                ]
            ),
        )
        self.assertIn("extra_ids", errors)
        self.assertTrue(valid)

    def test_truncated_json_retains_complete_items(self):
        p = self.ps[0]
        text = (
            json.dumps([{"paragraph_id": p.paragraph_id, "translated_text": "科学文档。" * 30}])[:-1]
            + ', {"paragraph_id":'
        )
        valid, invalid, errors = validate(self.ps, text)
        self.assertIn(p.paragraph_id, valid)
        self.assertEqual(len(invalid), 9)
        self.assertIn("truncated_or_invalid_json", errors)

    def test_reverse_order_mapping(self):
        valid, invalid, errors = validate(
            self.ps,
            json.dumps(
                [
                    {"paragraph_id": p.paragraph_id, "translated_text": "科学文档。" * 30}
                    for p in reversed(self.ps)
                ]
            ),
        )
        self.assertEqual(len(valid), 10)
        self.assertFalse(invalid or errors)

    def test_sections_and_impossible_paragraph(self):
        a = Paragraph.create(1, 0, "a", "Source paragraph. " * 300)
        b = Paragraph.create(2, 0, "b", "Another paragraph. " * 300)
        cfg = config()
        _, inp, out = capacity([a], cfg)
        cfg.update(output_limit=out + 5, context_limit=inp + out + 2200)
        self.assertEqual(len(plan([a, b], cfg)), 2)
        with self.assertRaises(ValueError):
            plan([a], config(output_limit=20))

    def test_explicit_chapter_strategy_groups_to_a_smaller_envelope(self):
        paragraphs = [Paragraph.create(i + 1, 0, str(i), "Scientific source. " * 300) for i in range(4)]
        self.assertEqual(len(plan(paragraphs, config())), 1)
        self.assertGreater(len(plan(paragraphs, config(strategy="chapters", chapter_input_target=1000))), 1)

    def test_cancel_no_network_and_explicit_resume(self):
        cfg = config()
        store = Store(self.tmp.name, task_identity("cancel", cfg))
        gw = Gateway(cfg, store.ledger, transport=self.good, cancelled=lambda: True)
        state = Coordinator(cfg, store, gw).run(self.ps)
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(store.events())
        # Ordinary reopen does not retry a cancelled task.
        again = Coordinator(cfg, store, gw).run(self.ps)
        self.assertEqual(again["status"], "cancelled")

    def test_context_identity_changes(self):
        for c in [
            config(target_language="ja"),
            config(model="other"),
            config(glossary={"method": "方法"}),
            config(strategy="chapters"),
            config(base_url="https://another.invalid/v1"),
        ]:
            self.assertNotEqual(task_identity("doc", config()), task_identity("doc", c))

    def test_resume_does_not_reset_http_budget(self):
        def error(*args):
            raise GatewayError("transient", 429)

        self.run_case(error, config(max_requests=1))
        again, _ = self.run_case(self.good, config(max_requests=1), resume=True)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(again["status"], "complete")

    def test_real_engine_seam_and_reinjection(self):
        from babeldoc.format.pdf.document_il.il_version_1 import (
            Box,
            Document,
            Page,
            PdfCharacter,
            PdfLine,
            PdfParagraph,
            PdfParagraphComposition,
            PdfStyle,
        )
        from babeldoc.format.pdf.high_level import _translate_document
        from babeldoc.format.pdf.translation_config import TranslationConfig

        from worker.adapter import DocumentAdapter, NoNetworkTranslator

        cfg = config(preserve_terms=["TEDS"])
        store = Store(self.tmp.name, "actual-il")
        gateway = Gateway(cfg, store.ledger, transport=self.good, sleep=lambda _: None)
        adapter = DocumentAdapter(Coordinator(cfg, store, gateway))
        engine_cfg = TranslationConfig(
            NoNetworkTranslator("zh-CN"),
            Path(self.tmp.name) / "fixture.pdf",
            "en",
            "zh-CN",
            object(),
            working_dir=Path(self.tmp.name) / "layout",
            output_dir=Path(self.tmp.name) / "pdf",
            document_translator=adapter,
            auto_extract_glossary=False,
        )
        source = "This is a paragraph with formula {v1}."
        paragraph = PdfParagraph(
            unicode=source,
            pdf_style=PdfStyle(font_id="fixture", font_size=10),
            box=Box(0, 0, 100, 20),
            pdf_paragraph_composition=[
                PdfParagraphComposition(
                    pdf_line=PdfLine(pdf_character=[PdfCharacter(char_unicode=c) for c in source])
                )
            ],
        )
        label = PdfParagraph(
            unicode="TEDS",
            pdf_style=PdfStyle(font_id="fixture", font_size=10),
            box=Box(0, 30, 100, 50),
            pdf_paragraph_composition=[
                PdfParagraphComposition(
                    pdf_line=PdfLine(pdf_character=[PdfCharacter(char_unicode=c) for c in "TEDS"])
                )
            ],
        )
        docs = Document(page=[Page(pdf_paragraph=[paragraph, label])])

        def formula_good(url, headers, body, timeout):
            inputs = json.loads(body["messages"][1]["content"])
            self.assertNotIn("TEDS", [p["source"] for p in inputs])
            return response(
                [
                    {"paragraph_id": p["paragraph_id"], "translated_text": "这是公式 {v1} 的段落。"}
                    for p in inputs
                ]
            )

        gateway.transport = formula_good
        with (
            patch("babeldoc.format.pdf.high_level.ILTranslator", side_effect=AssertionError("legacy called")),
            patch(
                "babeldoc.format.pdf.high_level.ILTranslatorLLMOnly",
                side_effect=AssertionError("legacy called"),
            ),
        ):
            _translate_document(docs, engine_cfg)
        self.assertEqual(adapter.state["status"], "complete")
        self.assertEqual(paragraph.unicode, "这是公式 {v1} 的段落。")
        self.assertTrue(paragraph.pdf_paragraph_composition)
        self.assertEqual(label.unicode, "TEDS")
        from worker.adapter import preserve_content

        self.assertFalse(preserve_content("TEDS evaluates table structure.", "text", ["TEDS"]))


if __name__ == "__main__":
    unittest.main()
