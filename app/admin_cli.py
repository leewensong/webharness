"""服务器本地管理超级管理员：.venv/bin/python -m app.admin_cli grant wilson。

只修改已有账号；不会自动创建用户，也不会因用户名匹配而在注册时授予权限。
部署时应在实际服务目录下执行。Web/API 用户无法调用此运维入口。
"""

import argparse

from .db import DB_PATH, get_db, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="管理平台超级管理员（需服务器本地权限）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出现有超级管理员")
    for command in ("grant", "revoke"):
        sub = commands.add_parser(command, help="授予权限" if command == "grant" else "撤销权限")
        sub.add_argument("username", help="已有的人类账号用户名")
    args = parser.parse_args(argv)
    if not DB_PATH.exists():
        parser.error(f"数据库不存在：{DB_PATH}；请先启动服务并创建账号")
    init_db()
    with get_db() as conn:
        if args.command == "list":
            print(f"数据库：{DB_PATH}")
            for row in conn.execute(
                "SELECT id, username, status FROM users WHERE kind = 'human' AND is_superadmin = 1 ORDER BY id"
            ):
                print(f"{row['id']}\t{row['username']}\t{row['status']}")
            return 0
        row = conn.execute(
            "SELECT id, username, kind, status FROM users WHERE username = ?", (args.username,)
        ).fetchone()
        if not row:
            parser.error("用户不存在；请先核实并创建人类账号")
        if row["kind"] != "human":
            parser.error("只能向人类账号授予平台权限；Agent 不继承此权限")
        if args.command == "grant" and row["status"] != "active":
            parser.error("只能向启用的人类账号授予权限")
        conn.execute("UPDATE users SET is_superadmin = ? WHERE id = ?",
                     (int(args.command == "grant"), row["id"]))
    print(f"{args.command}: {row['username']} (id={row['id']}) — {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
