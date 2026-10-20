"""The only translation worker entry. Never invokes pdf2zh_next CLI."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from translation.coordinator import Coordinator, task_identity
from translation.document import fingerprint
from translation.gateway import FileCircuit, Gateway
from translation.store import Store, atomic_json


def validate_config(config):
    terms = config.get("preserve_terms", [])
    if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise ValueError("preserve_terms must be a list of nonempty exact names")
    for key in ("context_limit", "output_limit", "max_task_tokens", "max_requests", "timeout_seconds"):
        if not isinstance(config.get(key), (int, float)) or config[key] <= 0:
            raise ValueError("Missing positive channel setting: " + key)
    if not config.get("capacity_verified"):
        raise ValueError("Channel input/output capacity must be explicitly verified")
    if config.get("terminology_mode", "contextual") != "contextual":
        raise ValueError("Only contextual translation with an optional saved glossary is supported")


def pdf_qa(paths, source, alternating=False):
    import pymupdf

    failures, checks = [], []
    with pymupdf.open(source) as original:
        expected = len(original)
    for mode, path in paths.items():
        with pymupdf.open(path) as pdf:
            wanted = expected * (2 if mode == "dual" and alternating else 1)
            if pdf.is_encrypted or len(pdf) != wanted:
                failures.append(mode + ":page_count_or_encryption")
            pages = []
            for n, page in enumerate(pdf):
                blocks = page.get_text("dict")["blocks"]
                text = page.get_text()
                fonts = page.get_fonts()
                if "\ufffd" in text:
                    failures.append(f"{mode}:replacement_character:{n + 1}")
                for block in blocks:
                    for line in block.get("lines", []):
                        for span in line["spans"]:
                            x0, y0, x1, y1 = span["bbox"]
                            if x0 < -2 or y0 < -2 or x1 > page.rect.width + 2 or y1 > page.rect.height + 2:
                                failures.append(f"{mode}:text_out_of_bounds:{n + 1}")
                pages.append({"page": n + 1, "text_characters": len(text), "font_count": len(fonts)})
            checks.append({"mode": mode, "pages": pages, "expected_pages": wanted})
    if set(paths) != {"mono", "dual"}:
        failures.append("missing_output_mode")
    return {
        "passed": not failures,
        "failures": sorted(set(failures)),
        "checks": checks,
        "semantic_review": "human_sampling_required",
        "limits": "Bounding boxes and text extraction do not prove visual or semantic correctness.",
    }


def run_pdf(source, config, runs, resume=False, progress=None, gateway_factory=None):
    import hashlib

    task_id = task_identity(hashlib.sha256(Path(source).read_bytes()).hexdigest(), config)
    with Store(runs, task_id).locked("worker.lock"):
        return _run_pdf(source, config, runs, resume, progress, gateway_factory)


def _run_pdf(source, config, runs, resume=False, progress=None, gateway_factory=None):
    import hashlib

    from babeldoc.format.pdf.high_level import translate
    from babeldoc.format.pdf.translation_config import (
        TranslationConfig,
        WatermarkOutputMode,
    )

    from worker.adapter import DocumentAdapter, NoNetworkTranslator

    validate_config(config)
    source = Path(source)
    document_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    task_id = task_identity(document_hash, config)
    store = Store(runs, task_id)
    request_metadata = store.path / "request.json"
    original_name = (
        json.loads(request_metadata.read_text(encoding="utf-8")).get("fileName", source.name)
        if request_metadata.exists()
        else source.name
    )
    status_path = store.path / "status.json"
    old = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else None
    if old and old.get("finished") and not resume:
        return old
    started = time.monotonic()
    started_at = time.time()

    def update(state):
        payload = {
            "taskId": task_id,
            "fileName": original_name,
            "status": "running",
            "stage": state.get("stage", "parsing"),
            "progress": None,
            "validated": len(state.get("valid", {})),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "started_at": started_at,
            "finished": False,
            "fileList": [],
        }
        atomic_json(status_path, payload)
        if progress:
            progress(payload)

    update({})

    def cancelled():
        return (store.path / "cancel.request").exists()

    circuit = FileCircuit(
        Store(Path(runs).parent / "circuits", fingerprint([config["base_url"], config["model"]]))
    )
    gateway = (
        gateway_factory or (lambda: Gateway(config, store.ledger, circuit=circuit, cancelled=cancelled))
    )()
    coordinator = Coordinator(config, store, gateway, update)
    adapter = DocumentAdapter(coordinator, resume)
    try:
        cfg = TranslationConfig(
            NoNetworkTranslator(config["target_language"]),
            source,
            "en",
            config["target_language"],
            None,
            output_dir=store.path / "pdf",
            working_dir=store.path / "layout",
            auto_extract_glossary=False,
            document_translator=adapter,
            split_strategy=None,
            use_rich_pbar=False,
            min_text_length=1,
            use_alternating_pages_dual=True,
            watermark_output_mode=WatermarkOutputMode.NoWatermark,
        )
        result = translate(cfg)
        state = adapter.state
        update(dict(state, stage="pdf_qa"))
        paths = {
            mode: Path(getattr(result, mode + "_pdf_path"))
            for mode in ("mono", "dual")
            if getattr(result, mode + "_pdf_path", None)
        }
        qa = pdf_qa(paths, source, alternating=True)
        if not qa["passed"]:
            state["errors"].extend(qa["failures"])
            state["status"] = "failed"
        final_paths = {}
        for mode, path in paths.items():
            prefix = "PARTIAL-" if state["status"] == "partial" else ""
            destination = path.with_name(
                prefix + task_id[:12] + "-" + Path(original_name).stem + "." + mode + ".pdf"
            )
            if path != destination:
                path.replace(destination)
            final_paths[mode] = str(destination)
        atomic_json(
            store.path / "qa.json",
            {
                "pdf": qa,
                "invalid": state["invalid"],
                "errors": state["errors"],
                "ir": state["ir"],
                "items": [
                    {
                        "paragraph_id": p["paragraph_id"],
                        "page": p["page"],
                        "reason": state["invalid"][p["paragraph_id"]],
                    }
                    for p in state["ir"]
                    if p["paragraph_id"] in state["invalid"]
                ],
            },
        )
        store.save(state)
        status = state["status"]
        payload = {
            "taskId": task_id,
            "fileName": original_name,
            "status": status,
            "finished": True,
            "stage": "finished",
            "progress": 100 if status == "complete" else None,
            "fileList": [Path(p).name for p in final_paths.values()]
            if status in ("complete", "partial")
            else [],
            "filePaths": list(final_paths.values()),
            "outputDir": str(store.path / "pdf"),
            "qa": "qa.json",
            "errors": state["errors"],
            "invalid": state["invalid"],
        }
    except Exception:  # noqa: BLE001 -- Worker boundary always persists a terminal status.
        state = store.read() or {}
        payload = {
            "taskId": task_id,
            "fileName": original_name,
            "status": "cancelled" if cancelled() else "failed",
            "finished": True,
            "stage": "finished",
            "progress": None,
            "fileList": [],
            "errors": state.get("errors") or ["worker_error"],
        }
        # Local private diagnostic; never include raw errors in client or request ledger.
        import traceback

        (store.path / "worker-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    payload["elapsed_seconds"] = round(time.monotonic() - started, 1)
    atomic_json(status_path, payload)
    if progress:
        progress(payload)
    return payload


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path, nargs="?")
    p.add_argument("--config", type=Path)
    p.add_argument("--runs", type=Path)
    p.add_argument("--check", action="store_true")
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    if args.check:
        import inspect

        from babeldoc.format.pdf.high_level import _translate_document
        from babeldoc.format.pdf.translation_config import TranslationConfig

        assert "document_translator" in inspect.signature(TranslationConfig).parameters
        print(
            json.dumps(
                {
                    "python": sys.version.split()[0],
                    "engine_source": inspect.getfile(_translate_document),
                    "worker": "whole-document-v1",
                    "model_calls": 0,
                }
            )
        )
        sys.exit(0)
    if not args.source or not args.config or not args.runs:
        p.error("source, --config and --runs are required for translation")
    output = run_pdf(args.source, json.loads(args.config.read_text(encoding="utf-8")), args.runs, args.resume)
    print(json.dumps(output, ensure_ascii=True))
    sys.exit(0 if output["status"] == "complete" else 2)
