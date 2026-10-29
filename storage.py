"""Where timers are saved.

- If DATABASE_URL is set (e.g. a free Neon Postgres), timers are stored there.
  Use this on Render/Railway etc., where local files get wiped on restart.
- Otherwise they go into a local SQLite file (fine on your laptop or a VPS).
"""

import sqlite3

_PG_SCHEMA = """CREATE TABLE IF NOT EXISTS timers (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    channel_id  BIGINT NOT NULL,
    author_id   BIGINT NOT NULL,
    message     TEXT   NOT NULL,
    ping        TEXT   NOT NULL,
    fire_at     BIGINT NOT NULL,
    repeat      TEXT   NOT NULL DEFAULT 'once'
)"""

_SQLITE_SCHEMA = """CREATE TABLE IF NOT EXISTS timers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    message     TEXT    NOT NULL,
    ping        TEXT    NOT NULL,
    fire_at     INTEGER NOT NULL,
    repeat      TEXT    NOT NULL DEFAULT 'once'
)"""


class Store:
    def __init__(self, database_url: str | None, sqlite_path: str = "timers.db"):
        self.pg = bool(database_url)
        self.url = database_url
        self.sqlite_path = sqlite_path

    @property
    def kind(self) -> str:
        return "Postgres" if self.pg else f"SQLite ({self.sqlite_path})"

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

    # All methods are blocking; the bot calls them via asyncio.to_thread.
    def init(self) -> None:
        self._run(_PG_SCHEMA if self.pg else _SQLITE_SCHEMA)

    def all(self) -> list[dict]:
        return self._run("SELECT * FROM timers ORDER BY fire_at", fetch=True)

    def add(self, t: dict) -> int:
        rows = self._run(
            "INSERT INTO timers (guild_id, channel_id, author_id, message, ping, fire_at, repeat) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (t["guild_id"], t["channel_id"], t["author_id"], t["message"],
             t["ping"], t["fire_at"], t["repeat"]),
            fetch=True,
        )
        return rows[0]["id"]

    def delete(self, timer_id: int) -> None:
        self._run("DELETE FROM timers WHERE id = %s", (timer_id,))

    def set_fire_at(self, timer_id: int, fire_at: int) -> None:
        self._run("UPDATE timers SET fire_at = %s WHERE id = %s", (fire_at, timer_id))
