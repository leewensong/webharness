"""Legacy points backfill tests use only disposable SQLite databases."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import backfill_welcome_points as backfill


class LegacyWelcomeBackfillTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.database = self.root / "webharness.db"
        conn = sqlite3.connect(self.database)
        try:
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE users (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                    owner_id INTEGER REFERENCES users(id), status TEXT NOT NULL DEFAULT 'active');
                CREATE TABLE point_accounts (user_id INTEGER PRIMARY KEY REFERENCES users(id),
                    balance INTEGER NOT NULL CHECK(balance>=0), updated_at TEXT);
                CREATE TABLE point_transactions (id INTEGER PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payer_user_id INTEGER NOT NULL REFERENCES users(id),
                    payee_user_id INTEGER NOT NULL REFERENCES users(id),
                    amount INTEGER NOT NULL, status TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}');
                INSERT INTO users (id,kind,status) VALUES (1,'human','active'), (2,'human','disabled'),
                    (4,'human','active'), (5,'human','active');
                INSERT INTO users (id,kind,owner_id) VALUES (3,'agent',1);
                INSERT INTO point_accounts (user_id,balance) VALUES (1,73), (5,1000);
                INSERT INTO point_transactions (idempotency_key,payer_user_id,payee_user_id,amount,status)
                VALUES ('signup-welcome:5',5,5,1000,'committed');
                """
            )
            conn.commit()
        finally:
            conn.close()

    def balances(self):
        conn = sqlite3.connect(self.database)
        try:
            return dict(conn.execute("SELECT user_id,balance FROM point_accounts"))
        finally:
            conn.close()

    def test_plan_is_read_only_including_missing_transaction_type(self):
        result = backfill.run(self.database, 5)
        self.assertEqual(result["eligibleCount"], 3)
        self.assertEqual(result["pointsToAdd"], 3000)
        self.assertEqual(result["signupReceivedCount"], 1)
        self.assertEqual(self.balances(), {1: 73, 5: 1000})
        conn = sqlite3.connect(self.database)
        try:
            self.assertNotIn("transaction_type", {r[1] for r in conn.execute("PRAGMA table_info(point_transactions)")})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM point_transactions").fetchone()[0], 1)
        finally:
            conn.close()

    def test_legacy_schema_additive_wallets_frozen_cohort_backup_and_retry(self):
        result = backfill.run(self.database, 2, apply=True, backup_dir=self.root / "backup1", expected_eligible=2)
        self.assertEqual(result["pointsToAdd"], 2000)
        self.assertEqual(self.balances(), {1: 1073, 2: 1000, 5: 1000})
        backup = sqlite3.connect(self.root / "backup1" / "webharness.db")
        try:
            self.assertEqual(dict(backup.execute("SELECT user_id,balance FROM point_accounts")), {1: 73, 5: 1000})
        finally:
            backup.close()
        conn = sqlite3.connect(self.database)
        try:
            grants = conn.execute("SELECT transaction_type,amount,metadata FROM point_transactions WHERE transaction_type='campaign_grant'").fetchall()
            self.assertEqual(len(grants), 2)
            self.assertTrue(all(row[1] == 1000 and json.loads(row[2])["campaignKey"] == backfill.CAMPAIGN_KEY for row in grants))
            self.assertEqual(conn.execute("SELECT transaction_type FROM point_transactions WHERE idempotency_key='signup-welcome:5'").fetchone()[0], "purchase")
        finally:
            conn.close()
        retried = backfill.run(self.database, 2, apply=True, backup_dir=self.root / "backup2", expected_eligible=0)
        self.assertEqual(retried["pointsToAdd"], 0)
        self.assertEqual(retried["alreadyBackfilledCount"], 2)
        self.assertEqual(self.balances(), {1: 1073, 2: 1000, 5: 1000})
        self.assertEqual(json.loads((self.root / "backup1" / "grant-report.json").read_text())["status"], "committed")

    def test_new_schema_signup_grants_with_alternate_key_are_skipped(self):
        conn = sqlite3.connect(self.database)
        try:
            conn.execute("ALTER TABLE point_transactions ADD COLUMN transaction_type TEXT NOT NULL DEFAULT 'purchase'")
            conn.execute("UPDATE point_transactions SET transaction_type='signup_grant', idempotency_key='historical-signup' WHERE payee_user_id=5")
            conn.commit()
        finally:
            conn.close()
        result = backfill.run(self.database, 5, apply=True, backup_dir=self.root / "backup", expected_eligible=3)
        self.assertEqual(result["signupReceivedCount"], 1)
        self.assertEqual(self.balances(), {1: 1073, 2: 1000, 4: 1000, 5: 1000})
        self.assertNotIn(3, self.balances(), "Agent must not get a separate wallet")

    def test_failed_wallet_write_rolls_back_all_grants_and_schema(self):
        conn = sqlite3.connect(self.database)
        try:
            conn.executescript("""
                CREATE TRIGGER reject_wallet BEFORE INSERT ON point_accounts
                WHEN NEW.user_id=2 BEGIN SELECT RAISE(ABORT,'simulated failure'); END;
            """)
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            backfill.run(self.database, 2, apply=True, backup_dir=self.root / "backup", expected_eligible=2)
        self.assertEqual(self.balances(), {1: 73, 5: 1000})
        conn = sqlite3.connect(self.database)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM point_transactions").fetchone()[0], 1)
            self.assertNotIn("transaction_type", {r[1] for r in conn.execute("PRAGMA table_info(point_transactions)")})
        finally:
            conn.close()
        self.assertEqual(json.loads((self.root / "backup" / "grant-report.json").read_text())["status"], "rolled_back")

    def test_conflicting_existing_key_aborts_without_balance_change(self):
        conn = sqlite3.connect(self.database)
        try:
            conn.execute("INSERT INTO point_transactions (idempotency_key,payer_user_id,payee_user_id,amount,status) VALUES (?,1,1,12,'committed')", (f"campaign:{backfill.CAMPAIGN_KEY}:user:1",))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(ValueError, "Conflicting ledger"):
            backfill.run(self.database, 2, apply=True, backup_dir=self.root / "backup", expected_eligible=2)
        self.assertEqual(self.balances(), {1: 73, 5: 1000})

    def test_apply_requires_backup_and_matching_audited_count(self):
        with self.assertRaises(ValueError):
            backfill.run(self.database, 2, apply=True)
        with self.assertRaisesRegex(ValueError, "Expected 3"):
            backfill.run(self.database, 2, apply=True, backup_dir=self.root / "backup", expected_eligible=3)
        self.assertEqual(self.balances(), {1: 73, 5: 1000})
        self.assertFalse((self.root / "backup").exists())


if __name__ == "__main__":
    unittest.main()
