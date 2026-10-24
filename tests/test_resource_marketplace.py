"""资源审核、商店 listing、积分结算和 room_files 导入回归测试。"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, main


class ResourceMarketplaceTests(unittest.TestCase):
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
            (auth, "SECRET", b"isolated-resource-marketplace-test-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        with db.get_db() as conn:
            for user_id, username, is_superadmin in (
                (1, "alice", 0),
                (2, "bob", 0),
                (3, "admin", 1),
            ):
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, is_superadmin) VALUES (?, ?, ?, ?)",
                    (user_id, username, auth.hash_password(username + "-pass"), is_superadmin),
                )
            conn.execute(
                "INSERT INTO users (id, username, kind, owner_id) VALUES (4, 'bob-agent', 'agent', 2)"
            )
        self.alice = self.headers(1, "alice")
        self.bob = self.headers(2, "bob")
        self.admin = self.headers(3, "admin")
        self.bob_agent = self.headers(4, "bob-agent", "agent")

    @staticmethod
    def headers(user_id, username, kind="human"):
        return {"Authorization": "Bearer " + auth.create_token(user_id, username, kind, 0)}

    def test_review_listing_payment_and_room_snapshot_are_atomic_and_idempotent(self):
        uploaded = self.client.post(
            "/api/library/resources",
            headers=self.alice,
            files={"file": ("shared.md", b"hello store", "text/markdown")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        resource = uploaded.json()
        resource_id = resource["id"]
        version_id = resource["currentVersion"]["id"]

        submitted = self.client.post(
            f"/api/library/resources/{resource_id}/versions/{version_id}/submit",
            headers=self.alice,
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        queue = self.client.get("/api/admin/resource-reviews", headers=self.admin)
        self.assertEqual(queue.status_code, 200, queue.text)
        self.assertEqual(queue.json()["reviews"][0]["id"], version_id)

        approved = self.client.post(
            f"/api/admin/resource-reviews/{version_id}",
            headers=self.admin,
            json={"decision": "approved", "note": "ok"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        listing = self.client.post(
            f"/api/library/resources/{resource_id}/versions/{version_id}/listing",
            headers=self.alice,
            json={"pricePoints": 10},
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        listing_id = listing.json()["listing"]["id"]

        store = self.client.get("/api/store/resources", headers=self.bob)
        self.assertEqual(store.status_code, 200, store.text)
        self.assertEqual(store.json()["resources"][0]["listingId"], listing_id)
        self.assertNotIn("contentUrl", store.json()["resources"][0])

        created_room = self.client.post(
            "/api/rooms",
            headers=self.alice,
            json={"roomName": "MarketRoom", "visibility": "public"},
        )
        self.assertEqual(created_room.status_code, 200, created_room.text)
        room_id = created_room.json()["roomId"]
        joined = self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomId": room_id, "roomName": "MarketRoom"},
        )
        self.assertEqual(joined.status_code, 200, joined.text)

        with db.get_db() as conn:
            conn.execute("INSERT INTO point_accounts (user_id, balance) VALUES (2, 20)")

        import_body = {
            "resourceId": resource_id,
            "resourceVersionId": version_id,
            "listingId": listing_id,
            "name": "shared-in-room.md",
            "idempotencyKey": "purchase-1",
        }
        imported = self.client.post(
            f"/api/rooms/{room_id}/files/from-library",
            headers=self.bob,
            json=import_body,
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertTrue(imported.json()["charged"])
        file_id = imported.json()["file"]["id"]
        self.assertEqual(imported.json()["file"]["source"]["resourceId"], resource_id)
        self.assertEqual(imported.json()["file"]["source"]["licenseId"], imported.json()["licenseId"])

        with db.get_db() as conn:
            balances = conn.execute("SELECT user_id, balance FROM point_accounts ORDER BY user_id").fetchall()
        self.assertEqual([(row["user_id"], row["balance"]) for row in balances], [(1, 10), (2, 10)])

        repeated = self.client.post(
            f"/api/rooms/{room_id}/files/from-library",
            headers=self.bob,
            json=import_body,
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertTrue(repeated.json()["idempotent"])
        self.assertEqual(repeated.json()["fileId"], file_id)

        with db.get_db() as conn:
            transaction_count = conn.execute("SELECT COUNT(*) AS c FROM point_transactions").fetchone()["c"]
        self.assertEqual(transaction_count, 1)

        store_unlisted = self.client.delete(
            f"/api/library/listings/{listing_id}", headers=self.alice
        )
        self.assertEqual(store_unlisted.status_code, 200, store_unlisted.text)
        existing_file_still_readable = self.client.get(
            f"/api/rooms/{room_id}/files/{file_id}/content", headers=self.bob
        )
        self.assertEqual(existing_file_still_readable.content, b"hello store")

        deleted = self.client.delete(
            f"/api/rooms/{room_id}/files/{file_id}", headers=self.bob
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        with db.get_db() as conn:
            license_row = conn.execute(
                "SELECT room_file_id FROM room_resource_licenses WHERE id = ?",
                (imported.json()["licenseId"],),
            ).fetchone()
        self.assertIsNone(license_row["room_file_id"])

    def test_insufficient_points_does_not_create_file_or_transaction(self):
        uploaded = self.client.post(
            "/api/library/resources",
            headers=self.alice,
            files={"file": ("paid.txt", b"paid", "text/plain")},
        )
        resource = uploaded.json()
        resource_id = resource["id"]
        version_id = resource["currentVersion"]["id"]
        self.client.post(
            f"/api/library/resources/{resource_id}/versions/{version_id}/submit",
            headers=self.alice,
        )
        self.client.post(
            f"/api/admin/resource-reviews/{version_id}",
            headers=self.admin,
            json={"decision": "approved"},
        )
        listing = self.client.post(
            f"/api/library/resources/{resource_id}/versions/{version_id}/listing",
            headers=self.alice,
            json={"pricePoints": 5},
        ).json()["listing"]
        room = self.client.post(
            "/api/rooms",
            headers=self.alice,
            json={"roomName": "NoMoney", "visibility": "public"},
        ).json()
        self.client.post(
            "/api/rooms",
            headers=self.bob,
            json={"roomId": room["roomId"], "roomName": "NoMoney"},
        )
        response = self.client.post(
            f"/api/rooms/{room['roomId']}/files/from-library",
            headers=self.bob,
            json={
                "resourceId": resource_id,
                "resourceVersionId": version_id,
                "listingId": listing["id"],
                "idempotencyKey": "no-money",
            },
        )
        self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(response.json()["detail"]["code"], "insufficient_points")
        with db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM point_transactions").fetchone()["c"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM room_files").fetchone()["c"], 0)

    def test_campaign_grants_are_human_only_and_idempotent(self):
        first = self.client.post(
            "/api/admin/points/grants",
            headers=self.admin,
            json={"username": "bob", "campaignKey": "launch-2026", "amount": 250, "reason": "Launch activity"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["idempotent"])
        self.assertEqual(first.json()["balance"], 250)

        repeated = self.client.post(
            "/api/admin/points/grants",
            headers=self.admin,
            json={"username": "bob", "campaignKey": "launch-2026", "amount": 250, "reason": "Launch activity"},
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertTrue(repeated.json()["idempotent"])
        self.assertEqual(repeated.json()["balance"], 250)

        changed = self.client.post(
            "/api/admin/points/grants",
            headers=self.admin,
            json={"username": "bob", "campaignKey": "launch-2026", "amount": 300, "reason": "Changed amount"},
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["detail"]["code"], "points_idempotency_conflict")

        agent_denied = self.client.post(
            "/api/admin/points/grants",
            headers=self.admin,
            json={"username": "bob-agent", "campaignKey": "agent-event", "amount": 10, "reason": "Must target humans"},
        )
        self.assertEqual(agent_denied.status_code, 404, agent_denied.text)

        history = self.client.get("/api/points/transactions", headers=self.bob_agent)
        self.assertEqual(history.status_code, 200, history.text)
        grant = next(item for item in history.json()["transactions"] if item["transactionType"] == "campaign_grant")
        self.assertEqual(grant["metadata"]["campaignKey"], "launch-2026")


if __name__ == "__main__":
    unittest.main()


class LegacyPointsMigrationTests(unittest.TestCase):
    def test_old_point_transactions_get_purchase_type_without_balance_grants(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, value in (
            ("DATA_DIR", root),
            ("DB_PATH", root / "legacy-points.db"),
            ("UPLOADS_DIR", root / "uploads"),
            ("FILES_DIR", root / "files"),
            ("LIBRARY_DIR", root / "library"),
        ):
            self.enterContext(patch.object(db, name, value))
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO users (id, username) VALUES (1, 'legacy-human');
                CREATE TABLE point_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payer_user_id INTEGER NOT NULL,
                    payee_user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    resource_id INTEGER,
                    resource_version_id INTEGER,
                    room_id INTEGER,
                    license_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                INSERT INTO point_transactions (idempotency_key, payer_user_id, payee_user_id, amount, status)
                VALUES ('legacy-purchase', 1, 1, 0, 'committed');
                """
            )
        db.init_db()
        db.init_db()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT transaction_type FROM point_transactions WHERE idempotency_key='legacy-purchase'"
            ).fetchone()
            self.assertEqual(row["transaction_type"], "purchase")
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM point_accounts").fetchone()["c"], 0)
