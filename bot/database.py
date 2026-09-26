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
    mod_channel_id INTEGER,
    mod_role_id INTEGER,
    target_language TEXT DEFAULT 'ru',
    delete_message INTEGER DEFAULT 0,
    llm_enabled INTEGER DEFAULT 1,
    strike_thresholds TEXT DEFAULT '{}',
    manual_strikes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    language TEXT,
    UNIQUE(guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    rule_id TEXT,
    severity TEXT DEFAULT 'low',
    method TEXT DEFAULT 'regex',
    message_snapshot TEXT,
    original_text TEXT,
    translated_text TEXT,
    detected_language TEXT,
    channel_id INTEGER,
    message_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS punishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    violation_id INTEGER,
    moderator_id INTEGER,
    action TEXT,
    duration_seconds INTEGER,
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
"""

_SERVER_COLS = {
    "mod_channel_id",
    "mod_role_id",
    "target_language",
    "delete_message",
    "llm_enabled",
    "strike_thresholds",
    "manual_strikes",
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
            conn.executescript(SCHEMA)
            # Миграция для старых БД: колонка роли модерации
            try:
                conn.execute("ALTER TABLE servers ADD COLUMN mod_role_id INTEGER")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # колонка уже есть
            conn.commit()

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
                    "mod_channel_id": None,
                    "mod_role_id": None,
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
                vals.append(guild_id)
                conn.execute(
                    f"UPDATE servers SET {', '.join(cols)} WHERE guild_id = ?",
                    vals,
                )
            conn.commit()

    # ---- Users ----
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
            cols = ["guild_id", "user_id"] + [
                k for k in fields if k in [
                    "rule_id", "severity", "method", "message_snapshot",
                    "original_text", "translated_text", "detected_language",
                    "channel_id", "message_id",
                ]
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
            cols = ["guild_id", "user_id"] + [
                k for k in fields if k in [
                    "violation_id", "moderator_id", "action", "duration_seconds", "status",
                ]
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
        """Выгружает нарушения вместе с решениями модераторов за последние `days` дней (для CSV /export)."""
        days = max(1, int(days))
        since_expr = f"-{days} days"
        with self._session() as conn:
            rows = conn.execute(
                """SELECT
                       v.id AS violation_id,
                       v.created_at AS violation_time,
                       v.user_id,
                       v.channel_id,
                       v.rule_id,
                       v.severity,
                       v.method,
                       v.original_text,
                       v.translated_text,
                       p.action AS mod_action,
                       p.duration_seconds,
                       p.moderator_id,
                       p.status AS punishment_status,
                       p.created_at AS decision_time
                   FROM violations v
                   LEFT JOIN punishments p ON p.violation_id = v.id
                   WHERE v.guild_id = ? AND v.created_at >= datetime('now', ?)
                   ORDER BY v.id DESC LIMIT ?""",
                (guild_id, since_expr, limit),
            ).fetchall()
            return [dict(r) for r in rows]


_db = Database()


def get_db() -> Database:
    return _db


