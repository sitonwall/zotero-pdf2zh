import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from translation.gateway import GatewayError, post_json, read_stream


class DisconnectTest(unittest.TestCase):
    def test_stream_content_usage_and_incomplete_json_preserved(self):
        chunks = [
            {
                "id": "fixture",
                "choices": [{"delta": {"content": '[{"paragraph_id":"p1",'}, "finish_reason": None}],
            },
            {
                "id": "fixture",
                "choices": [{"delta": {"content": '"translated_text":"译文"}]'}, "finish_reason": "stop"}],
            },
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22}},
        ]
        body = b"".join(("data: " + json.dumps(c) + "\n\n").encode() for c in chunks)
        result = read_stream(io.BytesIO(body + b"data: [DONE]\n\n"))
        self.assertEqual(result["usage"]["total_tokens"], 22)
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertFalse(result["_stream_incomplete"])
        partial = read_stream(io.BytesIO(body))
        self.assertTrue(partial["_stream_incomplete"])
        self.assertEqual(partial["choices"][0]["finish_reason"], "interrupted")
        self.assertIn("译文", partial["choices"][0]["message"]["content"])

    def test_remote_close_is_unknown_outcome(self):
        class CloseHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), CloseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(GatewayError) as caught:
                post_json(
                    f"http://127.0.0.1:{server.server_port}/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "fixture"},
                    2,
                )
            self.assertEqual(caught.exception.category, "outcome_unknown")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
