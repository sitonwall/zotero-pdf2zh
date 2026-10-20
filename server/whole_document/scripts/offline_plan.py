"""Capture the real engine IR without making any model call or writing translated PDFs."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
from babeldoc.format.pdf.high_level import translate
from babeldoc.format.pdf.translation_config import TranslationConfig

from translation.document import capacity, plan
from translation.store import Store, atomic_json
from worker.adapter import DocumentAdapter, NoNetworkTranslator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", type=int, default=128000)
    parser.add_argument("--output-limit", type=int, default=64000)
    args = parser.parse_args()
    cfg = {
        "target_language": "zh-CN",
        "context_limit": args.context,
        "output_limit": args.output_limit,
        "max_task_tokens": 300000,
        "strategy": "whole",
    }
    store = Store(args.output, "capture")

    class Capture:
        def __init__(self):
            self.store = store
            self.config = cfg

        def run(self, paragraphs, resume=False):
            from dataclasses import asdict

            pending = [p for p in paragraphs if not p.preserve]
            batches = plan(paragraphs, cfg)
            result = {
                "mode": "offline_scenario_not_verified_channel_limits",
                "config": cfg,
                "pages": max((p.page for p in paragraphs), default=0),
                "paragraphs": len(paragraphs),
                "pending": len(pending),
                "preserved": len(paragraphs) - len(pending),
                "batches": [
                    {
                        "ids": [p.paragraph_id for p in b],
                        "input_estimate": capacity(b, cfg)[1],
                        "output_reservation": capacity(b, cfg)[2],
                    }
                    for b in batches
                ],
                "ir": [asdict(p) for p in paragraphs],
            }
            atomic_json(args.output / "plan.json", result)
            raise StopIteration("offline_plan_captured")

    engine = TranslationConfig(
        NoNetworkTranslator("zh-CN"),
        args.source,
        "en",
        "zh-CN",
        None,
        output_dir=args.output / "unused",
        working_dir=args.output / "layout",
        auto_extract_glossary=False,
        document_translator=DocumentAdapter(Capture()),
        use_rich_pbar=False,
    )
    try:
        translate(engine)
    except StopIteration:
        pass
    data = json.loads((args.output / "plan.json").read_text(encoding="utf-8"))
    print(json.dumps({k: v for k, v in data.items() if k not in ("ir", "batches")}, ensure_ascii=True))
    print("planned batches:", len(data["batches"]))


if __name__ == "__main__":
    main()
