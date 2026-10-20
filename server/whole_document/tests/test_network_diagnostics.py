"""The real socket failure must retain useful, secret-free evidence."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from translation.gateway import GatewayError, post_json, post_json_httpx
from translation.network_diagnostics import proxy_for


class DiagnosticsTest(unittest.TestCase):
    def test_completed_sse_is_not_recorded_as_network_failure(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(
                    b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                    b'data: [DONE]\n\n'
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            evidence = {}
            _, _, result = post_json_httpx(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"stream": True}, 2, diagnostics=evidence,
            )
            self.assertFalse(result["_stream_incomplete"])
            self.assertNotIn("failure_phase", evidence)
        finally:
            server.shutdown()
            server.server_close()

    def test_proxy_policy_is_explicit_and_loopback_stays_local(self):
        from translation.gateway import classify

        self.assertEqual(classify(524, "upstream timeout"), "transient")
        with (
            patch(
                "translation.network_diagnostics.urllib.request.getproxies",
                return_value={"https": "http://127.0.0.1:7990"},
            ),
            patch("translation.network_diagnostics.urllib.request.proxy_bypass", return_value=False),
        ):
            self.assertEqual(proxy_for("https://provider.invalid/v1", "system"), "http://127.0.0.1:7990")
            self.assertIsNone(proxy_for("https://provider.invalid/v1", "direct"))
            self.assertIsNone(proxy_for("http://127.0.0.1:1234", "system"))

    def test_gateway_records_oneapi_id_and_safe_response_headers(self):
        from translation.gateway import Gateway

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("X-Oneapi-Request-Id", "fixture-provider-id")
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "private-cookie")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"choices": [{"finish_reason": "stop", "message": {"content": "译文"}}]}
                    ).encode()
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for transport in (post_json, post_json_httpx):
                events = []
                config = {
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "api_key": "fixture-secret-123",
                    "model": "fixture",
                    "max_requests": 1,
                    "max_task_tokens": 1000,
                    "timeout_seconds": 2,
                }
                gw = Gateway(config, events.append, transport=transport)
                gw.request(
                    "fixture", "initial", 0, [{"role": "user", "content": "PRIVATE DOCUMENT TEXT"}], 10, 20
                )
                event = events[-1]
                self.assertEqual(event["request_id"], "fixture-provider-id")
                self.assertTrue(event["network"]["response_headers_received"])
                self.assertIn("response_headers_seconds", event["network"])
                self.assertNotIn("set-cookie", event["network"]["response_headers"])
                self.assertNotIn("fixture-secret-123", json.dumps(events))
                self.assertNotIn("PRIVATE DOCUMENT TEXT", json.dumps(events))
        finally:
            server.shutdown()
            server.server_close()

    def test_disconnect_retains_exception_chain_and_stage(self):
        class CloseHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), CloseHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for transport in (post_json, post_json_httpx):
                with self.subTest(transport=transport.__name__):
                    with self.assertRaises(GatewayError) as raised:
                        transport(
                            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                            {
                                "Authorization": "Bearer fixture-secret-123",
                                "Content-Type": "application/json",
                            },
                            {
                                "model": "fixture",
                                "messages": [{"role": "user", "content": "PRIVATE DOCUMENT TEXT"}],
                            },
                            2,
                        )
                    evidence = raised.exception.diagnostics
                    self.assertTrue(evidence["exception_chain"])
                    self.assertIn("receive_headers", evidence["phase"])
                    self.assertFalse(evidence["response_headers_received"])
                    self.assertGreaterEqual(evidence["elapsed_seconds"], 0)
                    serialized = json.dumps(evidence)
                    self.assertNotIn("fixture-secret-123", serialized)
                    self.assertNotIn("PRIVATE DOCUMENT TEXT", serialized)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
