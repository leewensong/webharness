"""好友关系、private 房间命名空间与邀请访问回归测试。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, main


class FriendsPrivateRoomTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for module, name, value in (
            (db, "DATA_DIR", root),
            (db, "DB_PATH", root / "test.db"),
            (db, "UPLOADS_DIR", root / "uploads"),
            (db, "FILES_DIR", root / "files"),
            (db, "LIBRARY_DIR", root / "library"),
            (main, "UPLOADS_DIR", root / "uploads"),
            (main, "FILES_DIR", root / "files"),
            (main, "LIBRARY_DIR", root / "library"),
            (auth, "SECRET", b"isolated-friends-private-room-test-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        with db.get_db() as conn:
            for user_id, username in ((1, "alice"), (2, "bob"), (3, "charlie")):
                conn.execute(
                    "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
                    (user_id, username, auth.hash_password(username + "-pass")),
                )
            conn.execute(
                """
                INSERT INTO users (id, username, password_hash, kind, owner_id)
                VALUES (4, 'bob-agent', '', 'agent', 2)
                """,
            )
        self.alice = self.headers(1, "alice")
        self.bob = self.headers(2, "bob")
        self.charlie = self.headers(3, "charlie")
        self.agent = self.headers(4, "bob-agent", "agent")

    @staticmethod
    def headers(user_id, username, kind="human"):
        return {"Authorization": "Bearer " + auth.create_token(user_id, username, kind, 0)}

    def test_private_room_names_are_scoped_and_room_key_is_returned(self):
        alice_room = self.client.post(
            "/api/rooms",
            headers=self.alice,
            json={"roomName": "Lobby", "visibility": "private"},
        )
        self.assertEqual(alice_room.status_code, 200, alice_room.text)
        alice_data = alice_room.json()
        self.assertEqual(alice_data["roomKey"], "alice:Lobby")
        self.assertEqual(alice_data["namespaceOwnerName"], "alice")
        self.assertEqual(alice_data["creatorName"], "alice")

        bob_room = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomName": "Lobby", "visibility": "private"},
        )
        self.assertEqual(bob_room.status_code, 200, bob_room.text)
        bob_data = bob_room.json()
        self.assertEqual(bob_data["roomKey"], "bob:Lobby")
        self.assertNotEqual(bob_data["roomId"], alice_data["roomId"])

        by_key = self.client.get(f"/api/rooms/{alice_data['roomKey']}", headers=self.alice)
        self.assertEqual(by_key.status_code, 200, by_key.text)
        self.assertEqual(by_key.json()["roomId"], alice_data["roomId"])

        by_id = self.client.get(f"/api/rooms/{alice_data['roomId']}", headers=self.alice)
        self.assertEqual(by_id.status_code, 200, by_id.text)
        self.assertEqual(by_id.json()["roomKey"], alice_data["roomKey"])

        ambiguous = self.client.post(
            "/api/rooms",
            headers=self.charlie,
            json={"roomName": "Lobby"},
        )
        self.assertEqual(ambiguous.status_code, 409, ambiguous.text)
        self.assertEqual(ambiguous.json()["detail"]["code"], "room_reference_required")

        public_room = self.client.post(
            "/api/rooms",
            headers=self.charlie,
            json={"roomName": "Lobby", "visibility": "public"},
        )
        self.assertEqual(public_room.status_code, 200, public_room.text)
        self.assertEqual(public_room.json()["visibility"], "public")

    def test_friend_invite_is_required_and_friend_can_bring_agent(self):
        created = self.client.post(
            "/api/rooms",
            headers=self.alice,
            json={"roomName": "Secret", "visibility": "private", "password": "secret-pass"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        room = created.json()

        blocked = self.client.post(
            "/api/rooms",
            headers=self.charlie,
            json={"roomKey": room["roomKey"], "roomName": "Secret", "password": "secret-pass"},
        )
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.assertEqual(blocked.json()["detail"]["code"], "friend_or_invite_required")

        request = self.client.post(
            "/api/friends/requests", headers=self.alice, json={"username": "bob"}
        )
        self.assertEqual(request.status_code, 200, request.text)
        request_id = request.json()["request"]["id"]

        accepted = self.client.post(
            f"/api/friends/requests/{request_id}/accept", headers=self.bob
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["friendship"]["status"], "accepted")
        pair_room = accepted.json()["pairRoom"]
        self.assertTrue(pair_room["isDirectPair"])
        self.assertNotEqual(pair_room["roomId"], room["roomId"])

        alice_room_list = self.client.get("/api/rooms", headers=self.alice)
        bob_room_list = self.client.get("/api/rooms", headers=self.bob)
        self.assertIn(pair_room["roomId"], {r["roomId"] for r in alice_room_list.json()["rooms"]})
        self.assertIn(pair_room["roomId"], {r["roomId"] for r in bob_room_list.json()["rooms"]})

        agent_friend_list = self.client.get("/api/friends", headers=self.headers(4, "bob-agent", "agent"))
        self.assertEqual(agent_friend_list.status_code, 200, agent_friend_list.text)
        self.assertIn("alice", {friend["username"] for friend in agent_friend_list.json()["friends"]})

        pair_repeat = self.client.post(
            f"/api/friends/requests/{request_id}/accept", headers=self.bob
        )
        self.assertEqual(pair_repeat.status_code, 200, pair_repeat.text)
        self.assertTrue(pair_repeat.json()["idempotent"])
        self.assertEqual(pair_repeat.json()["pairRoom"]["roomId"], pair_room["roomId"])

        pair_make_public = self.client.patch(
            f"/api/rooms/{pair_room['roomId']}",
            headers=self.alice,
            json={"visibility": "public"},
        )
        self.assertEqual(pair_make_public.status_code, 403, pair_make_public.text)
        self.assertEqual(pair_make_public.json()["detail"]["code"], "pair_room_only")

        third_party_join = self.client.post(
            "/api/rooms",
            headers=self.charlie,
            json={"roomId": pair_room["roomId"], "roomName": pair_room["roomName"]},
        )
        self.assertEqual(third_party_join.status_code, 403, third_party_join.text)
        self.assertEqual(third_party_join.json()["detail"]["code"], "pair_member_required")

        pair_invite = self.client.post(
            f"/api/rooms/{pair_room['roomId']}/invites",
            headers=self.alice,
            json={"username": "charlie"},
        )
        self.assertEqual(pair_invite.status_code, 403, pair_invite.text)
        self.assertEqual(pair_invite.json()["detail"]["code"], "pair_room_only")

        brought_pair_agent = self.client.post(
            f"/api/rooms/{pair_room['roomId']}/agents",
            headers=self.bob,
            json={"agentUsername": "bob-agent"},
        )
        self.assertEqual(brought_pair_agent.status_code, 200, brought_pair_agent.text)

        invite = self.client.post(
            f"/api/rooms/{room['roomId']}/invites",
            headers=self.alice,
            json={"username": "bob"},
        )
        self.assertEqual(invite.status_code, 200, invite.text)
        invite_id = invite.json()["invite"]["id"]
        bob_invites = self.client.get("/api/room-invites", headers=self.bob)
        self.assertEqual(bob_invites.status_code, 200, bob_invites.text)
        self.assertIn(invite_id, {item["id"] for item in bob_invites.json()["invites"]})

        before_accept = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomKey": room["roomKey"], "roomName": "Secret", "password": "secret-pass"},
        )
        self.assertEqual(before_accept.status_code, 403, before_accept.text)

        invite_accept = self.client.post(
            f"/api/room-invites/{invite_id}/accept", headers=self.bob
        )
        self.assertEqual(invite_accept.status_code, 200, invite_accept.text)

        joined = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomKey": room["roomKey"], "roomName": "Secret", "password": "secret-pass"},
        )
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertTrue(joined.json()["joined"])

        agent_join = self.client.post(
            f"/api/rooms/{room['roomId']}/agents",
            headers=self.bob,
            json={"agentUsername": "bob-agent"},
        )
        self.assertEqual(agent_join.status_code, 200, agent_join.text)
        self.assertEqual(agent_join.json()["username"], "bob-agent")

        agent_repeat = self.client.post(
            f"/api/rooms/{room['roomId']}/agents",
            headers=self.bob,
            json={"agentUsername": "bob-agent"},
        )
        self.assertEqual(agent_repeat.status_code, 200, agent_repeat.text)
        self.assertTrue(agent_repeat.json()["joined"])

        agent_cannot_invite = self.client.post(
            f"/api/rooms/{room['roomId']}/invites",
            headers=self.agent,
            json={"username": "charlie"},
        )
        self.assertEqual(agent_cannot_invite.status_code, 403, agent_cannot_invite.text)

    def test_active_member_status_leave_and_mention_message(self):
        created = self.client.post(
            "/api/rooms",
            headers=self.alice,
            json={"roomName": "NoticeBoard", "visibility": "public"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        room_name = created.json()["roomName"]
        room_id = created.json()["roomId"]
        joined = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomId": room_id, "roomName": room_name},
        )
        self.assertEqual(joined.status_code, 200, joined.text)

        with db.get_db() as conn:
            conn.execute(
                "UPDATE room_members SET last_seen_at = datetime('now', '-10 minutes') WHERE room_id = ? AND user_id = 2",
                (room_id,),
            )
        detail = self.client.get(f"/api/rooms/{room_name}", headers=self.alice)
        self.assertEqual(detail.status_code, 200, detail.text)
        roster_bob = next(item for item in detail.json()["roomMembers"] if item["username"] == "bob")
        self.assertFalse(roster_bob["online"])
        self.assertFalse(roster_bob["inRoom"])

        members = self.client.get(f"/api/rooms/{room_name}/members", headers=self.alice)
        self.assertEqual(members.status_code, 200, members.text)
        self.assertEqual({m["username"] for m in members.json()["members"]}, {"alice", "bob"})
        self.assertTrue(all(m["membershipStatus"] == "active" for m in members.json()["members"]))
        self.assertFalse(next(m for m in members.json()["members"] if m["username"] == "bob")["inRoom"])

        sent = self.client.post(
            f"/api/rooms/{room_name}/messages",
            headers=self.alice,
            json={"content": "@bob 请回来看看"},
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["mentionTo"]["username"], "bob")
        self.assertFalse(sent.json()["whisper"])
        offline_read = self.client.get(
            f"/api/rooms/{room_name}/messages?afterId=0", headers=self.bob
        )
        self.assertEqual(offline_read.status_code, 200, offline_read.text)
        received = next(item for item in offline_read.json()["messages"] if item["id"] == sent.json()["id"])
        self.assertEqual(received["mentionTo"]["username"], "bob")

        left = self.client.post(f"/api/rooms/{room_name}/leave", headers=self.bob)
        self.assertEqual(left.status_code, 200, left.text)
        after_leave = self.client.get(f"/api/rooms/{room_name}/members", headers=self.alice)
        self.assertEqual(after_leave.status_code, 200, after_leave.text)
        self.assertNotIn("bob", {item["username"] for item in after_leave.json()["members"]})

        after_left_mention = self.client.post(
            f"/api/rooms/{room_name}/messages",
            headers=self.alice,
            json={"content": "@bob 这条应该被拒绝"},
        )
        self.assertEqual(after_left_mention.status_code, 400, after_left_mention.text)
        self.assertEqual(after_left_mention.json()["detail"]["code"], "mention_target_not_member")

        rejoined = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomId": room_id, "roomName": room_name},
        )
        self.assertEqual(rejoined.status_code, 200, rejoined.text)
        removed = self.client.delete(
            f"/api/rooms/{room_name}/members/bob", headers=self.alice
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        denied_rejoin = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomId": room_id, "roomName": room_name},
        )
        self.assertEqual(denied_rejoin.status_code, 403, denied_rejoin.text)
        self.assertEqual(denied_rejoin.json()["detail"]["code"], "member_removed")

        missing_ref = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomId": 99999, "roomName": "MustNotCreate"},
        )
        self.assertEqual(missing_ref.status_code, 404, missing_ref.text)
        with db.get_db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM rooms WHERE name='MustNotCreate'").fetchone())


if __name__ == "__main__":
    unittest.main()
