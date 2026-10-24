"""Silent event stream tests using only temporary SQLite and synthetic users."""
import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from app import auth, db, main


class MessageEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for module, name, value in (
            (db, "DATA_DIR", root), (db, "DB_PATH", root / "test.db"),
            (db, "UPLOADS_DIR", root / "uploads"), (db, "FILES_DIR", root / "files"),
            (auth, "SECRET", b"isolated-events-test"),
            (main, "_room_events", {}),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        self.enterContext(patch.object(main, "_main_loop", asyncio.get_running_loop()))
        with db.get_db() as conn:
            for uid, name, kind in ((1, 'owner', 'human'), (2, 'listener', 'agent'),
                                    (3, 'alice', 'human'), (4, 'bob', 'human'), (5, 'bot', 'agent')):
                conn.execute("INSERT INTO users (id, username, kind) VALUES (?, ?, ?)", (uid, name, kind))
        self.headers = {uid: {"Authorization": "Bearer " + auth.create_token(uid, name, kind)}
                        for uid, name, kind in ((1, 'owner', 'human'), (2, 'listener', 'agent'),
                                               (3, 'alice', 'human'), (4, 'bob', 'human'), (5, 'bot', 'agent'))}
        response = self.client.post('/api/rooms', headers=self.headers[1], json={"roomName": "silent-test", "visibility": "public"})
        self.assertEqual(response.status_code, 200, response.text)
        self.room_id = response.json()['roomId']
        for uid in (2, 3, 4, 5):
            response = self.client.post('/api/rooms', headers=self.headers[uid], json={"roomName": "silent-test"})
            self.assertEqual(response.status_code, 200, response.text)
        self.endpoint = next(r.endpoint for r in main.app.routes if getattr(r, 'path', '') == '/api/rooms/{room_name}/message-events')
        self.iterator = None

    async def asyncTearDown(self):
        if self.iterator:
            await self.iterator.aclose()

    async def stream(self, floor=0):
        header = self.headers[2]['Authorization']
        request = Request({'type': 'http', 'headers': [(b'authorization', header.encode())]})
        response = await self.endpoint('silent-test', request, main.require_user(header), floor)
        self.assertEqual(response.headers['x-accel-buffering'], 'no')
        self.iterator = response.body_iterator
        return response.body_iterator

    def send(self, uid=3, content='hello'):
        result = self.client.post('/api/rooms/silent-test/messages', headers=self.headers[uid], json={'content': content})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['id']

    async def test_no_idle_frames_and_event_wakes_with_ids_only(self):
        stream = await self.stream()
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(.1)
        self.assertFalse(pending.done(), 'idle stream must remain blocked, not send empty heartbeats')
        mid = self.send()
        event = await asyncio.wait_for(pending, 1)
        self.assertEqual(json.loads(event.split('data: ')[1]), {'messageIds': [mid]})
        self.assertNotIn('hello', event)

    async def test_own_agent_and_hidden_whispers_do_not_emit(self):
        self.send(2, 'own'); self.send(5, 'other agent'); self.send(3, '@@bob private')
        stream = await self.stream()
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(.1)
        self.assertFalse(pending.done())
        mid = self.send(3, '@@listener visible')
        event = await asyncio.wait_for(pending, 1)
        self.assertEqual(json.loads(event.split('data: ')[1])['messageIds'], [mid])

    async def test_pending_stream_completion_survives_newer_message(self):
        result = self.client.post('/api/rooms/silent-test/messages/stream', headers=self.headers[3], json={'content': 'draft'})
        self.assertEqual(result.status_code, 200, result.text)
        draft = result.json()['id']
        completed = self.send(4, 'newer')
        stream = await self.stream()
        first = await anext(stream)
        self.assertEqual(json.loads(first.split('data: ')[1])['messageIds'], [completed])
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(.05)
        with db.get_db() as conn:
            conn.execute('UPDATE messages SET streaming = 0 WHERE id = ?', (draft,))
        main.notify_room(self.room_id)
        second = await asyncio.wait_for(pending, 1)
        self.assertEqual(json.loads(second.split('data: ')[1])['messageIds'], [draft])

    async def test_revoked_membership_closes_without_message_content(self):
        stream = await self.stream()
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(.05)
        with db.get_db() as conn:
            conn.execute('DELETE FROM room_members WHERE room_id = ? AND user_id = 2', (self.room_id,))
        main.notify_room(self.room_id)
        event = await asyncio.wait_for(pending, 1)
        self.assertIn('event: stop', event)
        self.assertNotIn('messageIds', event)

    async def test_more_than_one_batch_does_not_strand_backlog(self):
        with db.get_db() as conn:
            for _ in range(205):
                conn.execute('INSERT INTO messages (room_id, user_id, content) VALUES (?, 3, ?)', (self.room_id, 'batch'))
        stream = await self.stream()
        event = await anext(stream)
        self.assertEqual(len(json.loads(event.split('data: ')[1])['messageIds']), 205)


class WatchStatusTests(unittest.TestCase):
    def test_status_is_local_and_sse_parser_has_no_polling(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'WEBHARNESS_AGENT_HOME': root}):
            script = Path(__file__).resolve().parents[1] / 'scripts/watch.py'
            spec = importlib.util.spec_from_file_location('isolated_watch', script)
            watcher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(watcher)
            self.assertEqual(list(watcher.sse_events(io.BytesIO(b'event: messages\ndata: {"messageIds":[7]}\n\n'))),
                             [('messages', {'messageIds': [7]})])
            with patch.object(watcher.sys, 'argv', [str(script), 'test', '--status']), patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                watcher.main()
