"""个人资源库基础 API 回归测试。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, main


class LibraryResourceTests(unittest.TestCase):
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
            (auth, "SECRET", b"isolated-library-test-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (1, 'alice', ?)",
                (auth.hash_password("alice-pass"),),
            )
            conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (2, 'bob', ?)",
                (auth.hash_password("bob-pass"),),
            )
            conn.execute(
                "INSERT INTO users (id, username, password_hash, kind, owner_id) VALUES (3, 'alice-agent', '', 'agent', 1)"
            )
        self.alice = self.headers(1, "alice")
        self.bob = self.headers(2, "bob")
        self.agent = self.headers(3, "alice-agent", "agent")

    @staticmethod
    def headers(user_id, username, kind="human"):
        return {"Authorization": "Bearer " + auth.create_token(user_id, username, kind, 0)}

    def test_private_versioned_upload_replace_download_and_archive(self):
        created = self.client.post(
            "/api/library/resources",
            headers=self.alice,
            files={"file": ("notes.md", b"# v1", "text/markdown")},
            data={"description": "private notes"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        resource = created.json()
        self.assertEqual(resource["name"], "notes.md")
        self.assertEqual(resource["currentVersion"]["versionNo"], 1)
        resource_id = resource["id"]
        version1 = resource["currentVersion"]["id"]

        listing = self.client.get("/api/library/resources", headers=self.alice)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([item["id"] for item in listing.json()["resources"]], [resource_id])
        self.assertEqual(listing.json()["quota"]["resourceCount"], 1)

        hidden = self.client.get("/api/library/resources", headers=self.bob)
        self.assertEqual(hidden.status_code, 200, hidden.text)
        self.assertEqual(hidden.json()["resources"], [])

        download = self.client.get(
            f"/api/library/resources/{resource_id}/versions/{version1}/content",
            headers=self.alice,
        )
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, b"# v1")

        forbidden = self.client.get(
            f"/api/library/resources/{resource_id}/versions/{version1}/content",
            headers=self.bob,
        )
        self.assertEqual(forbidden.status_code, 404, forbidden.text)

        duplicate = self.client.post(
            f"/api/library/resources/{resource_id}/versions",
            headers=self.alice,
            files={"file": ("notes.md", b"# v1", "text/markdown")},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.json()["detail"]["code"], "resource_version_conflict")

        replaced = self.client.post(
            f"/api/library/resources/{resource_id}/versions",
            headers=self.alice,
            files={"file": ("notes.md", b"# v2", "text/markdown")},
        )
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["currentVersion"]["versionNo"], 2)
        version2 = replaced.json()["currentVersion"]["id"]
        self.assertNotEqual(version1, version2)

        old_still_readable = self.client.get(
            f"/api/library/resources/{resource_id}/versions/{version1}/content",
            headers=self.alice,
        )
        self.assertEqual(old_still_readable.content, b"# v1")
        current = self.client.get(
            f"/api/library/resources/{resource_id}/versions/{version2}/content",
            headers=self.alice,
        )
        self.assertEqual(current.content, b"# v2")

        renamed = self.client.patch(
            f"/api/library/resources/{resource_id}",
            headers=self.alice,
            json={"name": "meeting-notes", "description": "updated"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "meeting-notes")

        archived = self.client.delete(
            f"/api/library/resources/{resource_id}", headers=self.alice
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertTrue(archived.json()["archived"])
        after_archive = self.client.get(
            f"/api/library/resources/{resource_id}", headers=self.alice
        )
        self.assertEqual(after_archive.status_code, 200, after_archive.text)
        self.assertEqual(after_archive.json()["status"], "archived")

        no_new_version = self.client.post(
            f"/api/library/resources/{resource_id}/versions",
            headers=self.alice,
            files={"file": ("notes.md", b"# v3", "text/markdown")},
        )
        self.assertEqual(no_new_version.status_code, 404, no_new_version.text)

    def test_agent_and_human_share_owner_library(self):
        created = self.client.post(
            "/api/library/resources",
            headers=self.agent,
            files={"file": ("agent-notes.txt", b"agent owned", "text/plain")},
        )
        self.assertEqual(created.status_code, 200, created.text)
        resource = created.json()
        self.assertEqual(resource["ownerAccountId"], 1)

        agent_resources = self.client.get("/api/library/resources", headers=self.agent)
        human_resources = self.client.get("/api/library/resources", headers=self.alice)
        self.assertEqual([item["id"] for item in agent_resources.json()["resources"]], [resource["id"]])
        self.assertEqual([item["id"] for item in human_resources.json()["resources"]], [resource["id"]])

        version_id = resource["currentVersion"]["id"]
        human_download = self.client.get(
            f"/api/library/resources/{resource['id']}/versions/{version_id}/content",
            headers=self.alice,
        )
        self.assertEqual(human_download.status_code, 200, human_download.text)
        self.assertEqual(human_download.content, b"agent owned")

        human_update = self.client.patch(
            f"/api/library/resources/{resource['id']}",
            headers=self.alice,
            json={"description": "managed by owner"},
        )
        self.assertEqual(human_update.status_code, 200, human_update.text)
        self.assertEqual(human_update.json()["description"], "managed by owner")

        agent_friend_write = self.client.post(
            "/api/friends/requests", headers=self.agent, json={"username": "bob"}
        )
        self.assertEqual(agent_friend_write.status_code, 403, agent_friend_write.text)

    def test_signup_welcome_points_once_and_agent_uses_same_wallet(self):
        existing_balance = self.client.get("/api/points", headers=self.alice)
        self.assertEqual(existing_balance.status_code, 200, existing_balance.text)
        self.assertEqual(existing_balance.json()["balance"], 0, "existing users must not receive retroactive signup grants")

        registration = self.client.post(
            "/api/users",
            json={"username": "new-human", "password": "new-human-pass"},
        )
        self.assertEqual(registration.status_code, 200, registration.text)
        new_user_id = registration.json()["userId"]
        self.assertEqual(registration.json()["welcomePoints"], 1000)
        login = self.client.post(
            "/api/login",
            json={"username": "new-human", "password": "new-human-pass"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        human_token = {"Authorization": "Bearer " + login.json()["token"]}
        human_points = self.client.get("/api/points", headers=human_token)
        self.assertEqual(human_points.json()["balance"], 1000)
        ledger = self.client.get("/api/points/transactions", headers=human_token)
        self.assertEqual(ledger.status_code, 200, ledger.text)
        welcome = next(item for item in ledger.json()["transactions"] if item["transactionType"] == "signup_grant")
        self.assertEqual(welcome["amount"], 1000)

        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, kind, owner_id) VALUES ('new-human-agent', '', 'agent', ?)",
                (new_user_id,),
            )
            agent_id = conn.execute("SELECT id FROM users WHERE username='new-human-agent'").fetchone()["id"]
        agent_token = self.headers(agent_id, "new-human-agent", "agent")
        agent_points = self.client.get("/api/points", headers=agent_token)
        self.assertEqual(agent_points.json()["balance"], 1000)

        duplicate = self.client.post(
            "/api/users",
            json={"username": "new-human", "password": "new-human-pass"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        with db.get_db() as conn:
            grant_count = conn.execute(
                "SELECT COUNT(*) AS c FROM point_transactions WHERE transaction_type='signup_grant' AND payee_user_id=?",
                (new_user_id,),
            ).fetchone()["c"]
        self.assertEqual(grant_count, 1)

    def test_migration_moves_legacy_agent_resources_into_owner_namespace(self):
        with db.get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO library_resources (owner_account_id, economic_owner_id, name, description)
                VALUES (3, 1, 'legacy-agent-resource.txt', 'legacy')
                """
            )
            resource_id = cur.lastrowid
        db.init_db()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT owner_account_id, economic_owner_id FROM library_resources WHERE id = ?",
                (resource_id,),
            ).fetchone()
        self.assertEqual((row["owner_account_id"], row["economic_owner_id"]), (1, 1))
        visible_to_owner = self.client.get("/api/library/resources", headers=self.alice)
        self.assertIn(resource_id, {item["id"] for item in visible_to_owner.json()["resources"]})


if __name__ == "__main__":
    unittest.main()
