import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Optional

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    guild_id INTEGER PRIMARY KEY,
    guild_name TEXT,
    guild_icon_url TEXT,
    member_count INTEGER DEFAULT 0,
    mod_channel_id INTEGER,
    mod_channel_name TEXT,
    mod_role_id INTEGER,
    mod_role_name TEXT,
    target_language TEXT DEFAULT 'ru',
    delete_message INTEGER DEFAULT 0,
    llm_enabled INTEGER DEFAULT 1,
    strike_thresholds TEXT DEFAULT '{}',
    manual_strikes INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    global_name TEXT,
    display_name TEXT,
    avatar_url TEXT,
    language TEXT,
    account_created_at TEXT,
    joined_at TEXT,
    top_role_name TEXT,
    top_role_color TEXT,
    roles_json TEXT,
    is_bot INTEGER DEFAULT 0,
    is_moderator INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'text',
    category_name TEXT,
    position INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS roles (
    role_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    color TEXT,
    position INTEGER DEFAULT 0,
    is_staff INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    guild_name TEXT,
    user_id INTEGER NOT NULL,
    user_name TEXT,
    user_display_name TEXT,
    user_avatar_url TEXT,
    rule_id TEXT,
    severity TEXT DEFAULT 'low',
    method TEXT DEFAULT 'regex',
    reason TEXT,
    message_snapshot TEXT,
    original_text TEXT,
    translated_text TEXT,
    detected_language TEXT,
    channel_id INTEGER,
    channel_name TEXT,
    message_id INTEGER,
    jump_url TEXT,
    attachments_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS punishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT,
    user_display_name TEXT,
    user_avatar_url TEXT,
    violation_id INTEGER,
    moderator_id INTEGER,
    moderator_name TEXT,
    moderator_display_name TEXT,
    moderator_avatar_url TEXT,
    action TEXT,
    duration_seconds INTEGER,
    reason TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS banned_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    UNIQUE(guild_id, word)
);

CREATE TABLE IF NOT EXISTS rule_exceptions (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    rule TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id, rule)
);

