"""Offline integration: real PDF parsing, IL, reinjection and both output renderers."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
import pymupdf

from translation.coordinator import task_identity
from translation.gateway import Gateway
from translation.store import Store
from worker.run import run_pdf


def main():
    runs = Path(sys.argv[1])
    runs.mkdir(parents=True, exist_ok=True)
    source = runs / "synthetic-source.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text((50, 65), "Scientific translation integration fixture", fontsize=18)
    page.insert_textbox(
        pymupdf.Rect(50, 100, 545, 215),
        "1 Introduction\nThis document tests the complete PDF pipeline. "
        "Translation must retain all formulas and headings. The scientific method "
        "compares a baseline and a proposed system. [1]",
        fontsize=12,
    )
    page.insert_text((50, 260), "Accuracy = correct / total", fontsize=12)
    page.insert_text((50, 310), "2 Results", fontsize=16)
    page.insert_text((50, 345), "Method                  Accuracy", fontsize=12)
    page.insert_text((50, 370), "Baseline                   0.75", fontsize=12)
    page.insert_text((50, 395), "Proposed                   0.91", fontsize=12)
    page.insert_textbox(
        pymupdf.Rect(50, 450, 545, 550),
        "The proposed method improves accuracy while retaining mathematical expressions. "
        "This is synthetic fixture content for offline layout verification only.",
        fontsize=12,
    )
    pdf.save(source)
    pdf.close()
    cfg = {
        "base_url": "https://fixture.invalid/v1",
        "api_key": "fixture",
        "model": "synthetic-v1",
        "target_language": "zh-CN",
        "context_limit": 128000,
        "output_limit": 32000,
        "max_requests": 4,
        "max_task_tokens": 100000,
        "timeout_seconds": 30,
        "capacity_verified": True,
    }
    calls = []
    task_id = task_identity(__import__("hashlib").sha256(source.read_bytes()).hexdigest(), cfg)
    store = Store(runs, task_id)

    def transport(url, headers, body, timeout):
        items = json.loads(body["messages"][1]["content"])
        calls.append(len(items))
        # Clearly synthetic prose, preserving the engine's exact formula/style tokens.
        result = []
        from translation.document import TOKEN_RE

        for p in items:
            tokens = TOKEN_RE.findall(p["source"])
            value = "科学翻译测试：保留公式、标题与实验结果。" + "".join(tokens)
            result.append({"paragraph_id": p["paragraph_id"], "translated_text": value})
        return (
            200,
            {},
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)}}
                ]
            },
        )

    output = run_pdf(
        source, cfg, runs, gateway_factory=lambda: Gateway(cfg, store.ledger, transport=transport)
    )
    output["fixture_calls"] = calls
    (runs / "synthetic-result.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=True))
    assert output["status"] == "complete", output
    assert len(calls) == 1, calls
    for path in output["filePaths"]:
        pdf = pymupdf.open(path)
        for n, page in enumerate(pdf):
            page.get_pixmap(matrix=pymupdf.Matrix(1, 1)).save(runs / (Path(path).stem + f"-{n + 1}.png"))
        pdf.close()


if __name__ == "__main__":
    main()
