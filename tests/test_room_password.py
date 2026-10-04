"""房间加入密码回归测试。仅使用临时 SQLite，不修改真实服务数据。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, main


class RoomPasswordTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for module, name, value in (
            (db, "DATA_DIR", root),
            (db, "DB_PATH", root / "test.db"),
            (db, "UPLOADS_DIR", root / "uploads"),
            (db, "FILES_DIR", root / "files"),
            (main, "UPLOADS_DIR", root / "uploads"),
            (main, "FILES_DIR", root / "files"),
            (auth, "SECRET", b"isolated-room-password-test-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (1, 'wilson', ?)",
                (auth.hash_password("wilson-pass"),),
            )
            conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (2, 'leewensong', ?)",
                (auth.hash_password("leewensong-pass"),),
            )
        self.wilson = self.headers(1, "wilson")
        self.leewensong = self.headers(2, "leewensong")

    @staticmethod
    def headers(user_id, username):
        return {"Authorization": "Bearer " + auth.create_token(user_id, username)}

    def test_public_room_with_password_still_requires_password_per_account(self):
        created = self.client.post(
            "/api/rooms",
            headers=self.wilson,
            json={"roomName": "Empty3DTest", "visibility": "public", "password": "room-pass"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertTrue(created.json()["hasPassword"])

        listed = self.client.get("/api/rooms/public", headers=self.leewensong)
        self.assertEqual(listed.status_code, 200, listed.text)
        public_room = next(room for room in listed.json()["rooms"] if room["roomName"] == "Empty3DTest")
        self.assertTrue(public_room["hasPassword"])
        self.assertFalse(public_room["joined"])

        for body, expected_detail in (
            ({"roomName": "Empty3DTest"}, "需要房间密码"),
            ({"roomName": "Empty3DTest", "password": "wrong-pass"}, "房间密码错误"),
        ):
            response = self.client.post("/api/rooms", headers=self.leewensong, json=body)
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["detail"], expected_detail)

        with db.get_db() as conn:
            member_count = conn.execute(
                "SELECT COUNT(*) AS count FROM room_members WHERE room_id = "
                "(SELECT id FROM rooms WHERE name = 'Empty3DTest')"
            ).fetchone()["count"]
        self.assertEqual(member_count, 1, "failed password attempts must not create membership")

        joined = self.client.post(
            "/api/rooms",
            headers=self.leewensong,
            json={"roomName": "Empty3DTest", "password": "room-pass"},
        )
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertTrue(joined.json()["joined"])

        open_created = self.client.post(
            "/api/rooms",
            headers=self.wilson,
            json={"roomName": "OpenPublic", "visibility": "public"},
        )
        self.assertEqual(open_created.status_code, 200, open_created.text)
        open_joined = self.client.post(
            "/api/rooms",
            headers=self.leewensong,
            json={"roomName": "OpenPublic"},
        )
        self.assertEqual(open_joined.status_code, 200, open_joined.text)


if __name__ == "__main__":
    unittest.main()
