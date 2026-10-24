"""One-off, operator-authorized legacy welcome grant; defaults to a read-only plan.

Use the project's isolated interpreter. This script never imports the app or runs
application migrations. --apply requires a frozen user-ID cutoff and a fresh
backup directory. Agent accounts never receive separate grants.
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CAMPAIGN_KEY = "legacy-welcome-20261006"
AMOUNT = 1000
TRANSACTION_TYPE = "campaign_grant"
REASON = "旧人类账号欢迎积分一次性补发（用户于 2026-10-06 授权）"


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _plan(conn, cutoff_user_id):
    columns = _columns(conn, "point_transactions")
    required = {"idempotency_key", "payer_user_id", "payee_user_id", "amount", "status", "metadata"}
    if not required.issubset(columns):
        raise ValueError("Unsupported points ledger schema")
    signup_type = " OR (t.transaction_type = 'signup_grant' AND t.status = 'committed')" if "transaction_type" in columns else ""
    rows = conn.execute(
        f"""
        SELECT u.id, COALESCE(a.balance, 0) AS balance, (a.user_id IS NOT NULL) AS had_wallet,
               EXISTS(SELECT 1 FROM point_transactions t
                      WHERE t.payee_user_id = u.id AND
                            (t.idempotency_key = 'signup-welcome:' || u.id {signup_type})) AS signup_received
        FROM users u LEFT JOIN point_accounts a ON a.user_id = u.id
        WHERE u.kind = 'human' AND u.id <= ? ORDER BY u.id
        """,
        (cutoff_user_id,),
    ).fetchall()
    items = []
    for row in rows:
        key = f"campaign:{CAMPAIGN_KEY}:user:{row['id']}"
        prior = conn.execute("SELECT * FROM point_transactions WHERE idempotency_key = ?", (key,)).fetchone()
        if prior:
            metadata = json.loads(prior["metadata"] or "{}")
            if (
                prior["payer_user_id"] != row["id"]
                or prior["payee_user_id"] != row["id"]
                or prior["amount"] != AMOUNT
                or prior["status"] != "committed"
                or metadata.get("campaignKey") != CAMPAIGN_KEY
                or ("transaction_type" in columns and prior["transaction_type"] != TRANSACTION_TYPE)
            ):
                raise ValueError(f"Conflicting ledger entry for user ID {row['id']}")
            disposition = "already_backfilled"
        elif row["signup_received"]:
            disposition = "signup_received"
        else:
            disposition = "eligible"
        items.append({"userId": row["id"], "balanceBefore": row["balance"], "hadWallet": bool(row["had_wallet"]), "key": key, "disposition": disposition})
    eligible = sum(item["disposition"] == "eligible" for item in items)
    return {"campaignKey": CAMPAIGN_KEY, "amountPerHuman": AMOUNT, "cutoffUserId": cutoff_user_id,
            "cohortHumanCount": len(items), "eligibleCount": eligible,
            "alreadyBackfilledCount": sum(item["disposition"] == "already_backfilled" for item in items),
            "signupReceivedCount": sum(item["disposition"] == "signup_received" for item in items),
            "pointsToAdd": eligible * AMOUNT, "items": items}


def _invariants(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("users", "rooms", "room_members", "messages", "room_files") if t in tables}
    counts["agentWallets"] = conn.execute("SELECT COUNT(*) FROM point_accounts a JOIN users u ON u.id=a.user_id WHERE u.kind='agent'").fetchone()[0]
    return counts


def _save_report(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def run(database, cutoff_user_id, *, apply=False, backup_dir=None, expected_eligible=None):
    database = Path(database).resolve(strict=True)
    if cutoff_user_id < 1:
        raise ValueError("A positive frozen user-ID cutoff is required")
    if apply and (backup_dir is None or expected_eligible is None):
        raise ValueError("Apply requires a fresh backup directory and expected eligible count")
    mode = "rw" if apply else "ro"
    conn = sqlite3.connect(database.as_uri() + f"?mode={mode}", uri=True, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    report_path = None
    committed = False
    report = None
    try:
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        plan = _plan(conn, cutoff_user_id)
        if not apply:
            return {k: v for k, v in plan.items() if k != "items"} | {"mode": "dry-run"}
        if plan["eligibleCount"] != expected_eligible:
            raise ValueError(f"Expected {expected_eligible} eligible humans, found {plan['eligibleCount']}; audit again")
        before = _invariants(conn)
        ledger_before = conn.execute("SELECT COUNT(*) FROM point_transactions").fetchone()[0]
        balance_before = conn.execute("SELECT COALESCE(SUM(balance),0) FROM point_accounts").fetchone()[0]
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        backup_dir.chmod(0o700)
        backup_file = backup_dir / database.name
        # The writer reservation prevents another writer from committing while we
        # capture the pre-grant database via a separate read connection (WAL-safe).
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(backup_file)
        try:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
            if destination.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Backup foreign-key check failed")
        finally:
            destination.close()
            source.close()
        backup_file.chmod(0o600)
        report_path = backup_dir / "grant-report.json"
        report = {"database": str(database), "backupDatabase": str(backup_file),
                  "startedAtUtc": datetime.now(timezone.utc).isoformat(), "plan": plan,
                  "beforeCounts": before, "ledgerRowsBefore": ledger_before, "balanceTotalBefore": balance_before,
                  "status": "backed_up"}
        _save_report(report_path, report)
        if "transaction_type" not in _columns(conn, "point_transactions"):
            conn.execute("ALTER TABLE point_transactions ADD COLUMN transaction_type TEXT NOT NULL DEFAULT 'purchase' CHECK (transaction_type IN ('signup_grant','campaign_grant','purchase'))")
        metadata = json.dumps({"campaignKey": CAMPAIGN_KEY, "reason": REASON,
                               "source": "legacy_welcome_backfill", "issuedBy": "server_operator",
                               "authorizedOn": "2026-10-06"}, ensure_ascii=False, sort_keys=True)
        for item in plan["items"]:
            if item["disposition"] != "eligible":
                continue
            conn.execute(
                """INSERT INTO point_transactions (idempotency_key,transaction_type,payer_user_id,payee_user_id,amount,status,metadata)
                   VALUES (?,?,?,?,?,'committed',?)""",
                (item["key"], TRANSACTION_TYPE, item["userId"], item["userId"], AMOUNT, metadata),
            )
            conn.execute(
                """INSERT INTO point_accounts (user_id,balance) VALUES (?,?)
                   ON CONFLICT(user_id) DO UPDATE SET balance=point_accounts.balance+excluded.balance,updated_at=datetime('now')""",
                (item["userId"], AMOUNT),
            )
        for item in plan["items"]:
            row = conn.execute("SELECT balance FROM point_accounts WHERE user_id=?", (item["userId"],)).fetchone()
            expected = item["balanceBefore"] + (AMOUNT if item["disposition"] == "eligible" else 0)
            if (row[0] if row else 0) != expected:
                raise ValueError(f"Balance delta verification failed for user ID {item['userId']}")
            item["balanceAfter"] = expected
        after = _invariants(conn)
        if after != before:
            raise ValueError("Unrelated users/messages/files or Agent wallets changed")
        ledger_after = conn.execute("SELECT COUNT(*) FROM point_transactions").fetchone()[0]
        balance_after = conn.execute("SELECT COALESCE(SUM(balance),0) FROM point_accounts").fetchone()[0]
        if ledger_after != ledger_before + plan["eligibleCount"] or balance_after != balance_before + plan["pointsToAdd"]:
            raise ValueError("Ledger count or total balance verification failed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Post-grant database checks failed")
        conn.commit()
        committed = True
        report.update(status="committed", committedAtUtc=datetime.now(timezone.utc).isoformat(),
                      afterCounts=after, ledgerRowsAfter=ledger_after, balanceTotalAfter=balance_after)
        _save_report(report_path, report)
        return {k: v for k, v in plan.items() if k != "items"} | {
            "mode": "apply", "status": "committed", "backupDir": str(backup_dir),
            "reportPath": str(report_path), "balanceTotalBefore": balance_before, "balanceTotalAfter": balance_after,
            "dbIntegrity": "ok", "fkViolations": 0}
    except Exception as exc:
        if not committed:
            conn.rollback()
            if report_path is not None and report is not None:
                report.update(status="rolled_back", error=str(exc))
                _save_report(report_path, report)
        raise
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--cutoff-user-id", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-eligible", type=int)
    args = parser.parse_args(argv)
    result = run(args.database, args.cutoff_user_id, apply=args.apply,
                 backup_dir=args.backup_dir, expected_eligible=args.expected_eligible)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
