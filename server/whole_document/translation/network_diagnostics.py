"""Network-stage evidence without request headers, source text, or response bodies."""

import hashlib
import http.client
import ipaddress
import re
import time
import traceback
import urllib.parse
import urllib.request

SAFE_HEADERS = {
    "server",
    "via",
    "date",
    "content-type",
    "content-length",
    "transfer-encoding",
    "connection",
    "retry-after",
    "x-request-id",
    "x-oneapi-request-id",
    "cf-ray",
    "x-new-api-version",
    "cf-cache-status",
    "alt-svc",
}


def proxy_for(url, mode="system"):
    parsed = urllib.parse.urlsplit(url)
    if mode not in ("system", "environment", "direct"):
        raise ValueError("Unsupported proxy mode")
    if mode == "direct":
        return None
    host = parsed.hostname or ""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        if host.lower() == "localhost":
            return None
    proxies = (
        urllib.request.getproxies_environment() if mode == "environment" else urllib.request.getproxies()
    )
    if mode == "environment":
        bypass = urllib.request.proxy_bypass_environment(host, proxies)
    else:
        bypass = urllib.request.proxy_bypass(host)
    if bypass:
        return None
    return proxies.get(parsed.scheme) or proxies.get("all")


def safe_proxy(value):
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "has_credentials": bool(parsed.username or parsed.password),
    }


class NetworkEvidence:
    def __init__(self, transport, url, mode, headers, body, output=None):
        self.started = time.monotonic()
        self.output = output if output is not None else {}
        parsed = urllib.parse.urlsplit(url)
        self.proxy = proxy_for(url, mode)
        auth = headers.get("Authorization", "")
        self.secrets = [x for x in (auth, auth.removeprefix("Bearer ")) if x]
        self.secrets.extend(
            x.get("content")
            for x in body.get("messages", [])
            if isinstance(x.get("content"), str) and x["content"]
        )
        self.output.update(
            schema=1,
            transport=transport,
            host=parsed.hostname,
            path=parsed.path,
            proxy_mode=mode,
            effective_proxy=safe_proxy(self.proxy),
            phase="initializing",
            response_headers_received=False,
            response_headers={},
            exception_chain=[],
            events=[],
        )

    def clean(self, value):
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)Bearer\s+[^\s\x27\x22]+", "Bearer [REDACTED]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
        text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
        return text[:400]

    def mark(self, phase):
        normalized = phase.replace("receive_response_headers", "receive_headers")
        if phase.endswith(".failed"):
            self.output["failure_phase"] = normalized
        self.output["phase"] = self.output.get("failure_phase", normalized)
        if len(self.output["events"]) < 80:
            self.output["events"].append(
                {"phase": phase, "seconds": round(time.monotonic() - self.started, 6)}
            )

    def headers(self, status, headers):
        self.output.update(
            response_headers_received=True,
            status_code=status,
            response_headers={
                str(k).lower(): self.clean(v)
                for k, v in dict(headers).items()
                if str(k).lower() in SAFE_HEADERS
            },
            response_headers_seconds=round(time.monotonic() - self.started, 6),
        )

    def failure(self, exc):
        self.output.setdefault("failure_phase", self.output["phase"])
        seen = set()
        chain = []
        while exc is not None and id(exc) not in seen and len(chain) < 12:
            seen.add(id(exc))
            chain.append(
                {
                    "class": type(exc).__module__ + "." + type(exc).__name__,
                    "message": self.clean(exc),
                    "errno": getattr(exc, "errno", None),
                    "winerror": getattr(exc, "winerror", None),
                    "frames": [
                        {
                            "function": f.name,
                            "file": f.filename.replace("\\", "/").rsplit("/", 1)[-1],
                            "line": f.lineno,
                        }
                        for f in traceback.extract_tb(exc.__traceback__)[-10:]
                    ],
                }
            )
            exc = exc.__cause__ or exc.__context__
        self.output["exception_chain"] = chain
        self.finish()

    def finish(self):
        self.output["elapsed_seconds"] = round(time.monotonic() - self.started, 6)

    def httpx_trace(self, name, info):
        # Closing the iterator after SSE [DONE] raises GeneratorExit inside httpcore.
        # It is client cleanup, not a failed completed model response.
        if (
            name.endswith("receive_response_body.failed")
            and self.output.get("sse_done_received")
            and isinstance(info.get("exception"), GeneratorExit)
        ):
            self.mark("receive_body.closed_after_done")
            return
        self.mark(name)
        if name.endswith("connect_tcp.complete"):
            stream = info.get("return_value")
            if stream:
                peer = stream.get_extra_info("server_addr")
                if peer:
                    self.output["peer"] = list(peer)[:2]
        if name.endswith("start_tls.complete"):
            stream = info.get("return_value")
            if stream:
                self.tls(stream.get_extra_info("ssl_object"))

    def tls(self, sock):
        if sock and all(
            callable(getattr(sock, k, None)) for k in ("version", "selected_alpn_protocol", "cipher")
        ):
            self.output["tls"] = {
                "version": sock.version(),
                "alpn": sock.selected_alpn_protocol(),
                "cipher": sock.cipher()[0] if sock.cipher() else None,
            }
            certificate = sock.getpeercert(True)
            if certificate:
                self.output["tls"]["certificate_sha256"] = hashlib.sha256(certificate).hexdigest()


class ObservedConnection:
    def __init__(self, *args, evidence, **kwargs):
        self.evidence = evidence
        super().__init__(*args, **kwargs)

    def connect(self):
        self.evidence.mark("connect_tcp_proxy_tls.started")
        super().connect()
        self.evidence.mark("connect_tcp_proxy_tls.complete")
        if self.sock:
            self.evidence.output["peer"] = list(self.sock.getpeername())[:2]
            self.evidence.tls(self.sock)

    def send(self, data):
        if self.sock is None:
            self.connect()
        self.evidence.mark("send_request.started")
        super().send(data)
        self.evidence.mark("send_request.complete")

    def getresponse(self):
        self.evidence.mark("receive_headers.started")
        response = super().getresponse()
        self.evidence.headers(response.status, response.headers)
        self.evidence.mark("receive_headers.complete")
        return response


class ObservedHTTPConnection(ObservedConnection, http.client.HTTPConnection):
    pass


class ObservedHTTPSConnection(ObservedConnection, http.client.HTTPSConnection):
    pass


def opener(evidence):
    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(lambda *a, **k: ObservedHTTPConnection(*a, evidence=evidence, **k), req)

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(
                lambda *a, **k: ObservedHTTPSConnection(*a, evidence=evidence, **k),
                req,
                context=self._context,
            )

    proxies = {}
    if evidence.proxy:
        proxies = {"http": evidence.proxy, "https": evidence.proxy}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies), HTTP(), HTTPS())
