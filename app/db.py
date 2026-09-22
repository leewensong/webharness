import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_NEW_DB = DATA_DIR / "webharness.db"
_OLD_DB = DATA_DIR / "chatroom.db"
DB_PATH = _OLD_DB if _OLD_DB.exists() and not _NEW_DB.exists() else _NEW_DB
UPLOADS_DIR = DATA_DIR / "uploads"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> bool:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        return True
    return False


def _rebuild_rooms_if_name_globally_unique(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='rooms'"
    ).fetchone()
    sql = (row["sql"] if row else "") or ""
    if "name TEXT NOT NULL UNIQUE" not in sql:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE rooms_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            password_hash TEXT,
            visibility TEXT NOT NULL DEFAULT 'private',
            muted INTEGER NOT NULL DEFAULT 0,
            ended_at TEXT,
            archived_at TEXT,
            FOREIGN KEY (created_by) REFERENCES users(id)
        );
        INSERT INTO rooms_new (
            id, name, created_by, created_at, password_hash, visibility, muted, ended_at, archived_at
        )
        SELECT id, name, created_by, created_at, password_hash, visibility, muted, ended_at, archived_at
        FROM rooms;
        DROP TABLE rooms;
        ALTER TABLE rooms_new RENAME TO rooms;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


def init_db() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'human',
                owner_id INTEGER REFERENCES users(id),
                public_key TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                avatar BLOB,
                avatar_mime TEXT,
                avatar_updated_at TEXT,
                model3d BLOB,
                model3d_mime TEXT,
                model3d_url TEXT,
                model3d_arkit INTEGER NOT NULL DEFAULT 0,
                model3d_humanoid INTEGER NOT NULL DEFAULT 0,
                model3d_updated_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS agent_challenges (
                nonce TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                password_hash TEXT,
                visibility TEXT NOT NULL DEFAULT 'private',
                muted INTEGER NOT NULL DEFAULT 0,
                ended_at TEXT,
                archived_at TEXT,
                rules TEXT,
                room_agent_id INTEGER REFERENCES users(id),
                FOREIGN KEY (created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS room_members (
                room_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                can_speak INTEGER NOT NULL DEFAULT 1,
                can_upload INTEGER NOT NULL DEFAULT 1,
                can_view_history INTEGER NOT NULL DEFAULT 1,
                first_visible_msg_id INTEGER NOT NULL DEFAULT 0,
                last_read_msg_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, user_id),
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                msg_type TEXT NOT NULL DEFAULT 'text',
                attachment_name TEXT,
                attachment_path TEXT,
                whisper_to INTEGER REFERENCES users(id),
                whisper_to_ids TEXT,
                reply_to INTEGER REFERENCES messages(id),
                recalled INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                streaming INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                contact TEXT,
                kind TEXT NOT NULL DEFAULT 'human',
                username TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS whisper_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                list_type TEXT NOT NULL CHECK (list_type IN ('allow', 'deny')),
                priority INTEGER NOT NULL DEFAULT 0,
                sender TEXT NOT NULL DEFAULT '*',
                receiver TEXT NOT NULL DEFAULT '*',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                UNIQUE (room_id, list_type, sender, receiver)
            );

            CREATE TABLE IF NOT EXISTS room_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                FOREIGN KEY (created_by) REFERENCES users(id),
                UNIQUE (room_id, name)
            );

            CREATE TABLE IF NOT EXISTS room_group_members (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES room_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS room_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                rules TEXT NOT NULL DEFAULT '',
                params TEXT NOT NULL DEFAULT '{}',
                script_name TEXT,
                script_data BLOB,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_messages_room_id
                ON messages(room_id, id DESC);
            """
        )

        # 旧库增量迁移：先加列，再回填，最后建依赖新列的索引
        _add_column_if_missing(conn, "users", "kind", "TEXT NOT NULL DEFAULT 'human'")
        _add_column_if_missing(conn, "users", "owner_id", "INTEGER REFERENCES users(id)")
        _add_column_if_missing(conn, "users", "public_key", "TEXT")
        _add_column_if_missing(conn, "users", "status", "TEXT NOT NULL DEFAULT 'active'")
        _add_column_if_missing(conn, "users", "avatar", "BLOB")
        _add_column_if_missing(conn, "users", "avatar_mime", "TEXT")
        _add_column_if_missing(conn, "users", "avatar_updated_at", "TEXT")
        _add_column_if_missing(conn, "users", "model3d", "BLOB")
        _add_column_if_missing(conn, "users", "model3d_mime", "TEXT")
        _add_column_if_missing(conn, "users", "model3d_url", "TEXT")
        _add_column_if_missing(conn, "users", "model3d_arkit", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "users", "model3d_humanoid", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "users", "model3d_updated_at", "TEXT")
        _add_column_if_missing(conn, "rooms", "password_hash", "TEXT")
        _add_column_if_missing(conn, "rooms", "visibility", "TEXT NOT NULL DEFAULT 'private'")
        _add_column_if_missing(conn, "rooms", "muted", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "rooms", "ended_at", "TEXT")
        _add_column_if_missing(conn, "rooms", "archived_at", "TEXT")
        conn.execute(
            """
            UPDATE rooms SET archived_at = ended_at
            WHERE ended_at IS NOT NULL AND archived_at IS NULL
            """
        )
        _rebuild_rooms_if_name_globally_unique(conn)
        # 重建 rooms 表只搬运显式列出的字段，新增列必须放在重建之后。
        _add_column_if_missing(conn, "rooms", "rules", "TEXT")
        _add_column_if_missing(conn, "rooms", "room_agent_id", "INTEGER REFERENCES users(id)")
        _add_column_if_missing(conn, "rooms", "template", "TEXT")
        # 房间 3D 场景：map3d 是场景描述符 JSON（kind=builtin/file/url），用户上传的
        # GLB 本体存 scene_data（同 users.model3d 的做法）。xr_state 是需求 9 的预留
        # 透传列，本期只建列、不写入也不消费。
        _add_column_if_missing(conn, "rooms", "map3d", "TEXT")
        _add_column_if_missing(conn, "rooms", "scene_data", "BLOB")
        _add_column_if_missing(conn, "rooms", "scene_mime", "TEXT")
        _add_column_if_missing(conn, "rooms", "scene_updated_at", "TEXT")
        _add_column_if_missing(conn, "rooms", "xr_state", "TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_live_name
            ON rooms(name COLLATE NOCASE) WHERE archived_at IS NULL
            """
        )
        _add_column_if_missing(conn, "room_members", "last_seen_at", "TEXT")
        _add_column_if_missing(conn, "room_members", "can_speak", "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "room_members", "can_upload", "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "room_members", "can_view_history", "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "room_members", "first_visible_msg_id", "INTEGER NOT NULL DEFAULT 0")
        if _add_column_if_missing(conn, "room_members", "last_read_msg_id", "INTEGER NOT NULL DEFAULT 0"):
            conn.execute(
                """
                UPDATE room_members
                SET last_read_msg_id = (
                    SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = room_members.room_id
                )
                """
            )
        _add_column_if_missing(conn, "messages", "msg_type", "TEXT NOT NULL DEFAULT 'text'")
        _add_column_if_missing(conn, "messages", "whisper_to", "INTEGER REFERENCES users(id)")
        _add_column_if_missing(conn, "messages", "whisper_to_ids", "TEXT")
        _add_column_if_missing(conn, "messages", "reply_to", "INTEGER REFERENCES messages(id)")
        _add_column_if_missing(conn, "messages", "recalled", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "messages", "duration_ms", "INTEGER")
        _add_column_if_missing(conn, "messages", "attachment_name", "TEXT")
        _add_column_if_missing(conn, "messages", "attachment_path", "TEXT")
        _add_column_if_missing(conn, "messages", "streaming", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "messages", "updated_at", "TEXT")
        conn.execute(
            """
            UPDATE messages
            SET updated_at = COALESCE(updated_at, created_at, strftime('%Y-%m-%d %H:%M:%f', 'now'))
            WHERE updated_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_streaming
                ON messages(room_id, id) WHERE streaming = 1
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_whisper_rules_room
                ON whisper_rules(room_id)
            """
        )

        conn.execute(
            """
            UPDATE room_members
            SET last_seen_at = COALESCE(last_seen_at, joined_at, datetime('now'))
            WHERE last_seen_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_members_last_seen
                ON room_members(room_id, last_seen_at)
            """
        )


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def seed_builtin_templates() -> None:
    """把仓库 templates/ 目录下的内置房间模板种进数据库。

    目录布局：templates/<name>/template.json（name/title/description）+ rules.md +
    可选脚本附件（如 werewolf_gm.zip，文件名记录在 template.json 的 scriptName）。
    同名用户模板不动；同名系统模板（created_by 为空）随版本刷新。
    """
    import json as _json

    if not TEMPLATES_DIR.is_dir():
        return
    with get_db() as conn:
        for d in sorted(TEMPLATES_DIR.iterdir()):
            meta_path = d / "template.json"
            if not d.is_dir() or not meta_path.is_file():
                continue
            try:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = str(meta.get("name") or "").strip()
            title = str(meta.get("title") or "").strip()
            if not name or not title:
                continue
            exists = conn.execute(
                "SELECT created_by FROM room_templates WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            rules_path = d / "rules.md"
            rules = rules_path.read_text(encoding="utf-8") if rules_path.is_file() else ""
            script_name = meta.get("scriptName")
            script_data = None
            if script_name:
                script_path = d / str(script_name)
                if script_path.is_file():
                    script_data = script_path.read_bytes()
            if exists:
                if exists["created_by"] is None:
                    conn.execute(
                        """
                        UPDATE room_templates
                        SET title = ?, description = ?, rules = ?, params = '{}',
                            script_name = ?, script_data = ?, updated_at = datetime('now')
                        WHERE name = ? COLLATE NOCASE
                        """,
                        (title, str(meta.get("description") or ""), rules, script_name, script_data, name),
                    )
                continue
            conn.execute(
                """
                INSERT INTO room_templates (name, title, description, rules, params, script_name, script_data)
                VALUES (?, ?, ?, ?, '{}', ?, ?)
                """,
                (name, title, str(meta.get("description") or ""), rules, script_name, script_data),
            )
