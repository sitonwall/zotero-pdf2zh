"""Replan a saved private IR without reparsing a PDF or calling a model."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from translation.document import Paragraph, capacity, plan
from translation.store import atomic_json


def main():
    p = argparse.ArgumentParser()
    p.add_argument("capture", type=Path)
    a = p.parse_args()
    d = json.loads(a.capture.read_text(encoding="utf-8"))
    paragraphs = [Paragraph(**x) for x in d["ir"]]
    batches = plan(paragraphs, d["config"])
    d["batches"] = [
        {
            "ids": [p.paragraph_id for p in b],
            "input_estimate": capacity(b, d["config"])[1],
            "output_reservation": capacity(b, d["config"])[2],
        }
        for b in batches
    ]
    atomic_json(a.capture, d)
    print(
        json.dumps(
            {
                "pages": d["pages"],
                "paragraphs": d["paragraphs"],
                "pending": d["pending"],
                "batches": [
                    {
                        "paragraphs": len(b["ids"]),
                        "input_estimate": b["input_estimate"],
                        "output_reservation": b["output_reservation"],
                    }
                    for b in d["batches"]
                ],
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
