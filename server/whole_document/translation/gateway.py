"""One auditable HTTP layer. SDK retries cannot bypass this budget."""

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from .network_diagnostics import NetworkEvidence, opener


class GatewayError(RuntimeError):
    def __init__(self, category, status=None, request_id=None, diagnostics=None):
        super().__init__(category)
        self.category, self.status, self.request_id = category, status, request_id
        self.diagnostics = diagnostics or {}


def classify(status, body):
    text = body.lower()
    if status in (401, 403, 404) or any(
        s in text
        for s in (
            "no available channel",
            "no available channels",
            "无可用渠道",
            "无可用通道",
            "model_not_found",
            "insufficient_quota",
            "invalid_api_key",
            "无可用的渠道",
        )
    ):
        return "fatal"
    if status == 413 or any(s in text for s in ("context_length_exceeded", "maximum context length")):
        return "input_limit"
    return "transient" if status in (408, 429, 500, 502, 503, 504, 524) else "fatal"


class Circuit:
    def __init__(self, threshold=3):
        self.lock = threading.Lock()
        self.failures = 0
        self.open = False
        self.threshold = threshold

    def check(self):
        with self.lock:
            if self.open:
                raise GatewayError("circuit_open")

    def record(self, category):
        with self.lock:
            self.failures = self.failures + 1 if category else 0
            if category == "fatal" or self.failures >= self.threshold:
                self.open = True


class FileCircuit(Circuit):
    """Shared across serial worker processes; explicit operator reset after recovery."""

    def __init__(self, store, threshold=3):
        super().__init__(threshold)
        self.store = store

    def check(self):
        with self.store.locked():
            state = self.store.read() or {}
            if state.get("open"):
                raise GatewayError("circuit_open")

    def record(self, category):
        with self.store.locked():
            state = self.store.read() or {"failures": 0, "open": False}
            state["failures"] = state["failures"] + 1 if category else 0
            state["open"] = state["open"] or category == "fatal" or state["failures"] >= self.threshold
            self.store.save(state)


@dataclass
class Budget:
    max_requests: int = 12
    max_tokens: int = 200000
    requests: int = 0
    reserved_tokens: int = 0
    max_cost: float | None = None
    reserved_cost: float = 0

    def reserve(self, tokens, cost=0):
        if self.requests >= self.max_requests or self.reserved_tokens + tokens > self.max_tokens:
            raise GatewayError("budget_exhausted")
        if self.max_cost is not None and self.reserved_cost + cost > self.max_cost:
            raise GatewayError("budget_exhausted")
        self.requests += 1
        self.reserved_tokens += tokens
        self.reserved_cost += cost


def reservation_cost(config, input_tokens, output_tokens):
    if config.get("max_cost") is None:
        return 0
    return (
        input_tokens * config["input_price_per_million"] + output_tokens * config["output_price_per_million"]
    ) / 1000000


def post_json(url, headers, body, timeout, *, diagnostics=None, proxy_mode="system"):
    evidence = NetworkEvidence("urllib", url, proxy_mode, headers, body, diagnostics)
    data = json.dumps(body).encode()
    evidence.output["request_body_bytes"] = len(data)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with opener(evidence).open(req, timeout=timeout) as response:
            evidence.mark("receive_body.started")
            if body.get("stream") and "text/event-stream" in response.headers.get("Content-Type", ""):
                result = read_stream(response, evidence)
            else:
                result = json.loads(response.read())
            evidence.mark("receive_body.complete")
            return response.status, dict(response.headers), result
    except urllib.error.HTTPError as exc:
        # Never expose raw provider bodies: these may echo private prompts or credentials.
        data = exc.read(1048576).decode("utf-8", errors="replace")
        evidence.failure(exc)
        raise GatewayError(
            classify(exc.code, data),
            exc.code,
            exc.headers.get("x-request-id") or exc.headers.get("x-oneapi-request-id"),
            evidence.output,
        ) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        evidence.failure(exc)
        raise GatewayError("outcome_unknown", diagnostics=evidence.output) from None
    except GatewayError as exc:
        if not exc.diagnostics:
            evidence.failure(exc)
            exc.diagnostics = evidence.output
        raise
    finally:
        evidence.finish()


def read_stream(response, evidence=None):
    import httpx

    content, finish, usage, request_id = [], None, None, None
    done = False
    first_line = True
    try:
        for line in response:
            if evidence and first_line:
                evidence.mark("receive_body.first_line")
                first_line = False
            line = (line.decode("utf-8") if isinstance(line, bytes) else line).strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                if evidence:
                    evidence.output["sse_done_received"] = True
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise GatewayError(classify(503, json.dumps(chunk["error"])), 503)
            request_id = chunk.get("id") or request_id
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                if delta.get("refusal"):
                    raise GatewayError("refusal")
                finish = choice.get("finish_reason") or finish
    except (OSError, http.client.HTTPException, httpx.TransportError) as exc:
        if evidence:
            evidence.failure(exc)
        if not content:
            raise GatewayError("outcome_unknown", diagnostics=evidence.output if evidence else None) from None
    return {
        "id": request_id,
        "usage": usage,
        "_stream_incomplete": not done,
        "choices": [
            {"finish_reason": finish if done else "interrupted", "message": {"content": "".join(content)}}
        ],
    }