CREATE TABLE IF NOT EXISTS scan_progress (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_violations_guild_user ON violations(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_violations_message_id ON violations(message_id);
CREATE INDEX IF NOT EXISTS idx_punishments_violation_id ON punishments(violation_id);
CREATE INDEX IF NOT EXISTS idx_punishments_guild_user ON punishments(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_punishments_action_status ON punishments(action, status);
CREATE INDEX IF NOT EXISTS idx_users_guild_user ON users(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_channels_guild ON channels(guild_id);
CREATE INDEX IF NOT EXISTS idx_roles_guild ON roles(guild_id);
"""

_MIGRATION_COLUMNS: list[tuple[str, str, str]] = [
    # servers
    ("servers", "guild_name", "TEXT"),
    ("servers", "guild_icon_url", "TEXT"),
    ("servers", "member_count", "INTEGER DEFAULT 0"),
    ("servers", "mod_channel_id", "INTEGER"),
    ("servers", "mod_channel_name", "TEXT"),
    ("servers", "mod_role_id", "INTEGER"),
    ("servers", "mod_role_name", "TEXT"),
    ("servers", "target_language", "TEXT DEFAULT 'ru'"),
    ("servers", "delete_message", "INTEGER DEFAULT 0"),
    ("servers", "llm_enabled", "INTEGER DEFAULT 1"),
    ("servers", "strike_thresholds", "TEXT DEFAULT '{}'"),
    ("servers", "manual_strikes", "INTEGER DEFAULT 0"),
    ("servers", "updated_at", "TEXT"),
    # users
    ("users", "username", "TEXT"),
    ("users", "global_name", "TEXT"),
    ("users", "display_name", "TEXT"),
    ("users", "avatar_url", "TEXT"),
    ("users", "account_created_at", "TEXT"),
    ("users", "joined_at", "TEXT"),
    ("users", "top_role_name", "TEXT"),
    ("users", "top_role_color", "TEXT"),
    ("users", "roles_json", "TEXT"),
    ("users", "is_bot", "INTEGER DEFAULT 0"),
    ("users", "is_moderator", "INTEGER DEFAULT 0"),
    ("users", "updated_at", "TEXT"),
    # violations
    ("violations", "guild_name", "TEXT"),
    ("violations", "user_name", "TEXT"),
    ("violations", "user_display_name", "TEXT"),
    ("violations", "user_avatar_url", "TEXT"),
    ("violations", "reason", "TEXT"),
    ("violations", "channel_name", "TEXT"),
    ("violations", "jump_url", "TEXT"),
    ("violations", "attachments_json", "TEXT"),
    # punishments
    ("punishments", "user_name", "TEXT"),
    ("punishments", "user_display_name", "TEXT"),
    ("punishments", "user_avatar_url", "TEXT"),
    ("punishments", "moderator_name", "TEXT"),
    ("punishments", "moderator_display_name", "TEXT"),
    ("punishments", "moderator_avatar_url", "TEXT"),
    ("punishments", "reason", "TEXT"),
]

_SERVER_COLS = {
    "guild_name",
    "guild_icon_url",
    "member_count",
    "mod_channel_id",
    "mod_channel_name",
    "mod_role_id",
    "mod_role_name",
    "target_language",
    "delete_message",
    "llm_enabled",
    "strike_thresholds",
    "manual_strikes",
    "updated_at",
}

_VIOLATION_COLS = {
    "guild_name",
    "user_name",
    "user_display_name",
    "user_avatar_url",
    "rule_id",
    "severity",
    "method",
    "reason",
    "message_snapshot",
    "original_text",
    "translated_text",
    "detected_language",
    "channel_id",
    "channel_name",
    "message_id",
    "jump_url",
    "attachments_json",
}

_PUNISHMENT_COLS = {
    "user_name",
    "user_display_name",
    "user_avatar_url",
    "violation_id",
    "moderator_id",
    "moderator_name",
    "moderator_display_name",
    "moderator_avatar_url",
    "action",
    "duration_seconds",
    "reason",
    "status",
}


def migrate_conn(conn: sqlite3.Connection) -> None:
    """Создаёт все таблицы и безопасно добавляет недостающие колонки в существующую БД SQLite."""
    conn.executescript(SCHEMA)
    for table, col, col_type in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


def extract_discord_user_meta(user_obj: Any) -> dict[str, Any]:
    """Извлекает из объекта discord.Member / discord.User полный набор метаданных для БД и веб-панели."""
    if user_obj is None:
        return {}
    username = getattr(user_obj, "name", None) or str(user_obj)
    global_name = getattr(user_obj, "global_name", None)
    display_name = (
        getattr(user_obj, "display_name", None)
        or global_name
        or username
    )
    avatar_url = None
    disp_av = getattr(user_obj, "display_avatar", None) or getattr(user_obj, "avatar", None)
    if disp_av is not None:
        avatar_url = str(getattr(disp_av, "url", disp_av))

    created_at = getattr(user_obj, "created_at", None)
    account_created_str = created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else None

    joined_at = getattr(user_obj, "joined_at", None)
    joined_str = joined_at.strftime("%Y-%m-%d %H:%M:%S") if joined_at else None

    top_role_name = None
    top_role_color = None
    roles_list = []
    raw_roles = getattr(user_obj, "roles", None)
    if raw_roles:
        for r in raw_roles:
            if getattr(r, "is_default", lambda: False)():
                continue
            r_color = str(getattr(r, "color", "")) if getattr(r, "color", None) else None
            if r_color == "#000000":
                r_color = None
            roles_list.append({
                "id": str(getattr(r, "id", "")),
                "name": getattr(r, "name", ""),
                "color": r_color,
            })
        top_role = getattr(user_obj, "top_role", None)
        if top_role and not getattr(top_role, "is_default", lambda: False)():
            top_role_name = getattr(top_role, "name", None)
            tc = str(getattr(top_role, "color", ""))
            top_role_color = tc if tc and tc != "#000000" else None

    is_bot = 1 if getattr(user_obj, "bot", False) else 0
    perms = getattr(user_obj, "guild_permissions", None)
    is_mod = 0
    if perms is not None and (
        getattr(perms, "administrator", False)
        or getattr(perms, "moderate_members", False)
        or getattr(perms, "manage_messages", False)
        or getattr(perms, "ban_members", False)
        or getattr(perms, "kick_members", False)
    ):
        is_mod = 1

    return {
        "username": username,
        "global_name": global_name,
        "display_name": display_name,
        "avatar_url": avatar_url,
        "account_created_at": account_created_str,
        "joined_at": joined_str,
        "top_role_name": top_role_name,
        "top_role_color": top_role_color,
        "roles_json": json.dumps(roles_list, ensure_ascii=False) if roles_list else None,
        "is_bot": is_bot,
        "is_moderator": is_mod,
    }


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_path: Optional[str] = None

    def _open_connection(self) -> sqlite3.Connection:
        dirpath = os.path.dirname(self.path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def connect(self) -> sqlite3.Connection:
        return self._open_connection()

    @contextmanager
    def _session(self):
        with self._lock:
            if self._conn is None or self._conn_path != self.path:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                self._conn = self._open_connection()
                self._conn_path = self.path
            try:
                yield self._conn
            except sqlite3.ProgrammingError:
                # Re-open if external caller accidentally closed the shared connection
                self._conn = self._open_connection()
                self._conn_path = self.path
                yield self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._conn_path = None

    def init(self) -> None:
        with self._session() as conn:
            migrate_conn(conn)

    # ---- Servers ----
    def get_server(self, guild_id: int) -> dict[str, Any]:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM servers WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT OR IGNORE INTO servers (guild_id) VALUES (?)", (guild_id,)
                )
                conn.commit()
                return {
                    "guild_id": guild_id,
                    "guild_name": None,
                    "guild_icon_url": None,
                    "member_count": 0,
                    "mod_channel_id": None,
                    "mod_channel_name": None,
                    "mod_role_id": None,
                    "mod_role_name": None,
                    "target_language": "ru",
                    "delete_message": 0,
                    "llm_enabled": 1,
                    "strike_thresholds": {},
                    "manual_strikes": 0,
                }
            d = dict(row)
            d["strike_thresholds"] = json.loads(d.get("strike_thresholds") or "{}")
            return d

    def update_server(self, guild_id: int, **fields) -> None:
        valid = {k: v for k, v in fields.items() if k in _SERVER_COLS}
        with self._session() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO servers (guild_id) VALUES (?)", (guild_id,)
            )
            if valid:
                cols = []
                vals = []
                for key, value in valid.items():
                    if key == "strike_thresholds" and isinstance(value, (dict, list)):
                        value = json.dumps(value)
                    cols.append(f"{key} = ?")
                    vals.append(value)
                cols.append("updated_at = datetime('now')")
                vals.append(guild_id)
                conn.execute(
                    f"UPDATE servers SET {', '.join(cols)} WHERE guild_id = ?",
                    vals,
                )
            conn.commit()

    def upsert_guild_metadata(
        self,
        guild_id: int,
        guild_name: str,
        guild_icon_url: Optional[str] = None,
        member_count: int = 0,
        mod_channel_name: Optional[str] = None,
        mod_role_name: Optional[str] = None,
    ) -> None:
        """Сохраняет название сервера, иконку, число участников и имена канала/роли модерации."""
        with self._session() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO servers (guild_id) VALUES (?)",
                (guild_id,),
            )
            conn.execute(
                """UPDATE servers
                   SET guild_name = ?,
                       guild_icon_url = COALESCE(?, guild_icon_url),
                       member_count = CASE WHEN ? > 0 THEN ? ELSE COALESCE(member_count, 0) END,
                       mod_channel_name = COALESCE(?, mod_channel_name),
                       mod_role_name = COALESCE(?, mod_role_name),
                       updated_at = datetime('now')
                   WHERE guild_id = ?""",
                (
                    guild_name,
                    guild_icon_url,
                    member_count,
                    member_count,
                    mod_channel_name,
                    mod_role_name,
                    guild_id,
                ),
            )
            # Дозаполняем название сервера в старых нарушениях, если оно было пустым
            if guild_name:
                conn.execute(
                    "UPDATE violations SET guild_name = ? WHERE guild_id = ? AND (guild_name IS NULL OR guild_name = '')",
                    (guild_name, guild_id),
                )
            conn.commit()

    def sync_guild_channels_and_roles(
        self,
        guild_id: int,
        channels_list: list[dict[str, Any]],
        roles_list: list[dict[str, Any]],
    ) -> None:
        """Синхронизирует список всех каналов и ролей сервера для удобного отображения и выбора в веб-панели."""
        with self._session() as conn:
            for ch in channels_list:
                cid = int(ch["channel_id"])
                cname = str(ch["name"])
                conn.execute(
                    """INSERT INTO channels (channel_id, guild_id, name, type, category_name, position, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(channel_id) DO UPDATE SET
                           guild_id = excluded.guild_id,
                           name = excluded.name,
                           type = excluded.type,
                           category_name = excluded.category_name,
                           position = excluded.position,
                           updated_at = datetime('now')""",
                    (
                        cid,
                        guild_id,
                        cname,
                        ch.get("type") or "text",
                        ch.get("category_name"),
                        int(ch.get("position") or 0),
                    ),
                )
                # Дозаполняем название канала в старых нарушениях
                conn.execute(
                    "UPDATE violations SET channel_name = ? WHERE guild_id = ? AND channel_id = ? AND (channel_name IS NULL OR channel_name = '')",
                    (cname, guild_id, cid),
                )

            for rl in roles_list:
                rid = int(rl["role_id"])
                conn.execute(
                    """INSERT INTO roles (role_id, guild_id, name, color, position, is_staff, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(role_id) DO UPDATE SET
                           guild_id = excluded.guild_id,
                           name = excluded.name,
                           color = excluded.color,
                           position = excluded.position,
                           is_staff = excluded.is_staff,
                           updated_at = datetime('now')""",
                    (
                        rid,
                        guild_id,
                        str(rl["name"]),
                        rl.get("color"),
                        int(rl.get("position") or 0),
                        int(rl.get("is_staff") or 0),
                    ),
                )
            conn.commit()

    # ---- Users & Profiles ----
    def upsert_user_profile(self, guild_id: int, user_id: int, **meta) -> None:
        """Сохраняет или обновляет профиль пользователя (никнейм, логин, аватарку, роли, дату регистрации)
        и автоматически обогащает записи в violations и punishments."""
        if not guild_id or not user_id:
            return
        with self._session() as conn:
            conn.execute(
                """INSERT INTO users (
                       guild_id, user_id, username, global_name, display_name, avatar_url,
                       account_created_at, joined_at, top_role_name, top_role_color,
                       roles_json, is_bot, is_moderator, updated_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET
                       username = COALESCE(excluded.username, users.username),
                       global_name = COALESCE(excluded.global_name, users.global_name),
                       display_name = COALESCE(excluded.display_name, users.display_name),
                       avatar_url = COALESCE(excluded.avatar_url, users.avatar_url),
                       account_created_at = COALESCE(excluded.account_created_at, users.account_created_at),
                       joined_at = COALESCE(excluded.joined_at, users.joined_at),
                       top_role_name = COALESCE(excluded.top_role_name, users.top_role_name),
                       top_role_color = COALESCE(excluded.top_role_color, users.top_role_color),
                       roles_json = COALESCE(excluded.roles_json, users.roles_json),
                       is_bot = COALESCE(excluded.is_bot, users.is_bot),
                       is_moderator = COALESCE(excluded.is_moderator, users.is_moderator),
                       updated_at = datetime('now')""",
                (
                    guild_id,
                    user_id,
                    meta.get("username"),
                    meta.get("global_name"),
                    meta.get("display_name"),
                    meta.get("avatar_url"),
                    meta.get("account_created_at"),
                    meta.get("joined_at"),
                    meta.get("top_role_name"),
                    meta.get("top_role_color"),
                    meta.get("roles_json"),
                    meta.get("is_bot", 0),
                    meta.get("is_moderator", 0),
                ),
            )
            uname = meta.get("username")
            dname = meta.get("display_name") or uname
            av = meta.get("avatar_url")
            if uname or dname or av:
                conn.execute(
                    """UPDATE violations
                       SET user_name = COALESCE(?, user_name),
                           user_display_name = COALESCE(?, user_display_name),
                           user_avatar_url = COALESCE(?, user_avatar_url)
                       WHERE guild_id = ? AND user_id = ?""",
                    (uname, dname, av, guild_id, user_id),
                )
                conn.execute(
                    """UPDATE punishments
                       SET user_name = COALESCE(?, user_name),
                           user_display_name = COALESCE(?, user_display_name),
                           user_avatar_url = COALESCE(?, user_avatar_url)
                       WHERE guild_id = ? AND user_id = ?""",
                    (uname, dname, av, guild_id, user_id),
                )
                conn.execute(
                    """UPDATE punishments
                       SET moderator_name = COALESCE(?, moderator_name),
                           moderator_display_name = COALESCE(?, moderator_display_name),
                           moderator_avatar_url = COALESCE(?, moderator_avatar_url)
                       WHERE guild_id = ? AND moderator_id = ?""",
                    (uname, dname, av, guild_id, user_id),
                )
            conn.commit()

    def record_discord_member(self, guild_id: int, user_obj: Any) -> dict[str, Any]:
        """Удобный метод: извлекает данные из объекта discord.Member/User, сохраняет в БД и возвращает словарь."""
        if user_obj is None or not getattr(user_obj, "id", None):
            return {}
        meta = extract_discord_user_meta(user_obj)
        self.upsert_user_profile(guild_id, int(user_obj.id), **meta)
        return meta

    def get_user_profile(self, guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def get_user_language(self, guild_id: int, user_id: int) -> Optional[str]:
        with self._session() as conn:
            row = conn.execute(
                "SELECT language FROM users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return row["language"] if row else None

    def set_user_language(self, guild_id: int, user_id: int, language: str) -> None:
        with self._session() as conn:
            conn.execute(
                """INSERT INTO users (guild_id, user_id, language)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, user_id)
                   DO UPDATE SET language = excluded.language""",
                (guild_id, user_id, language),
            )
            conn.commit()

    # ---- Violations ----
    def add_violation(self, guild_id: int, user_id: int, **fields) -> int:
        with self._session() as conn:
            # Автоматически подтягиваем название сервера, канала и профиль пользователя из кэша БД,
            # если вызывающий код не передал их явно.
            if not fields.get("guild_name"):
                s_row = conn.execute(
                    "SELECT guild_name FROM servers WHERE guild_id = ?", (guild_id,)
                ).fetchone()
                if s_row and s_row["guild_name"]:
                    fields["guild_name"] = s_row["guild_name"]

            ch_id = fields.get("channel_id")
            if ch_id and not fields.get("channel_name"):
                c_row = conn.execute(
                    "SELECT name FROM channels WHERE channel_id = ?", (ch_id,)
                ).fetchone()
                if c_row and c_row["name"]:
                    fields["channel_name"] = c_row["name"]

            if not fields.get("user_name") or not fields.get("user_avatar_url"):
                u_row = conn.execute(
                    "SELECT username, display_name, avatar_url FROM users WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                if u_row:
                    fields.setdefault("user_name", u_row["username"])
                    fields.setdefault("user_display_name", u_row["display_name"] or u_row["username"])
                    fields.setdefault("user_avatar_url", u_row["avatar_url"])

            cols = ["guild_id", "user_id"] + [
                k for k in fields if k in _VIOLATION_COLS and fields[k] is not None
            ]
            vals = [guild_id, user_id] + [fields[k] for k in cols[2:]]
            cur = conn.execute(
                f"INSERT INTO violations ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})",
                vals,
            )
            conn.commit()
            return cur.lastrowid

    def list_violations(self, guild_id: int, user_id: int, limit: int = 100):
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM violations WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_violation(self, violation_id: int):
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM violations WHERE id = ?", (violation_id,)
            ).fetchone()
            return dict(row) if row else None

    def count_violations(self, guild_id: int, user_id: int) -> int:
        with self._session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM violations WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return row["c"] if row else 0

    def skipped_violation_ids(self, guild_id: int, user_id: int) -> set[int]:
        """ids нарушений, по которым модератор нажал «Пропустить»."""
        with self._session() as conn:
            rows = conn.execute(
                """SELECT p.violation_id AS vid FROM punishments p
                   JOIN violations v ON v.id = p.violation_id
                   WHERE v.guild_id = ? AND v.user_id = ? AND p.action = 'skip'""",
                (guild_id, user_id),
            ).fetchall()
            return {r["vid"] for r in rows if r["vid"] is not None}

    def count_skipped(self, guild_id: int, user_id: int) -> int:
        return len(self.skipped_violation_ids(guild_id, user_id))

    def count_real_violations(self, guild_id: int, user_id: int) -> int:
        """Реальные страйки: зафиксированные нарушения минус пропущенные модератором
        и снятые как «не нарушение»."""
        with self._session() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS c FROM violations v
                   WHERE v.guild_id = ? AND v.user_id = ?
                     AND NOT EXISTS (
                         SELECT 1 FROM punishments p
                         WHERE p.violation_id = v.id AND p.action IN ('skip', 'dismiss')
                     )""",
                (guild_id, user_id),
            ).fetchone()
            return row["c"] if row else 0

    def count_recent_violations(self, guild_id: int, user_id: int,
                                window_seconds: int) -> int:
        """Число нарушений пользователя за последние window_seconds (для экстренной панели)."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM violations "
                "WHERE guild_id = ? AND user_id = ? "
                "AND created_at >= datetime('now', ?)",
                (guild_id, user_id, f"-{int(window_seconds)} seconds"),
            ).fetchone()
            return row["c"] if row else 0

    def list_recent_violations(self, guild_id: int = None, limit: int = 200):
        """Недавние нарушения (для перерегистрации кнопок панелей после рестарта)."""
        with self._session() as conn:
            if guild_id is None:
                rows = conn.execute(
                    "SELECT * FROM violations ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM violations WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                    (guild_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def reset_user_stats(self, guild_id: int, user_id: int) -> None:
        """Полный сброс нарушений и истории наказаний пользователя на сервере."""
        with self._session() as conn:
            conn.execute(
                "DELETE FROM punishments WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            conn.execute(
                "DELETE FROM violations WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            conn.commit()

    # ---- Punishments ----
    def add_punishment(self, guild_id: int, user_id: int, **fields) -> int:
        with self._session() as conn:
            if user_id and (not fields.get("user_name") or not fields.get("user_avatar_url")):
                u_row = conn.execute(
                    "SELECT username, display_name, avatar_url FROM users WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                if u_row:
                    fields.setdefault("user_name", u_row["username"])
                    fields.setdefault("user_display_name", u_row["display_name"] or u_row["username"])
                    fields.setdefault("user_avatar_url", u_row["avatar_url"])

            mod_id = fields.get("moderator_id")
            if mod_id == 0:
                fields.setdefault("moderator_name", "pulse_web")
                fields.setdefault("moderator_display_name", "Веб-панель Pulse")
            elif mod_id and (not fields.get("moderator_name") or not fields.get("moderator_avatar_url")):
                m_row = conn.execute(
                    "SELECT username, display_name, avatar_url FROM users WHERE guild_id = ? AND user_id = ?",
                    (guild_id, mod_id),
                ).fetchone()
                if m_row:
                    fields.setdefault("moderator_name", m_row["username"])
                    fields.setdefault("moderator_display_name", m_row["display_name"] or m_row["username"])
                    fields.setdefault("moderator_avatar_url", m_row["avatar_url"])

            cols = ["guild_id", "user_id"] + [
                k for k in fields if k in _PUNISHMENT_COLS and fields[k] is not None
            ]
            vals = [guild_id, user_id] + [fields[k] for k in cols[2:]]
            cur = conn.execute(
                f"INSERT INTO punishments ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})",
                vals,
            )
            conn.commit()
            return cur.lastrowid

    def list_punishments(self, guild_id: int, user_id: int, limit: int = 100):
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def has_punishment_for_violation(self, violation_id: int) -> bool:
        """Есть ли уже принятое решение модератора по данному нарушению."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM punishments WHERE violation_id = ? LIMIT 1",
                (violation_id,),
            ).fetchone()
            return row is not None

    def list_expired_bans(self) -> list[dict[str, Any]]:
        """Временные баны, у которых истёк срок (для фонового авто-разбана)."""
        with self._session() as conn:
            rows = conn.execute(
                """SELECT id, guild_id, user_id, violation_id, duration_seconds, created_at
                   FROM punishments
                   WHERE action = 'ban'
                     AND status = 'applied'
                     AND duration_seconds IS NOT NULL
                     AND duration_seconds > 0
                     AND datetime(created_at, '+' || duration_seconds || ' seconds') <= datetime('now')"""
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_punishment_status(self, punishment_id: int, status: str) -> None:
        with self._session() as conn:
            conn.execute(
                "UPDATE punishments SET status = ? WHERE id = ?",
                (status, punishment_id),
            )
            conn.commit()

    # ---- Banned words ----
    def list_banwords(self, guild_id: int):
        with self._session() as conn:
            rows = conn.execute(
                "SELECT word FROM banned_words WHERE guild_id = ?", (guild_id,)
            ).fetchall()
            return [r["word"] for r in rows]

    def add_banword(self, guild_id: int, word: str) -> None:
        with self._session() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
                (guild_id, word.lower()),
            )
            conn.commit()

    def remove_banword(self, guild_id: int, word: str) -> bool:
        with self._session() as conn:
            cur = conn.execute(
                "DELETE FROM banned_words WHERE guild_id = ? AND word = ?",
                (guild_id, word.lower()),
            )
            conn.commit()
            return cur.rowcount > 0

    # ---- Rule exceptions per channel ----
    def list_rule_exceptions(self, guild_id: int, channel_id: int = None) -> list:
        """Исключённые правила канала (или всех каналов сервера,
        если channel_id не задан): список (channel_id, rule)."""
        with self._session() as conn:
            if channel_id is None:
                rows = conn.execute(
                    "SELECT channel_id, rule FROM rule_exceptions WHERE guild_id = ?",
                    (guild_id,),
                ).fetchall()
                return [(r["channel_id"], r["rule"]) for r in rows]
            rows = conn.execute(
                "SELECT rule FROM rule_exceptions WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).fetchall()
            return [r["rule"] for r in rows]

    def add_rule_exception(self, guild_id: int, channel_id: int, rule: str) -> None:
        with self._session() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rule_exceptions (guild_id, channel_id, rule) VALUES (?, ?, ?)",
                (guild_id, channel_id, rule.lower()),
            )
            conn.commit()

    def remove_rule_exception(self, guild_id: int, channel_id: int, rule: str) -> bool:
        with self._session() as conn:
            cur = conn.execute(
                "DELETE FROM rule_exceptions WHERE guild_id = ? AND channel_id = ? AND rule = ?",
                (guild_id, channel_id, rule.lower()),
            )
            conn.commit()
            return cur.rowcount > 0

    # ---- Scan progress (для бэкфилла пропущенных офлайн-сообщений) ----
    def update_scan_progress(self, guild_id: int, channel_id: int, last_message_id: int) -> None:
        with self._session() as conn:
            conn.execute(
                """INSERT INTO scan_progress (guild_id, channel_id, last_message_id, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(guild_id, channel_id)
                   DO UPDATE SET last_message_id = excluded.last_message_id,
                                 updated_at = datetime('now')""",
                (guild_id, channel_id, last_message_id),
            )
            conn.commit()

    def scan_progress(self, guild_id: int, channel_id: int) -> Optional[int]:
        with self._session() as conn:
            row = conn.execute(
                "SELECT last_message_id FROM scan_progress WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).fetchone()
            return row["last_message_id"] if row else None

    def list_scan_progress(self, guild_id: int = None):
        with self._session() as conn:
            if guild_id is None:
                rows = conn.execute(
                    "SELECT guild_id, channel_id, last_message_id FROM scan_progress"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT guild_id, channel_id, last_message_id "
                    "FROM scan_progress WHERE guild_id = ?", (guild_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def violation_for_message(self, message_id: int) -> bool:
        """Есть ли уже панель/запись по этому сообщению (защита от дублей при бэкфилле)."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT id FROM violations WHERE message_id = ?", (message_id,)
            ).fetchone()
            return row is not None

    def list_dismissed_examples(self, guild_id: int = None, limit: int = 12) -> list[str]:
        """Возвращает последние оригинальные фразы, помеченные модератором как «Не нарушение» (dismiss)."""
        with self._session() as conn:
            if guild_id is None:
                rows = conn.execute(
                    """SELECT v.original_text FROM violations v
                       JOIN punishments p ON p.violation_id = v.id
                       WHERE p.action = 'dismiss' AND v.original_text IS NOT NULL
                       ORDER BY p.id DESC LIMIT ?""",
                    (limit * 2,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT v.original_text FROM violations v
                       JOIN punishments p ON p.violation_id = v.id
                       WHERE p.action = 'dismiss' AND v.guild_id = ? AND v.original_text IS NOT NULL
                       ORDER BY p.id DESC LIMIT ?""",
                    (guild_id, limit * 2),
                ).fetchall()
            out = []
            seen = set()
            for r in rows:
                txt = (r["original_text"] or "").strip()
                if 3 <= len(txt) <= 240 and txt.lower() not in seen:
                    seen.add(txt.lower())
                    out.append(txt)
                    if len(out) >= limit:
                        break
            return out

    def get_digest_stats(self, guild_id: int, days: int = 7) -> dict[str, Any]:
        """Сводка по серверу за последние `days` дней для авто-дайджеста и команды /digest."""
        days = max(1, int(days))
        since_expr = f"-{days} days"
        with self._session() as conn:
            total_violations = conn.execute(
                """SELECT COUNT(*) AS n FROM violations
                   WHERE guild_id = ? AND created_at >= datetime('now', ?)""",
                (guild_id, since_expr),
            ).fetchone()["n"]

            dismissed_count = conn.execute(
                """SELECT COUNT(*) AS n FROM punishments
                   WHERE guild_id = ? AND action = 'dismiss'
                     AND created_at >= datetime('now', ?)""",
                (guild_id, since_expr),
            ).fetchone()["n"]

            skipped_count = conn.execute(
                """SELECT COUNT(*) AS n FROM punishments
                   WHERE guild_id = ? AND action = 'skip'
                     AND created_at >= datetime('now', ?)""",
                (guild_id, since_expr),
            ).fetchone()["n"]

            actions_rows = conn.execute(
                """SELECT action, COUNT(*) AS c FROM punishments
                   WHERE guild_id = ? AND action NOT IN ('dismiss', 'skip')
                     AND created_at >= datetime('now', ?)
                   GROUP BY action ORDER BY c DESC""",
                (guild_id, since_expr),
            ).fetchall()

            top_rules = conn.execute(
                """SELECT v.rule_id, COUNT(*) AS c FROM violations v
                   WHERE v.guild_id = ? AND v.created_at >= datetime('now', ?)
                     AND NOT EXISTS (
                         SELECT 1 FROM punishments p
                         WHERE p.violation_id = v.id AND p.action = 'dismiss'
                     )
                   GROUP BY v.rule_id ORDER BY c DESC LIMIT 5""",
                (guild_id, since_expr),
            ).fetchall()

            top_mods = conn.execute(
                """SELECT moderator_id, COUNT(*) AS c FROM punishments
                   WHERE guild_id = ? AND moderator_id IS NOT NULL
                     AND created_at >= datetime('now', ?)
                   GROUP BY moderator_id ORDER BY c DESC LIMIT 5""",
                (guild_id, since_expr),
            ).fetchall()

            return {
                "days": days,
                "total_violations": total_violations,
                "real_violations": max(0, total_violations - dismissed_count),
                "dismissed": dismissed_count,
                "skipped": skipped_count,
                "actions": {r["action"]: r["c"] for r in actions_rows},
                "top_rules": [(r["rule_id"], r["c"]) for r in top_rules],
                "top_mods": [(r["moderator_id"], r["c"]) for r in top_mods],
            }

    def export_guild_records(self, guild_id: int, days: int = 30, limit: int = 5000) -> list[dict[str, Any]]:
        """Выгружает нарушения вместе с решениями модераторов за последние `days` дней (для CSV /export и веб-панели)."""
        days = max(1, int(days))
        since_expr = f"-{days} days"
        with self._session() as conn:
            rows = conn.execute(
                """SELECT
                       v.id AS violation_id,
                       v.created_at AS violation_time,
                       v.guild_id,
                       COALESCE(v.guild_name, s.guild_name) AS guild_name,
                       v.user_id,
                       COALESCE(v.user_name, u.username) AS user_name,
                       COALESCE(v.user_display_name, u.display_name, v.user_name, u.username) AS user_display_name,
                       COALESCE(v.user_avatar_url, u.avatar_url) AS user_avatar_url,
                       v.channel_id,
                       COALESCE(v.channel_name, ch.name) AS channel_name,
                       v.rule_id,
                       v.severity,
                       v.method,
                       v.reason,
                       v.original_text,
                       v.translated_text,
                       v.jump_url,
                       p.action AS mod_action,
                       p.duration_seconds,
                       p.moderator_id,
                       COALESCE(p.moderator_name, mu.username) AS moderator_name,
                       COALESCE(p.moderator_display_name, mu.display_name, p.moderator_name, mu.username) AS moderator_display_name,
                       COALESCE(p.moderator_avatar_url, mu.avatar_url) AS moderator_avatar_url,
                       p.status AS punishment_status,
                       p.created_at AS decision_time
                   FROM violations v
                   LEFT JOIN punishments p ON p.violation_id = v.id
                   LEFT JOIN servers s ON s.guild_id = v.guild_id
                   LEFT JOIN users u ON u.guild_id = v.guild_id AND u.user_id = v.user_id
                   LEFT JOIN channels ch ON ch.channel_id = v.channel_id
                   LEFT JOIN users mu ON mu.guild_id = v.guild_id AND mu.user_id = p.moderator_id
                   WHERE v.guild_id = ? AND v.created_at >= datetime('now', ?)
                   ORDER BY v.id DESC LIMIT ?""",
                (guild_id, since_expr, limit),
            ).fetchall()
            return [dict(r) for r in rows]


_db = Database()


def get_db() -> Database:
    return _db


