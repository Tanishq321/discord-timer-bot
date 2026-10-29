"""Where the bot saves things.

- If DATABASE_URL is set (e.g. a free Neon Postgres), everything is stored there.
  Use this on Render/Railway etc., where local files get wiped on restart.
- Otherwise it goes into a local SQLite file (fine on your laptop or a VPS).

All methods are blocking; the bot calls them through asyncio.to_thread.
"""

import json
import sqlite3


def _schema(pg: bool) -> list[str]:
    idcol = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    big = "BIGINT" if pg else "INTEGER"
    return [
        f"""CREATE TABLE IF NOT EXISTS timers (
            id          {idcol},
            guild_id    {big} NOT NULL,
            channel_id  {big} NOT NULL,
            author_id   {big} NOT NULL,
            message     TEXT  NOT NULL,
            ping        TEXT  NOT NULL,
            fire_at     {big} NOT NULL,
            repeat      TEXT  NOT NULL DEFAULT 'once'
        )""",
        f"""CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id {big} PRIMARY KEY,
            data     TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS rsvp_messages (
            message_id {big} PRIMARY KEY,
            timer_id   {big} NOT NULL,
            guild_id   {big} NOT NULL,
            created_at {big} NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS rsvps (
            message_id {big} NOT NULL,
            user_id    {big} NOT NULL,
            status     TEXT  NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS deletions (
            message_id {big} PRIMARY KEY,
            channel_id {big} NOT NULL,
            delete_at  {big} NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS birthdays (
            guild_id {big} NOT NULL,
            user_id  {big} NOT NULL,
            day      INTEGER NOT NULL,
            month    INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )""",
    ]


class Store:
    def __init__(self, database_url: str | None, sqlite_path: str = "timers.db"):
        self.pg = bool(database_url)
        self.url = database_url
        self.sqlite_path = sqlite_path

    @property
    def kind(self) -> str:
        return "Postgres" if self.pg else "SQLite file"

    # ------------------------------------------------------------ plumbing
    def _connect(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=20)
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, sql: str, params=(), fetch: bool = False):
        if not self.pg:
            sql = sql.replace("%s", "?")
        conn = self._connect()
        try:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    def init(self) -> None:
        for stmt in _schema(self.pg):
            self._run(stmt)
        # Upgrade older installs: one JSON column holds all the newer per-timer options.
        if self.pg:
            self._run("ALTER TABLE timers ADD COLUMN IF NOT EXISTS options TEXT NOT NULL DEFAULT '{}'")
        else:
            cols = [r["name"] for r in self._run("PRAGMA table_info(timers)", fetch=True)]
            if "options" not in cols:
                self._run("ALTER TABLE timers ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")

    # ------------------------------------------------------------ timers
    @staticmethod
    def _timer_row(r: dict) -> dict:
        r = dict(r)
        r["options"] = json.loads(r.get("options") or "{}")
        return r

    def all_timers(self) -> list[dict]:
        return [self._timer_row(r) for r in self._run("SELECT * FROM timers ORDER BY fire_at", fetch=True)]

    def add_timer(self, t: dict) -> int:
        rows = self._run(
            "INSERT INTO timers (guild_id, channel_id, author_id, message, ping, fire_at, repeat, options) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (t["guild_id"], t["channel_id"], t["author_id"], t["message"], t["ping"],
             t["fire_at"], t["repeat"], json.dumps(t.get("options", {}))),
            fetch=True,
        )
        return rows[0]["id"]

    def save_timer(self, t: dict) -> None:
        self._run(
            "UPDATE timers SET channel_id=%s, message=%s, ping=%s, fire_at=%s, repeat=%s, options=%s "
            "WHERE id=%s",
            (t["channel_id"], t["message"], t["ping"], t["fire_at"], t["repeat"],
             json.dumps(t.get("options", {})), t["id"]),
        )

    def delete_timer(self, timer_id: int) -> None:
        self._run("DELETE FROM timers WHERE id = %s", (timer_id,))

    # ------------------------------------------------------------ server settings
    def all_settings(self) -> dict[int, dict]:
        rows = self._run("SELECT * FROM guild_settings", fetch=True)
        return {r["guild_id"]: json.loads(r["data"]) for r in rows}

    def save_settings(self, guild_id: int, data: dict) -> None:
        self._run(
            "INSERT INTO guild_settings (guild_id, data) VALUES (%s, %s) "
            "ON CONFLICT (guild_id) DO UPDATE SET data = excluded.data",
            (guild_id, json.dumps(data)),
        )

    # ------------------------------------------------------------ RSVPs
    def add_rsvp_message(self, message_id: int, timer_id: int, guild_id: int, created_at: int) -> None:
        self._run(
            "INSERT INTO rsvp_messages (message_id, timer_id, guild_id, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (message_id) DO NOTHING",
            (message_id, timer_id, guild_id, created_at),
        )

    def latest_rsvp_message(self, timer_id: int) -> dict | None:
        rows = self._run(
            "SELECT * FROM rsvp_messages WHERE timer_id = %s ORDER BY created_at DESC LIMIT 1",
            (timer_id,), fetch=True,
        )
        return rows[0] if rows else None

    def set_rsvp(self, message_id: int, user_id: int, status: str) -> None:
        self._run(
            "INSERT INTO rsvps (message_id, user_id, status) VALUES (%s, %s, %s) "
            "ON CONFLICT (message_id, user_id) DO UPDATE SET status = excluded.status",
            (message_id, user_id, status),
        )

    def rsvp_users(self, message_id: int) -> dict[str, list[int]]:
        rows = self._run("SELECT user_id, status FROM rsvps WHERE message_id = %s", (message_id,), fetch=True)
        out = {"yes": [], "no": []}
        for r in rows:
            out.setdefault(r["status"], []).append(r["user_id"])
        return out

    # ------------------------------------------------------------ scheduled deletions
    def all_deletions(self) -> list[dict]:
        return self._run("SELECT * FROM deletions", fetch=True)

    def add_deletion(self, message_id: int, channel_id: int, delete_at: int) -> None:
        self._run(
            "INSERT INTO deletions (message_id, channel_id, delete_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (message_id) DO NOTHING",
            (message_id, channel_id, delete_at),
        )

    def remove_deletion(self, message_id: int) -> None:
        self._run("DELETE FROM deletions WHERE message_id = %s", (message_id,))

    # ------------------------------------------------------------ birthdays
    def set_birthday(self, guild_id: int, user_id: int, day: int, month: int) -> None:
        self._run(
            "INSERT INTO birthdays (guild_id, user_id, day, month) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET day = excluded.day, month = excluded.month",
            (guild_id, user_id, day, month),
        )

    def remove_birthday(self, guild_id: int, user_id: int) -> bool:
        before = self._run("SELECT 1 FROM birthdays WHERE guild_id=%s AND user_id=%s", (guild_id, user_id), fetch=True)
        self._run("DELETE FROM birthdays WHERE guild_id=%s AND user_id=%s", (guild_id, user_id))
        return bool(before)

    def birthdays(self, guild_id: int) -> list[dict]:
        return self._run("SELECT * FROM birthdays WHERE guild_id = %s", (guild_id,), fetch=True)
