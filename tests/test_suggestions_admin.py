"""建议 / 平台权限回归。仅使用临时 SQLite，不修改真实账户或服务数据。"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import admin_cli, auth, db, main


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for module, name, value in (
            (db, "DATA_DIR", root), (db, "DB_PATH", root / "test.db"),
            (db, "UPLOADS_DIR", root / "uploads"), (db, "FILES_DIR", root / "files"),
            (main, "UPLOADS_DIR", root / "uploads"), (main, "FILES_DIR", root / "files"),
            (admin_cli, "DB_PATH", root / "test.db"), (auth, "SECRET", b"isolated-test-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        with db.get_db() as conn:
            conn.execute("INSERT INTO users (id, username, password_hash, is_superadmin) VALUES (1, 'wilson', ?, 1)",
                         (auth.hash_password("testpass"),))
            conn.execute("INSERT INTO users (id, username) VALUES (2, 'alice')")
            conn.execute("INSERT INTO users (id, username, kind, owner_id, public_key) VALUES (3, 'wilson-bot', 'agent', 1, ?)", (key,))
        self.admin = self.headers(1, "wilson")
        self.human = self.headers(2, "alice")
        self.agent = self.headers(3, "wilson-bot", "agent")

    @staticmethod
    def headers(user_id, username, kind="human"):
        return {"Authorization": "Bearer " + auth.create_token(user_id, username, kind)}

    def submit(self, headers=None, **body):
        response = self.client.post("/api/suggestions", headers=headers or self.human,
                                    json={"content": "A useful suggestion", **body})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        return response.json()["id"]

    def _new_agent_public_key(self):
        key = Ed25519PrivateKey.generate().public_key()
        return key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def test_create_agent_can_atomically_create_same_name_room(self):
        name = "room-agent"
        response = self.client.post(
            "/api/agents", headers=self.admin,
            json={"username": name, "publicKey": self._new_agent_public_key(), "createRoom": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["username"], name)
        self.assertIn("room", data)
        room = data["room"]
        self.assertEqual(
            (room["roomName"], room["roomAgent"], room["ownerName"], room["visibility"], room["created"], room["joined"]),
            (name, name, "wilson", "private", True, True),
        )
        self.assertTrue(room["isOwner"])
        self.assertTrue(room["canManage"])
        with db.get_db() as conn:
            agent = conn.execute(
                "SELECT id, kind, owner_id FROM users WHERE username = ?", (name,)
            ).fetchone()
            stored_room = conn.execute(
                "SELECT created_by, room_agent_id, visibility, password_hash FROM rooms WHERE name = ?",
                (name,),
            ).fetchone()
            member = conn.execute(
                "SELECT 1 FROM room_members WHERE room_id = (SELECT id FROM rooms WHERE name = ?) AND user_id = 1",
                (name,),
            ).fetchone()
        self.assertEqual((agent["kind"], agent["owner_id"]), ("agent", 1))
        self.assertEqual((stored_room["created_by"], stored_room["visibility"], stored_room["password_hash"]), (1, "private", None))
        self.assertEqual(stored_room["room_agent_id"], agent["id"])
        self.assertIsNotNone(member)

    def test_create_agent_without_option_does_not_create_room(self):
        name = "plain-agent"
        response = self.client.post(
            "/api/agents", headers=self.admin,
            json={"username": name, "publicKey": self._new_agent_public_key()},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("room", response.json())
        with db.get_db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM rooms WHERE name = ?", (name,)).fetchone())

    def test_same_name_room_conflict_rolls_back_agent_creation(self):
        name = "room-conflict"
        room_response = self.client.post("/api/rooms", headers=self.admin, json={"roomName": name})
        self.assertEqual(room_response.status_code, 200, room_response.text)
        response = self.client.post(
            "/api/agents", headers=self.admin,
            json={"username": name, "publicKey": self._new_agent_public_key(), "createRoom": True},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("同名房间", response.text)
        with db.get_db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE username = ? AND kind = 'agent'", (name,)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM rooms WHERE name = ?", (name,)).fetchone())

    def test_legacy_submission_and_new_categories(self):
        human_id = self.submit(content="  legacy content  ", contact="   ")
        agent_id = self.submit(self.agent, category="skill", content="missing argument", contact="owner@example.com")
        human = self.client.get(f"/api/admin/suggestions/{human_id}", headers=self.admin).json()
        agent = self.client.get(f"/api/admin/suggestions/{agent_id}", headers=self.admin).json()
        self.assertEqual((human["category"], human["status"], human["content"], human["contact"]),
                         ("other", "new", "legacy content", None))
        self.assertEqual((agent["kind"], agent["username"], agent["category"]), ("agent", "wilson-bot", "skill"))
        for category in ("bug", "feature", "other"):
            self.submit(category=category)

    def test_auth_required_and_all_admin_routes_guarded(self):
        item_id = self.submit()
        routes = [("GET", "/api/admin", None), ("GET", "/api/admin/suggestions", None),
                  ("GET", f"/api/admin/suggestions/{item_id}", None),
                  ("PATCH", f"/api/admin/suggestions/{item_id}", {"status": "planned"})]
        self.assertEqual(self.client.post("/api/suggestions", json={"content": "x"}).status_code, 401)
        for method, path, body in routes:
            for headers, status in (({}, 401), (self.human, 403), (self.agent, 403)):
                with self.subTest(method=method, path=path, headers=headers):
                    response = self.client.request(method, path, headers=headers, json=body)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertNotIn("A useful suggestion", response.text)

    def test_self_capabilities_and_normal_login(self):
        login = self.client.post("/api/login", json={"username": "wilson", "password": "testpass"})
        self.assertEqual(login.status_code, 200)
        me = self.client.get("/api/me", headers={"Authorization": "Bearer " + login.json()["token"]}).json()
        self.assertTrue(me["isSuperadmin"])
        self.assertEqual(me["adminCapabilities"], ["suggestions.read", "suggestions.manage"])
        for headers in (self.human, self.agent):
            me = self.client.get("/api/me", headers=headers).json()
            self.assertFalse(me["isSuperadmin"])
            self.assertEqual(me["adminCapabilities"], [])

    def test_role_changes_take_effect_for_existing_tokens(self):
        self.assertEqual(self.client.get("/api/admin", headers=self.human).status_code, 403)
        with contextlib.redirect_stdout(io.StringIO()):
            admin_cli.main(["grant", "alice"])
        self.assertEqual(self.client.get("/api/admin", headers=self.human).status_code, 200)
        with contextlib.redirect_stdout(io.StringIO()):
            admin_cli.main(["revoke", "alice"])
        self.assertEqual(self.client.get("/api/admin", headers=self.human).status_code, 403)
        self.assertFalse(self.client.get("/api/me", headers=self.human).json()["isSuperadmin"])

    def test_disabled_or_epoch_invalidated_admin_is_rejected(self):
        with db.get_db() as conn:
            conn.execute("UPDATE users SET status = 'disabled' WHERE id = 1")
        self.assertEqual(self.client.get("/api/admin", headers=self.admin).status_code, 401)
        with db.get_db() as conn:
            conn.execute("UPDATE users SET status = 'active', token_epoch = 1 WHERE id = 1")
        self.assertEqual(self.client.get("/api/admin", headers=self.admin).status_code, 401)

    def test_no_username_or_owner_or_token_kind_privilege(self):
        # 名字 / token 里的快照不构成权限；即使 Agent 的数据库标志被误设也不获权。
        with db.get_db() as conn:
            conn.execute("UPDATE users SET is_superadmin = 0 WHERE id = 1")
            conn.execute("UPDATE users SET is_superadmin = 1 WHERE id = 3")
        self.assertEqual(self.client.get("/api/admin", headers=self.admin).status_code, 403)
        spoofed = self.headers(3, "wilson", "human")
        self.assertEqual(self.client.get("/api/admin", headers=spoofed).status_code, 403)
        me = self.client.get("/api/me", headers=spoofed).json()
        self.assertEqual((me["username"], me["kind"], me["isSuperadmin"]), ("wilson-bot", "agent", False))
        item_id = self.submit(spoofed)
        item = self.client.get(f"/api/admin/suggestions/{item_id}", headers=self.human)
        self.assertEqual(item.status_code, 403)
        with db.get_db() as conn:
            row = conn.execute("SELECT kind, username FROM suggestions WHERE id = ?", (item_id,)).fetchone()
        self.assertEqual(tuple(row), ("agent", "wilson-bot"))

    def test_room_governance_does_not_grant_platform_access(self):
        response = self.client.post("/api/rooms", headers=self.admin,
                                    json={"roomName": "private-test", "roomAgent": "wilson-bot"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/admin", headers=self.agent).status_code, 403)
        # 另一个普通房主同样没有平台权限。
        self.assertEqual(self.client.post("/api/rooms", headers=self.human, json={"roomName": "alice-private"}).status_code, 200)
        self.assertEqual(self.client.get("/api/admin", headers=self.human).status_code, 403)
        # 平台管理员也不自动绕过现有私有房成员权限。
        self.assertEqual(self.client.get("/api/rooms/alice-private", headers=self.admin).status_code, 403)

    def test_pagination_filters_counts_and_cache_policy(self):
        ids = [self.submit(self.agent if n % 2 else self.human,
                           category="skill" if n % 2 else "bug", content=f"item {n}") for n in range(5)]
        first = self.client.get("/api/admin/suggestions?limit=2", headers=self.admin)
        self.assertEqual(first.headers.get("cache-control"), "no-store")
        data = first.json()
        self.assertEqual([item["id"] for item in data["suggestions"]], ids[::-1][:2])
        self.assertEqual(data["total"], 5)
        second = self.client.get(f"/api/admin/suggestions?limit=2&beforeId={data['nextBeforeId']}", headers=self.admin).json()
        third = self.client.get(f"/api/admin/suggestions?limit=2&beforeId={second['nextBeforeId']}", headers=self.admin).json()
        self.assertEqual([x["id"] for x in data["suggestions"] + second["suggestions"] + third["suggestions"]], ids[::-1])
        self.assertIsNone(third["nextBeforeId"])
        filtered = self.client.get("/api/admin/suggestions?kind=agent&category=skill&status=new", headers=self.admin).json()
        self.assertEqual(filtered["total"], 2)
        self.assertTrue(all(x["kind"] == "agent" and x["category"] == "skill" for x in filtered["suggestions"]))
        overview = self.client.get("/api/admin", headers=self.admin).json()
        self.assertEqual(overview["totalSuggestions"], 5)
        self.assertEqual(overview["suggestionCounts"]["new"], 5)

    def test_review_preserves_original_and_tracks_reviewer(self):
        item_id = self.submit(self.agent, category="skill", content="original", contact="contact")
        response = self.client.patch(f"/api/admin/suggestions/{item_id}", headers=self.admin,
                                     json={"status": "planned", "adminNote": "  fix docs  "})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data["status"], data["adminNote"], data["updatedBy"]), ("planned", "fix docs", "wilson"))
        self.assertIsNotNone(data["updatedAt"])
        self.assertEqual((data["content"], data["kind"], data["username"], data["contact"]),
                         ("original", "agent", "wilson-bot", "contact"))
        for status in main.SUGGESTION_STATUSES:
            self.assertEqual(self.client.patch(f"/api/admin/suggestions/{item_id}", headers=self.admin,
                                               json={"status": status}).status_code, 200)
        cleared = self.client.patch(f"/api/admin/suggestions/{item_id}", headers=self.admin, json={"adminNote": ""}).json()
        self.assertEqual(cleared["adminNote"], "")
        self.assertEqual(cleared["status"], "rejected")
        self.assertEqual(self.client.get("/api/admin?unused=1", headers=self.admin).json()["suggestionCounts"]["rejected"], 1)
        self.assertEqual(self.client.get("/api/admin/suggestions?status=new", headers=self.admin).json()["total"], 0)

    def test_validation_and_not_found(self):
        for body in ({"content": ""}, {"content": "  "}, {"content": "x" * 5001},
                     {"content": "x", "category": "admin"}, {"content": "x", "contact": "x" * 201}):
            with self.subTest(body=str(body)[:80]):
                self.assertEqual(self.client.post("/api/suggestions", headers=self.human, json=body).status_code, 422)
        item_id = self.submit()
        for body in ({}, {"status": "unknown"}, {"adminNote": "x" * 5001}, {"status": None}, {"adminNote": None}):
            self.assertEqual(self.client.patch(f"/api/admin/suggestions/{item_id}", headers=self.admin, json=body).status_code, 422)
        for query in ("limit=0", "limit=101", "beforeId=0", "kind=other", "status=unknown", "category=admin"):
            self.assertEqual(self.client.get("/api/admin/suggestions?" + query, headers=self.admin).status_code, 422)
        self.assertEqual(self.client.get("/api/admin/suggestions/99999", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.patch("/api/admin/suggestions/99999", headers=self.admin, json={"status": "reviewing"}).status_code, 404)

    def test_client_cannot_self_promote_or_forge_submission_fields(self):
        response = self.client.post("/api/users", json={"username": "new-wilson", "password": "pass",
                                                       "is_superadmin": 1, "isSuperadmin": True})
        self.assertEqual(response.status_code, 200, response.text)
        login = self.client.post("/api/login", json={"username": "new-wilson", "password": "pass"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        self.assertFalse(self.client.get("/api/me", headers=headers).json()["isSuperadmin"])
        self.assertEqual(self.client.get("/api/admin", headers=headers).status_code, 403)
        item_id = self.submit(self.agent, username="wilson", kind="human", status="resolved", adminNote="fake")
        item = self.client.get(f"/api/admin/suggestions/{item_id}", headers=self.admin).json()
        self.assertEqual((item["username"], item["kind"], item["status"], item["adminNote"]),
                         ("wilson-bot", "agent", "new", ""))

    def test_cli_only_existing_active_human(self):
        for username in ("missing", "wilson-bot"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                admin_cli.main(["grant", username])
        with db.get_db() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=2")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            admin_cli.main(["grant", "alice"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            admin_cli.main(["list"])
        self.assertIn("wilson", output.getvalue())
        self.assertNotIn("wilson-bot", output.getvalue())
        with db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)

    def test_skill_is_served_with_real_origin_and_feedback_contract(self):
        response = self.client.get("/skill.md")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("{{BASE_URL}}", response.text)
        self.assertIn("http://testserver/skill.md", response.text)
        self.assertIn("/api/suggestions", response.text)
        self.assertIn('"category":"skill"', response.text)


class LegacyMigrationTests(unittest.TestCase):
    def test_old_database_retains_feedback_and_never_auto_grants_wilson(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, value in (("DATA_DIR", root), ("DB_PATH", root / "old.db"),
                            ("UPLOADS_DIR", root / "uploads"), ("FILES_DIR", root / "files")):
            self.enterContext(patch.object(db, name, value))
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                                    password_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')));
                CREATE TABLE suggestions (id INTEGER PRIMARY KEY, content TEXT NOT NULL, contact TEXT,
                                          kind TEXT NOT NULL DEFAULT 'human', username TEXT NOT NULL,
                                          created_at TEXT NOT NULL DEFAULT (datetime('now')));
                INSERT INTO users (id, username) VALUES (1, 'wilson');
                INSERT INTO suggestions (id, content, contact, username) VALUES (1, 'old feedback', 'old@example.com', 'wilson');
            """)
        db.init_db()
        db.init_db()  # 迁移可重复运行。
        with db.get_db() as conn:
            row = conn.execute("SELECT * FROM suggestions WHERE id=1").fetchone()
            self.assertEqual((row["content"], row["contact"], row["category"], row["status"], row["admin_note"]),
                             ("old feedback", "old@example.com", "other", "new", ""))
            self.assertEqual(conn.execute("SELECT is_superadmin FROM users WHERE id=1").fetchone()[0], 0)
            conn.execute("UPDATE users SET is_superadmin=1 WHERE id=1")
        db.init_db()
        with db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT is_superadmin FROM users WHERE id=1").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
