"""Watcher CLI/transport tests: temporary identities, fake HTTP, no live rooms."""
import contextlib
from email.message import Message
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'watch.py'
spec = importlib.util.spec_from_file_location('watch_under_test', SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class Response(io.BytesIO):
    def __init__(self, frames='', content_type='text/event-stream'):
        super().__init__(frames.encode())
        self.headers = Message()
        self.headers['Content-Type'] = content_type


def event(ids):
    return 'event: messages\ndata: ' + json.dumps({'messageIds': ids}) + '\n\n'


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'agent'
        self.watcher = watch.Watcher('test-room', self.home, once=True)
        watch._atomic_write(self.watcher.watermark_path, '10')
        self.inbox = types.SimpleNamespace(
            URL='https://unit.invalid', login=Mock(return_value=('listener', 'secret-token')),
            join_existing=Mock(), http=Mock(),
        )
        self.enterContext(patch.object(self.watcher, 'load_inbox', return_value=self.inbox))
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.sleep = self.enterContext(patch.object(watch.time, 'sleep'))

    def listen(self, responses):
        with patch.object(watch.urllib.request, 'urlopen', side_effect=responses) as opened:
            result = self.watcher.run()
        return result, opened

    def output(self, prefix):
        return [json.loads(line.split(' ', 1)[1]) for line in self.stdout.getvalue().splitlines()
                if line.startswith(prefix + ' ')]

    def test_once_exits_only_after_new_ids_and_never_advances_cursor(self):
        result, opened = self.listen([Response(event([9, 10, 11, 11, 12]))])
        self.assertEqual(result, 0)
        self.assertEqual(self.watcher.watermark(), 10)
        self.assertEqual(self.output('AGENT_LOOP_TICK_webharness')[0]['messageIds'], [11, 12])
        self.assertEqual(self.watcher.read_state()['pendingIds'], [11, 12])
        self.assertEqual(self.watcher.read_state()['status'], 'ready')
        self.assertFalse(self.watcher.status()['running'])
        self.assertIn('message-events?afterId=10', opened.call_args.args[0].full_url)
        self.assertEqual(opened.call_args.kwargs['timeout'], 660)
        self.assertEqual(self.inbox.join_existing.call_count, 1)
        self.assertNotIn('secret-token', self.stdout.getvalue())
        self.sleep.assert_not_called()

    def test_continuous_deduplicates_replay_including_after_reconnect(self):
        self.watcher.once = False
        frames = event([11]) + event([11, 12])
        stop = 'event: stop\ndata: {"status":403}\n\n'
        with self.assertRaises(watch.WatchError) as caught:
            self.listen([Response(frames), Response(event([11, 12]) + event([13]) + stop)])
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual([r['messageIds'] for r in self.output('AGENT_LOOP_TICK_webharness')],
                         [[11], [12], [13]])
        self.assertEqual(self.sleep.call_count, 1)

    def test_idle_timeout_and_transient_disconnect_do_not_exit_or_emit_ticks(self):
        def reconnect_delay(_seconds):
            self.assertEqual(self.stdout.getvalue(), '')
        self.sleep.side_effect = reconnect_delay
        result, opened = self.listen([TimeoutError('network secret'), ConnectionResetError(), Response(event([11]))])
        self.assertEqual(result, 0)
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(len(self.output('AGENT_LOOP_TICK_webharness')), 1)

    def test_token_expiry_reauthenticates_without_empty_notification(self):
        result, _ = self.listen([Response('event: stop\ndata: {"status":401}\n\n'), Response(event([11]))])
        self.assertEqual(result, 0)
        self.assertEqual(self.inbox.login.call_count, 2)
        self.assertEqual(len(self.output('AGENT_LOOP_TICK_webharness')), 1)
        self.assertEqual(self.sleep.call_count, 1)

    def test_late_stream_completion_behind_watermark_is_not_lost(self):
        watch._atomic_write(self.watcher.watermark_path, '12')
        watch._atomic_write(self.watcher.stream_state_path, '{"pendingIds":[11]}')
        result, opened = self.listen([Response(event([11, 12, 13]))])
        self.assertEqual(result, 0)
        self.assertIn('afterId=10', opened.call_args.args[0].full_url)
        self.assertEqual(self.output('AGENT_LOOP_TICK_webharness')[0]['messageIds'], [11, 13])

    def test_transient_login_server_error_is_retried_without_exposing_body(self):
        self.inbox.login.side_effect = [SystemExit('HTTP 503 /login: private body'), ('listener', 'token')]
        self.listen([Response(event([11]))])
        self.assertEqual(self.inbox.login.call_count, 2)
        self.assertNotIn('private body', self.stdout.getvalue())

    def test_truncated_chunked_response_recovers_in_same_process(self):
        from http.client import IncompleteRead
        self.listen([IncompleteRead(b'partial-secret'), Response(event([11]))])
        self.assertEqual(self.sleep.call_count, 1)
        self.assertNotIn('partial-secret', self.stdout.getvalue())

    def test_quick_failures_are_bounded_and_back_off(self):
        self.watcher.max_retries = 3
        with self.assertRaises(watch.WatchError) as caught:
            self.listen([Response(), Response(), Response()])
        self.assertEqual(caught.exception.reason, 'reconnect_exhausted')
        self.assertEqual(self.sleep.call_count, 2)
        self.assertGreaterEqual(self.sleep.call_args_list[1].args[0], 4)
        self.assertEqual(self.output('AGENT_LOOP_TICK_webharness'), [])
        self.assertEqual(self.watcher.read_state()['status'], 'stopped')

    def test_missing_sse_never_falls_back_or_retries(self):
        error = urllib.error.HTTPError('https://unit.invalid', 404, 'secret body', {}, None)
        with self.assertRaises(watch.WatchError) as caught:
            self.listen([error])
        self.assertEqual(caught.exception.reason, 'sse_unavailable_or_room_missing')
        self.sleep.assert_not_called()
        self.inbox.http.assert_not_called()

    def test_wrong_content_type_is_fatal(self):
        with self.assertRaises(watch.WatchError) as caught:
            self.listen([Response('<html>login</html>', 'text/html')])
        self.assertEqual(caught.exception.reason, 'expected_sse_response')
        self.sleep.assert_not_called()

    def test_revoked_access_is_fatal_not_a_successful_once_completion(self):
        with self.assertRaises(watch.WatchError) as caught:
            self.listen([Response('event: stop\ndata: {"status":410}\n\n')])
        self.assertEqual(caught.exception.reason, 'access_revoked')
        self.assertEqual(self.output('AGENT_LOOP_TICK_webharness'), [])

    def test_state_is_durable_before_notification(self):
        def emitted(kind, **fields):
            self.assertEqual(kind, 'AGENT_LOOP_TICK_webharness')
            self.assertEqual(self.watcher.read_state()['pendingIds'], fields['messageIds'])
            self.assertTrue(self.watcher.status()['running'])
        with patch.object(watch, '_emit', side_effect=emitted):
            self.listen([Response(event([11]))])

    def test_consumed_ids_disappear_from_pending(self):
        self.watcher.notify([11, 12])
        watch._atomic_write(self.watcher.watermark_path, '11')
        self.assertEqual(self.watcher.status()['pendingIds'], [12])
        self.assertFalse(self.watcher.notify([11, 12]))
        self.assertEqual(self.watcher.read_state()['pendingIds'], [12])

    def test_restart_redelivers_unread_ids_instead_of_suppressing_them(self):
        self.watcher.notify([11])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        self.listen([Response(event([11, 12]))])
        # Server cursor is authoritative; the new process must not interpret a
        # previous "notification printed" as "human message already handled".
        self.assertEqual(self.output('AGENT_LOOP_TICK_webharness')[0]['messageIds'], [12])
        second = watch.Watcher('test-room', self.home, once=True)
        with patch.object(second, 'load_inbox', return_value=self.inbox), \
                patch.object(watch.urllib.request, 'urlopen', return_value=Response(event([11, 12]))):
            second.run()
        self.assertEqual(self.output('AGENT_LOOP_TICK_webharness')[-1]['messageIds'], [11, 12])

    def test_duplicate_watcher_fails_before_login(self):
        with self.watcher.lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(watch.WatchError) as caught:
                self.watcher.run()
        self.assertEqual(caught.exception.reason, 'watcher_already_running')
        self.inbox.login.assert_not_called()

    def test_missing_or_corrupt_cursor_never_replays_history(self):
        self.watcher.watermark_path.unlink()
        with self.assertRaises(watch.WatchError) as caught:
            self.watcher.run()
        self.assertEqual(caught.exception.reason, 'cursor_missing_run_init_cursor')
        self.inbox.login.assert_not_called()
        watch._atomic_write(self.watcher.watermark_path, 'not-a-number')
        with self.assertRaises(watch.WatchError) as caught:
            self.watcher.run()
        self.assertEqual(caught.exception.reason, 'invalid_cursor')

    def test_init_sets_tail_without_printing_history(self):
        self.watcher.watermark_path.unlink()
        self.inbox.http.return_value = {'messages': [{'id': 120, 'content': 'private history'}]}
        self.assertEqual(self.watcher.run(initialize=True), 0)
        self.assertEqual(self.watcher.watermark(), 120)
        self.assertNotIn('private history', self.stdout.getvalue())
        self.assertIn('messages?limit=1', self.inbox.http.call_args.args[1])

    def test_init_empty_room_persists_zero(self):
        self.watcher.watermark_path.unlink()
        self.inbox.http.return_value = {'messages': []}
        self.watcher.run(initialize=True)
        self.assertTrue(self.watcher.watermark_path.exists())
        self.assertEqual(self.watcher.watermark(), 0)

    def test_init_never_resets_existing_watermark_or_logs_in(self):
        self.watcher.run(initialize=True)
        self.assertEqual(self.watcher.watermark(), 10)
        self.inbox.login.assert_not_called()
        self.inbox.http.assert_not_called()

    def test_initialization_auth_failure_is_sanitized_and_nonzero(self):
        self.watcher.watermark_path.unlink()
        self.inbox.login.side_effect = SystemExit('server body contains secret-token')
        with patch.object(watch, 'Watcher', return_value=self.watcher), \
                patch.object(watch, '_agent_home', return_value=self.home):
            result = watch.main(['test-room', '--init-cursor'])
        self.assertEqual(result, 1)
        self.assertEqual(self.output('WATCHER_ERROR')[0]['reason'],
                         'inbox_configuration_auth_or_membership_error')
        self.assertNotIn('secret-token', self.stdout.getvalue())

    def test_status_does_not_login_and_does_not_create_paths(self):
        empty = self.home / 'empty'
        other = watch.Watcher('test-room', empty)
        with patch.object(other, 'load_inbox', side_effect=AssertionError('no imports/login')):
            self.assertFalse(other.status()['running'])
        self.assertFalse(empty.exists())

    def test_status_uses_lock_not_a_recycled_pid(self):
        self.watcher.write_state('ready')
        self.assertEqual(self.watcher.read_state()['pid'], os.getpid())
        self.assertFalse(self.watcher.status()['running'])

    def test_help_and_invalid_flags_do_not_access_identity_or_network(self):
        with patch.object(watch, '_agent_home', side_effect=AssertionError('no identity access')):
            with self.assertRaises(SystemExit) as caught:
                watch.main(['--help'])
            self.assertEqual(caught.exception.code, 0)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    watch.main(['test-room', '--once', '--status'])
            self.assertEqual(caught.exception.code, 2)

    def test_parser_ignores_comments_and_only_yields_complete_frames(self):
        data = b': keepalive\r\nevent: messages\r\ndata: {"messageIds":\r\ndata: [11]}\r\n\r\nevent: messages\ndata: {"messageIds":[12]}'
        self.assertEqual(list(watch.sse_events(io.BytesIO(data))), [('messages', {'messageIds': [11]})])
        self.assertEqual(self.stdout.getvalue(), '')

    def test_tick_is_valid_json_with_quoted_path_and_room(self):
        unusual = watch.Watcher('test "room', self.home)
        watch._atomic_write(unusual.watermark_path, '0')
        with patch.object(watch, '_inbox_path', return_value=Path('/tmp/path with spaces/inbox.py')):
            unusual.notify([1])
        payload = self.output('AGENT_LOOP_TICK_webharness')[0]
        self.assertEqual(payload['room'], 'test "room')
        self.assertIn(sys.executable, payload['prompt'])
        self.assertIn("'/tmp/path with spaces/inbox.py'", payload['prompt'])

    def test_distributed_script_copies_are_identical(self):
        for name in ('watch.py', 'inbox.py'):
            self.assertEqual((ROOT / 'scripts' / name).read_bytes(),
                             (ROOT / '.cursor/skills/webharness-api/scripts' / name).read_bytes())

    def test_cli_fails_visibly_when_cursor_missing_without_touching_network(self):
        env = dict(os.environ, WEBHARNESS_AGENT_HOME=str(self.home / 'new'),
                   WEBHARNESS_URL='https://unit.invalid')
        result = subprocess.run([sys.executable, str(SCRIPT), 'test-room', '--once'],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('WATCHER_ERROR', result.stdout)
        self.assertIn('cursor_missing_run_init_cursor', result.stdout)
        self.assertEqual(result.stderr, '')


class WatchSocketTests(unittest.TestCase):
    """Exercise a real HTTP stream and subprocess, not just mocked iterators."""

    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ready = threading.Event()
        self.release = threading.Event()
        ready, release = self.ready, self.release
        self.headers_seen = []
        observed = self.headers_seen

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                observed.append((self.path, self.headers.get('Authorization')))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.flush()
                ready.set()
                release.wait(10)
                try:
                    self.wfile.write(event([1]).encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        shutil.copyfile(SCRIPT, self.root / 'watch.py')
        (self.root / 'inbox.py').write_text(
            f'URL = "http://127.0.0.1:{self.server.server_port}"\n'
            'def login(): return ("synthetic", "test-token")\n'
            'def join_existing(token, room): pass\n'
        )
        home = self.root / 'agent'
        (home / 'state').mkdir(parents=True)
        (home / 'state/last_id_socket-test').write_text('0')
        self.env = dict(os.environ, WEBHARNESS_AGENT_HOME=str(home))
        self.proc = subprocess.Popen([sys.executable, str(self.root / 'watch.py'), 'socket-test', '--once'],
                                     env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def tearDown(self):
        self.release.set()
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.communicate(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_idle_is_silent_and_one_message_completes_background_task(self):
        self.assertTrue(self.ready.wait(5), 'watcher must establish the HTTP stream')
        self.assertIsNone(self.proc.poll())
        readable, _, _ = select.select([self.proc.stdout], [], [], .15)
        self.assertEqual(readable, [], 'idle watcher must not print a wake-up')
        self.release.set()
        stdout, stderr = self.proc.communicate(timeout=5)
        self.assertEqual(self.proc.returncode, 0, stderr)
        self.assertEqual(stderr, '')
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertEqual(json.loads(stdout.split(' ', 1)[1])['messageIds'], [1])
        self.assertEqual(self.headers_seen, [('/api/rooms/socket-test/message-events?afterId=0', 'Bearer test-token')])

    def test_sigterm_stops_a_blocked_watcher_without_message_notification(self):
        self.assertTrue(self.ready.wait(5))
        self.proc.terminate()
        stdout, stderr = self.proc.communicate(timeout=5)
        self.assertEqual(self.proc.returncode, 130, stderr)
        self.assertEqual(stdout, '')
        state = json.loads((self.root / 'agent/state/watch_state_socket-test.json').read_text())
        self.assertEqual(state['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