def post_json_httpx(url, headers, body, timeout, *, diagnostics=None, proxy_mode="system"):
    import httpx

    evidence = NetworkEvidence("httpx", url, proxy_mode, headers, body, diagnostics)
    evidence.output["request_body_bytes"] = len(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    )
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 20)),
            follow_redirects=False,
            trust_env=False,
            proxy=evidence.proxy,
        ) as client:
            with client.stream(
                "POST", url, headers=headers, json=body, extensions={"trace": evidence.httpx_trace}
            ) as response:
                evidence.headers(response.status_code, response.headers)
                evidence.mark("receive_body.started")
                if response.status_code != 200:
                    text = response.read().decode("utf-8", errors="replace")
                    raise GatewayError(
                        classify(response.status_code, text),
                        response.status_code,
                        response.headers.get("x-request-id") or response.headers.get("x-oneapi-request-id"),
                        evidence.output,
                    )
                if body.get("stream") and "text/event-stream" in response.headers.get("Content-Type", ""):
                    result = read_stream(response.iter_lines(), evidence)
                else:
                    result = json.loads(response.read())
                evidence.mark("receive_body.complete")
                return response.status_code, dict(response.headers), result
    except httpx.TransportError as exc:
        evidence.failure(exc)
        raise GatewayError("outcome_unknown", diagnostics=evidence.output) from None
    finally:
        evidence.finish()


class Gateway:
    def __init__(
        self,
        config,
        ledger,
        budget=None,
        circuit=None,
        transport=None,
        sleep=time.sleep,
        cancelled=lambda: False,
    ):
        self.config, self.ledger = config, ledger
        if config.get("max_cost") is not None:
            prices = [config.get("input_price_per_million"), config.get("output_price_per_million")]
            if any(not isinstance(p, (int, float)) or p < 0 for p in prices) or max(prices) <= 0:
                raise ValueError("Trusted channel prices required for a monetary budget")
        self.budget = budget or Budget(
            config["max_requests"], config["max_task_tokens"], max_cost=config.get("max_cost")
        )
        self.circuit = circuit or Circuit()
        self.transport = transport or (post_json_httpx if config.get("transport") == "httpx" else post_json)
        self.sleep, self.cancelled = sleep, cancelled

    def request(self, task_id, stage, batch, messages, input_tokens, output_tokens):
        cfg = self.config
        body = {"model": cfg["model"], "messages": messages, "max_tokens": output_tokens}
        if cfg.get("stream"):
            body.update(stream=True, stream_options={"include_usage": True})
        for attempt in range(3):
            if self.cancelled():
                raise GatewayError("cancelled")
            self.circuit.check()
            cost = reservation_cost(cfg, input_tokens, output_tokens)
            self.budget.reserve(input_tokens + output_tokens, cost)
            event = {
                "schema": 1,
                "time_utc": datetime.now(UTC).isoformat(),
                "task_id": task_id,
                "stage": stage,
                "batch": batch,
                "attempt": attempt + 1,
                "input_estimate": input_tokens,
                "output_limit": output_tokens,
                "usage": None,
                "cached_input": None,
                "finish_reason": None,
                "status_code": None,
                "request_id": None,
                "local_cache": False,
                "error": None,
            }
            start = time.monotonic()
            failure = None
            network = {}
            # Persist reservation before dispatch, including across process crashes/resume.
            self.ledger(
                {
                    **event,
                    "event": "dispatch",
                    "reserved_tokens": input_tokens + output_tokens,
                    "reserved_cost_cny": cost if cfg.get("currency", "CNY") == "CNY" else None,
                }
            )
            try:
                arguments = (
                    cfg["base_url"].rstrip("/") + "/chat/completions",
                    {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]},
                    body,
                    cfg["timeout_seconds"],
                )
                if self.transport in (post_json, post_json_httpx):
                    status, headers, result = self.transport(
                        *arguments, diagnostics=network, proxy_mode=cfg.get("proxy_mode", "system")
                    )
                else:
                    status, headers, result = self.transport(*arguments)
                if status != 200:
                    raise GatewayError(classify(status, json.dumps(result)), status)
                event["status_code"] = status
                normalized_headers = {k.lower(): v for k, v in headers.items()}
                event["request_id"] = (
                    normalized_headers.get("x-request-id")
                    or normalized_headers.get("x-oneapi-request-id")
                    or result.get("id")
                )
                event["usage"] = result.get("usage")
                if result.get("_stream_incomplete"):
                    event["error"] = "outcome_unknown"
                usage = event["usage"] or {}
                event["cached_input"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                choices = result.get("choices") or []
                if not choices:
                    raise GatewayError("invalid_response", status)
                choice = choices[0]
                event["finish_reason"] = choice.get("finish_reason")
                message = choice.get("message") or {}
                if message.get("refusal") or choice.get("finish_reason") in ("content_filter", "tool_calls"):
                    raise GatewayError("refusal", status)
                text = message.get("content")
                if not isinstance(text, str) or not text.strip():
                    raise GatewayError("empty_response", status)
                self.circuit.record(None)
                return text, choice.get("finish_reason")
            except GatewayError as exc:
                failure = exc
                event.update(error=exc.category, status_code=exc.status, request_id=exc.request_id)
                if exc.diagnostics:
                    network = exc.diagnostics
                if exc.category in ("transient", "outcome_unknown", "fatal"):
                    self.circuit.record(exc.category)
                if exc.category not in ("transient", "outcome_unknown") or attempt == 2:
                    raise
            except (ValueError, KeyError, TypeError):
                event["error"] = "invalid_response"
                raise GatewayError("invalid_response") from None
            finally:
                if network:
                    event["network"] = network
                event["elapsed_seconds"] = round(time.monotonic() - start, 3)
                self.ledger({**event, "event": "result"})
            if failure:
                self.sleep(min(2**attempt, 4))
