"""Inbox cursor/stream regression tests, no network, credentials or live state."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/inbox.py'


def message(mid, *, content='hello', username='human', **extra):
    return {'id': mid, 'username': username, 'content': content, 'msgType': 'text', **extra}


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'agent'
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {'WEBHARNESS_AGENT_HOME': str(self.home),
                                                 'WEBHARNESS_URL': 'https://unit.invalid'}))
        spec = importlib.util.spec_from_file_location('inbox_under_test', SCRIPT)
        self.inbox = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.inbox)
        self.enterContext(patch.object(self.inbox, 'login', return_value=('listener', 'secret-token')))
        self.enterContext(patch.object(self.inbox, 'join_existing'))
        self.watermark = self.inbox._state_file('last_id_room')
        self.streams = self.inbox._state_file('inbox_streams_room.json')

    def run_inbox(self, responses, *args):
        with patch.object(self.inbox.sys, 'argv', [str(SCRIPT), 'room', *args]), \
                patch.object(self.inbox, 'http', side_effect=responses) as http, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.inbox.main()
        return json.loads(output.getvalue()), http

    def test_initialized_zero_uses_incremental_not_last_50(self):
        self.watermark.write_text('0')
        out, http = self.run_inbox([{'messages': [message(1)]}])
        self.assertIn('afterId=0', http.call_args.args[1])
        self.assertEqual(out['lastId'], 1)
        self.assertEqual(self.watermark.read_text(), '1')

    def test_first_empty_read_persists_zero(self):
        out, http = self.run_inbox([{'messages': []}])
        self.assertTrue(self.watermark.exists())
        self.assertEqual(self.watermark.read_text(), '0')
        self.assertEqual(out['newMessages'], [])
        self.assertIn('limit=50', http.call_args.args[1])

    def test_batch_over_200_is_drained_without_another_notification(self):
        self.watermark.write_text('0')
        out, http = self.run_inbox([{'messages': [message(i) for i in range(1, 201)]},
                                    {'messages': [message(i) for i in range(201, 206)]}])
        self.assertEqual(len(out['newMessages']), 205)
        self.assertEqual(out['lastId'], 205)
        self.assertEqual(http.call_count, 2)
        self.assertIn('afterId=200', http.call_args.args[1])

    def test_empty_draft_completion_survives_newer_message_and_restart(self):
        self.watermark.write_text('10')
        out, _ = self.run_inbox([{'messages': [message(11, content='', streaming=True), message(12)]}])
        self.assertEqual([m['id'] for m in out['newMessages']], [12])
        self.assertEqual(out['lastId'], 12)
        self.assertEqual(json.loads(self.streams.read_text())['pendingIds'], [11])
        out, http = self.run_inbox([{'messages': [message(11, content='completed')]}, {'messages': []}])
        self.assertIn('streamIds=11', http.call_args_list[0].args[1])
        self.assertEqual([m['id'] for m in out['newMessages']], [11])
        self.assertEqual(out['lastId'], 12)
        self.assertEqual(json.loads(self.streams.read_text())['pendingIds'], [])
        out, _ = self.run_inbox([{'messages': []}])
        self.assertEqual(out['newMessages'], [])

    def test_more_than_60_pending_streams_are_revisited_in_batches(self):
        self.watermark.write_text('100')
        self.streams.write_text(json.dumps({'pendingIds': list(range(1, 66))}))
        out, http = self.run_inbox([{'messages': [message(i) for i in range(1, 61)]},
                                    {'messages': [message(i) for i in range(61, 66)]}, {'messages': []}])
        self.assertEqual(len(out['newMessages']), 65)
        for call in http.call_args_list[:2]:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[1]).query)
            self.assertLessEqual(len(query['streamIds'][0].split(',')), 60)

    def test_peek_does_not_change_watermark_or_stream_state(self):
        self.watermark.write_text('10')
        self.streams.write_text('{"pendingIds":[9]}')
        out, _ = self.run_inbox([{'messages': [message(9)]}, {'messages': [message(11, streaming=True)]}], '--peek')
        self.assertEqual([m['id'] for m in out['newMessages']], [9])
        self.assertEqual(self.watermark.read_text(), '10')
        self.assertEqual(self.streams.read_text(), '{"pendingIds":[9]}')

    def test_hidden_own_and_recalled_filtered_but_empty_voice_kept(self):
        self.watermark.write_text('0')
        out, _ = self.run_inbox([{'messages': [message(1, content='', username='', streaming=True),
                                               message(2, username='listener', streaming=True),
                                               message(3, recalled=True),
                                               message(4, content='', msgType='voice')]}])
        self.assertEqual([m['id'] for m in out['newMessages']], [4])
        self.assertEqual(json.loads(self.streams.read_text())['pendingIds'], [])

    def test_long_poll_timeout_exceeds_wait_and_does_not_start_subprocess(self):
        self.watermark.write_text('0')
        out, http = self.run_inbox([{'messages': []}], '--wait', '600', '--peek')
        self.assertIn('wait=600', http.call_args.args[1])
        self.assertEqual(http.call_args.kwargs['timeout'], 610)
        self.assertFalse(out['shouldReply'])

    def test_pending_stream_no_longer_visible_is_removed(self):
        self.watermark.write_text('20')
        self.streams.write_text('{"pendingIds":[11,12]}')
        out, _ = self.run_inbox([{'messages': [message(11, content='', username='')]}, {'messages': []}])
        self.assertEqual(out['newMessages'], [])
        self.assertEqual(json.loads(self.streams.read_text())['pendingIds'], [])


if __name__ == '__main__':
    unittest.main()
