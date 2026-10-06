"""OpenAI-compatible wire contract using a local mock HTTP server, not a GPU."""
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.config import Config, DEFAULT_FILE, load_toml
from core.summarize import Summarizer, parse_sections


class UpstreamTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = Config(load_toml(DEFAULT_FILE))
        cfg.data['system']['opencc'] = False
        cfg.data['summary'].update(retries=0, min_chars=1, empty_chars=1, request_timeout=2)
        self.calls = []
        self.responses = []
        self.release = threading.Event()
        self.entered = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.calls.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                code, body, headers = owner.responses.pop(0)
                owner.entered.set()
                if body == 'wait':
                    owner.release.wait(5)
                    body = owner.good()
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(body).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.s = Summarizer(cfg, self.root, f'http://127.0.0.1:{self.server.server_port}/api/v1',
                            {'name': 'lab', 'remote': True, 'model_id': 'my-30b',
                             'api_key': 'secret-token', 'disable_thinking': False})

    def stop_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    @staticmethod
    def good():
        return {'choices': [{'message': {'content': json.dumps({'topic': 'Topic', 'points': ['Point'], 'terms': [], 'emphasis': []})}, 'finish_reason': 'stop'}], 'usage': {}}

    def test_wire_auth_model_base_path_and_no_engine_extensions(self):
        self.responses.append((200, self.good(), {}))
        self.s.warmup()
        self.assertEqual(self.calls, [])
        data, _ = self.s.call_llm('transcript')
        self.assertEqual(data['topic'], 'Topic')
        path, headers, body = self.calls[0]
        self.assertEqual(path, '/api/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer secret-token')
        self.assertEqual(body['model'], 'my-30b')
        self.assertNotIn('cache_prompt', body)
        self.assertNotIn('chat_template_kwargs', body)
        self.assertIn('response_format', body)

    def test_schema_rejection_falls_back_without_leaking_body(self):
        self.responses.extend([(400, {'error': 'response_format secret-token'}, {}), (200, self.good(), {})])
        with patch('core.summarize.log') as log:
            self.s.call_llm('text')
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('response_format', self.calls[1][2])
        self.assertNotIn('secret-token', str(log.call_args_list))

    def test_auth_error_not_retried_or_written_to_notes(self):
        self.s.s['retries'] = 2
        self.responses.append((401, {'error': 'secret-token http://private.invalid'}, {}))
        sections = parse_sections('## 00:00:00\nSome transcript')
        with patch('core.summarize.log') as log:
            self.s.process(sections, sections)
        text = self.s.jsonl.read_text() + self.s.notes_md.read_text() + str(log.call_args_list)
        self.assertIn('HTTP 401', text)
        self.assertNotIn('secret-token', text)
        self.assertNotIn('private.invalid', text)
        self.assertEqual(len(self.calls), 1)

    def test_transient_failure_retries_then_succeeds(self):
        self.s.s['retries'] = 2
        self.responses.extend([(503, {'error': 'secret-token'}, {}), (200, self.good(), {})])
        with patch.object(self.s.abort, 'wait', return_value=False):
            data, _ = self.s.call_llm('text')
        self.assertEqual(data['topic'], 'Topic')
        self.assertEqual(len(self.calls), 2)

    def test_connection_failure_is_sanitized(self):
        from urllib.error import URLError
        with patch.object(self.s, '_post_http', side_effect=URLError('http://secret-token.invalid')):
            with self.assertRaisesRegex(RuntimeError, '連線失敗') as error:
                self.s.call_llm('text')
        self.assertNotIn('secret-token', str(error.exception))

    def test_nullable_usage_is_accepted(self):
        response = self.good()
        response['usage'] = None
        self.responses.append((200, response, {}))
        sections = parse_sections('## 00:00:00\nSome transcript')
        self.s.process(sections, sections)
        self.assertEqual(json.loads(self.s.jsonl.read_text())['status'], 'ok')

    def test_redirect_is_not_followed(self):
        self.responses.append((307, {}, {'Location': '/steal'}))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 307'):
            self.s.call_llm('text')
        self.assertEqual(len(self.calls), 1)

    def test_timeout_and_malformed_responses_have_safe_errors(self):
        for body in (None, {'choices': []}, {'choices': [None]}, {'choices': [{'message': {'content': 'secret-token'}}]}):
            self.responses.append((200, body, {}))
            with self.assertRaisesRegex(RuntimeError, '回覆格式不合法') as error:
                self.s.call_llm('text')
            self.assertNotIn('secret-token', str(error.exception))
        self.s.s['request_timeout'] = 0.1
        self.responses.append((200, 'wait', {}))
        with self.assertRaisesRegex(RuntimeError, '逾時'):
            self.s.call_llm('text')

    def test_abort_returns_promptly_and_late_reply_never_saves(self):
        self.responses.append((200, 'wait', {}))
        sections = parse_sections('## 00:00:00\nSome transcript')
        worker = threading.Thread(target=self.s.process, args=(sections, sections))
        worker.start()
        self.assertTrue(self.entered.wait(2))
        self.s.abort.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.release.set()
        time.sleep(0.05)
        self.assertFalse(self.s.jsonl.exists())
        self.assertFalse(self.s.notes_md.exists())

    def test_local_payload_and_endpoint_are_unchanged(self):
        self.s.model = {'name': 'local-model', 'disable_thinking': True}
        self.s.url = 'http://localhost:1234'
        body = self.s._request_body([], 10)
        self.assertTrue(body['cache_prompt'])
        self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': False})
        self.assertNotIn('model', body)
        with patch('urllib.request.urlopen') as post:
            post.return_value.__enter__.return_value = io.StringIO('{}')
            self.s._post(body, 1)
        req = post.call_args.args[0]
        self.assertEqual(req.full_url, 'http://localhost:1234/v1/chat/completions')
        self.assertNotIn('Authorization', req.headers)
