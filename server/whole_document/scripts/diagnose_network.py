"""Read-only provider diagnosis. This entry point cannot submit a model request."""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from translation.network_diagnostics import NetworkEvidence, opener, safe_proxy
from translation.store import atomic_json

ALLOWED_GET_PATHS = {"/api/status", "/v1/models", "/api/log/token", "/api/usage/token/"}


def get_probe(base_url, path, headers, transport, mode, timeout=20):
    if urllib.parse.urlsplit(path).path not in ALLOWED_GET_PATHS:
        raise ValueError("Only read-only diagnostic endpoints are permitted")
    url = base_url.rstrip("/") + path
    output = {}
    evidence = NetworkEvidence(transport, url, mode, headers, {}, output)
    parsed = None
    try:
        if transport == "urllib":
            try:
                response = opener(evidence).open(
                    urllib.request.Request(url, headers=headers, method="GET"), timeout=timeout
                )
            except urllib.error.HTTPError as error:
                response = error
            with response:
                evidence.mark("receive_body.started")
                parsed = json.loads(response.read(5000000))
                evidence.mark("receive_body.complete")
        else:
            import httpx

            with httpx.Client(
                timeout=timeout, trust_env=False, proxy=evidence.proxy, follow_redirects=False
            ) as client:
                with client.stream(
                    "GET", url, headers=headers, extensions={"trace": evidence.httpx_trace}
                ) as response:
                    evidence.headers(response.status_code, response.headers)
                    evidence.mark("receive_body.started")
                    parsed = json.loads(response.read())
                    evidence.mark("receive_body.complete")
    except (OSError, ValueError) as error:
        evidence.failure(error)
    except Exception as error:  # Library-specific transport exceptions, recorded without raw request data.
        evidence.failure(error)
    finally:
        evidence.finish()
    return output, parsed


def relevant_logs(parsed, start, end, model):
    data = (parsed or {}).get("data")
    rows = data if isinstance(data, list) else (data or {}).get("items", []) if isinstance(data, dict) else []
    keep = []
    for row in rows:
        if row.get("model_name") != model or not start <= row.get("created_at", 0) <= end:
            continue
        other = row.get("other") or {}
        if isinstance(other, str):
            try:
                other = json.loads(other)
            except ValueError:
                other = {}
        item = {
            k: v
            for k, v in row.items()
            if k
            in (
                "created_at",
                "model_name",
                "type",
                "prompt_tokens",
                "completion_tokens",
                "quota",
                "use_time",
                "request_id",
                "is_stream",
            )
        }
        item["created_at_utc"] = datetime.fromtimestamp(row["created_at"], UTC).isoformat()
        item["provider_error"] = {
            k: v for k, v in other.items() if k in ("error_type", "error_code", "status_code", "request_path")
        }
        keep.append(item)
    return {
        "returned_total": len(rows),
        "matching_window": len(keep),
        "records": keep,
        "warning": "Provider may ignore server-side time filters; selection is applied locally. No matching log is not proof of no execution.",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    config = json.loads(a.config.read_text(encoding="utf-8"))
    origin = urllib.parse.urlsplit(config["base_url"])
    base_url = urllib.parse.urlunsplit((origin.scheme, origin.netloc, "", "", ""))
    proxy_environment = {}
    for k, v in os.environ.items():
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            proxy_environment[k] = safe_proxy(v)
    report = {
        "schema": 1,
        "time_utc": datetime.now(UTC).isoformat(),
        "paid_requests": 0,
        "environment_proxies": proxy_environment,
        "system_proxies": {k: safe_proxy(v) for k, v in urllib.request.getproxies().items() if k != "no"},
        "dns_addresses": sorted(
            {x[4][0] for x in socket.getaddrinfo(origin.hostname, 443, type=socket.SOCK_STREAM)}
        ),
        "read_only_probes": [],
    }
    # Equal methods, endpoints and timeout; route and HTTP implementation are explicit variables.
    for mode in ("direct", "system"):
        for transport in ("urllib", "httpx"):
            evidence, data = get_probe(base_url, "/api/status", {}, transport, mode)
            report["read_only_probes"].append(evidence)
    headers = {"Authorization": "Bearer " + config["api_key"]}
    evidence, models = get_probe(base_url, "/v1/models", headers, "httpx", "direct")
    evidence["model_list_contains_target"] = any(
        x.get("id") == config["model"] for x in (models or {}).get("data", [])
    )
    report["read_only_probes"].append(evidence)
    dispatches = []
    for path in a.runs.glob("*/ledger.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("event") == "dispatch":
                dispatches.append(item)
    if not dispatches:
        raise SystemExit("No local dispatch window to correlate")
    times = [datetime.fromisoformat(x["time_utc"]).timestamp() for x in dispatches]
    start, end = int(min(times)) - 30, int(max(times)) + 180
    query = urllib.parse.urlencode(
        {
            "p": 0,
            "page_size": 100,
            "start_timestamp": start,
            "end_timestamp": end,
            "model_name": config["model"],
        }
    )
    evidence, logs = get_probe(base_url, "/api/log/token?" + query, headers, "httpx", "direct")
    report["read_only_probes"].append(evidence)
    report["provider_log_access"] = {
        "success": (logs or {}).get("success"),
        "http_status": evidence.get("status_code"),
    }
    report["provider_records"] = relevant_logs(logs, start, end, config["model"])
    report["local_dispatches"] = [
        {
            k: v
            for k, v in x.items()
            if k in ("task_id", "stage", "batch", "attempt", "time_utc", "input_estimate", "output_limit")
        }
        for x in dispatches
    ]
    atomic_json(a.output, report)
    print(
        json.dumps(
            {
                "output": str(a.output),
                "paid_requests": 0,
                "probes": [
                    {
                        k: v
                        for k, v in x.items()
                        if k in ("transport", "proxy_mode", "status_code", "phase", "elapsed_seconds")
                    }
                    for x in report["read_only_probes"]
                ],
                "provider_records": report["provider_records"],
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
