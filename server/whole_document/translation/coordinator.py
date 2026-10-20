import math
from dataclasses import asdict

from .document import POLICY_VERSION, capacity, fingerprint, messages, plan, validate
from .gateway import Budget, GatewayError, reservation_cost


def task_identity(document_hash, config):
    # Endpoint, glossary and full channel/planning settings are part of context identity.
    public = {k: v for k, v in config.items() if k not in ("api_key", "runs_dir")}
    return fingerprint({"document": document_hash, "policy": POLICY_VERSION, "config": public})


class Coordinator:
    def __init__(self, config, store, gateway, progress=lambda state: None):
        self.config, self.store, self.gateway, self.progress = config, store, gateway, progress

    def run(self, paragraphs, resume=False):
        with self.store.locked():
            return self._run(paragraphs, resume)

    def _run(self, paragraphs, resume):
        cfg = self.config
        signature = fingerprint([asdict(p) for p in paragraphs])
        state = self.store.read()
        if state:
            if state["ir_signature"] != signature:
                raise ValueError("checkpoint_context_mismatch")
            if state["status"] == "complete" or not resume:
                return state
        else:
            batches = plan(paragraphs, cfg)
            state = {
                "schema": 1,
                "ir_signature": signature,
                "ir": [asdict(p) for p in paragraphs],
                "plan": [[p.paragraph_id for p in b] for b in batches],
                "initial_dispatched": [],
                "valid": {},
                "invalid": {},
                "errors": [],
                "repair_counts": {},
                "repair_source": 0,
                "repair_round": 0,
                "status": "running",
                "stage": "planned",
            }
            self.store.save(state)
        # Revalidate persisted translations before reuse; checkpoint is not a trust boundary.
        by_id = {p.paragraph_id: p for p in paragraphs if not p.preserve}
        for key, value in list(state["valid"].items()):
            if key not in by_id:
                del state["valid"][key]
                continue
            import json

            valid, _, _ = validate(
                [by_id[key]],
                json.dumps([{"paragraph_id": key, "translated_text": value}]),
                cfg["target_language"],
            )
            if not valid:
                del state["valid"][key]
        dispatches = [e for e in self.store.events() if e.get("event") == "dispatch"]
        self.gateway.budget = Budget(
            cfg["max_requests"],
            self.gateway.config["max_task_tokens"],
            len(dispatches),
            sum(e["reserved_tokens"] for e in dispatches),
            cfg.get("max_cost"),
            sum(
                e.get("reserved_cost_cny") or reservation_cost(cfg, e["input_estimate"], e["output_limit"])
                for e in dispatches
            ),
        )
        try:
            for index, ids in enumerate(state["plan"]):
                if index in state["initial_dispatched"]:
                    # A cancellation before dispatch consumed no request; explicit resume is safe.
                    dispatched = any(
                        e.get("stage") == "initial" and e.get("batch") == index for e in dispatches
                    )
                    if dispatched or all(k in state["valid"] for k in ids):
                        continue
                # Record before HTTP. Unknown outcomes require explicit bounded repair, never full replay.
                state["initial_dispatched"].append(index)
                self.store.save(state)
                self._translate([by_id[k] for k in ids if k not in state["valid"]], state, "initial", index)
            total_source = sum(len(p.source.encode()) for p in by_id.values())
            max_source = math.floor(total_source * cfg.get("repair_fraction", 0.20))
            for round_index in range(state["repair_round"], min(cfg.get("repair_rounds", 2), 2)):
                invalid = [p for key, p in by_id.items() if key not in state["valid"]]
                if not invalid:
                    break
                repair = [p for p in invalid if state["repair_counts"].get(p.paragraph_id, 0) < 2]
                required = sum(len(p.source.encode()) for p in repair)
                if not repair or state["repair_source"] + required > max_source:
                    state["errors"].append("repair_source_budget_exhausted")
                    break
                state["repair_round"] = round_index + 1
                # Reserve the whole round before sending so cancellation cannot reset its budget.
                state["repair_source"] += required
                for p in repair:
                    state["repair_counts"][p.paragraph_id] = state["repair_counts"].get(p.paragraph_id, 0) + 1
                self.store.save(state)
                for index, batch in enumerate(plan(repair, cfg)):
                    self._translate(batch, state, "repair", f"{round_index + 1}-{index}")
            missing = {
                key: state["invalid"].get(key, "missing_translation")
                for key in by_id
                if key not in state["valid"]
            }
            state["invalid"] = missing
            state["status"] = (
                "complete"
                if not missing and not state["errors"]
                else "partial"
                if state["valid"]
                else "failed"
            )
        except GatewayError as exc:
            # Preserve the underlying uncertain/fatal outcome when a subsequent retry is budget-blocked.
            for event in self.store.events():
                if (
                    event.get("event") == "result"
                    and event.get("error")
                    and event["error"] not in state["errors"]
                ):
                    state["errors"].append(event["error"])
            state["errors"].append(exc.category)
            state["status"] = (
                "cancelled" if exc.category == "cancelled" else "partial" if state["valid"] else "failed"
            )
        except Exception:
            state["errors"].append("pipeline_error")
            state["status"] = "failed"
            self.store.save(state)
            raise
        finally:
            state["invalid"] = {
                key: state["invalid"].get(key, "missing_translation")
                for key in by_id
                if key not in state["valid"]
            }
            state["stage"] = "translation_validated"
            self.store.save(state)
            self.progress(state)
        return state

    def _translate(self, batch, state, stage, index):
        if not batch:
            return
        state["stage"] = stage
        self.progress(state)
        fits, inp, out = capacity(batch, self.config)
        if not fits:
            raise GatewayError("input_limit")
        text, finish = self.gateway.request(
            self.store.path.name,
            stage,
            index,
            messages(batch, self.config["target_language"], self.config.get("glossary")),
            inp,
            out,
        )
        valid, invalid, errors = validate(batch, text, self.config["target_language"])
        state["valid"].update(valid)
        state["invalid"].update(invalid)
        # A truncated response with missing entries is resolved by bounded repair.
        # Other protocol anomalies remain visible and prevent complete status.
        for error in errors:
            if error != "truncated_or_invalid_json" or not invalid:
                state["errors"].append(error)
        if finish != "stop" and not invalid:
            state["errors"].append("non_stop_finish_reason")
        self.store.save(state)
