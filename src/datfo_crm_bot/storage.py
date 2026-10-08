from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries(update_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
                request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, value TEXT NOT NULL, result TEXT,
                created_at TEXT NOT NULL, completed_at TEXT);
            CREATE TABLE IF NOT EXISTS registrations(
                telegram_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                bitrix_id INTEGER NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
                approved_by INTEGER, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE UNIQUE INDEX IF NOT EXISTS registration_employee
                ON registrations(bitrix_id) WHERE status='approved';
            CREATE TABLE IF NOT EXISTS registration_sessions(telegram_id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_steps(
                request_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                PRIMARY KEY(request_id,name));
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def session(self, user_id: int) -> dict | None:
        row = self.db.execute("SELECT value FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_session(self, user_id: int, value: dict | None) -> None:
        with self.db:
            if value is None:
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            else:
                self.db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
                                (user_id, json.dumps(value, ensure_ascii=False)))

    def delivery(self, update_id: int) -> dict | None:
        row = self.db.execute("SELECT chat_id,value FROM deliveries WHERE update_id=?", (update_id,)).fetchone()
        return {"chat_id": row[0], **json.loads(row[1])} if row else None

    def commit_reply(self, update_id: int, chat_id: int, user_id: int, state: dict | None, text: str, keyboard: list, *, admin_only: bool = False) -> None:
        # Persist the next dialog state together with the reply. Replayed Telegram updates cannot advance the form twice.
        with self.db:
            if state is None:
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            else:
                self.db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
                                (user_id, json.dumps(state, ensure_ascii=False)))
            self.db.execute("INSERT INTO deliveries VALUES(?,?,?)",
                            (update_id, chat_id, json.dumps({"text": text, "keyboard": keyboard, "admin_only": admin_only}, ensure_ascii=False)))

    def prune_deliveries(self, acknowledged_offset: int) -> None:
        # Keep a recent replay window; old acknowledged updates will not be fetched again.
        with self.db:
            self.db.execute("DELETE FROM deliveries WHERE update_id<?", (acknowledged_offset - 1000,))

    def offset(self) -> int:
        row = self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, offset: int) -> None:
        with self.db:
            self.db.execute("INSERT INTO settings VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))

    def remember_identity(self, sender: dict) -> None:
        data = {key: sender.get(key) for key in ("id", "username", "first_name", "last_name")}
        with self.db:
            self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            ("identity:" + str(sender["id"]), json.dumps(data, ensure_ascii=False)))

    def registration(self, telegram_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM registrations WHERE telegram_id=?", (telegram_id,)).fetchone()
        return dict(row) if row else None

    def registrations(self, status: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM registrations WHERE status=? ORDER BY telegram_id", (status,))]

    def request_registration(self, telegram_id: int, chat_id: int, employee: dict) -> None:
        with self.db:
            self.db.execute("""INSERT INTO registrations(telegram_id,chat_id,bitrix_id,name,status)
                VALUES(?,?,?,?,'pending') ON CONFLICT(telegram_id) DO UPDATE SET
                chat_id=excluded.chat_id,bitrix_id=excluded.bitrix_id,name=excluded.name,
                status='pending',approved_by=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE registrations.status IN ('pending','rejected')""",
                (telegram_id, chat_id, employee["id"], employee["name"]))

    def approve_registration(self, telegram_id: int, administrator_id: int, employee: dict, *, work_role=None) -> bool:
        if work_role is not None and work_role not in {'fom_sales','tech','trainer','dispatcher'}:
            raise ValueError('Invalid work role')
        try:
            with self.db:
                changed = self.db.execute("""UPDATE registrations SET status='approved',approved_by=?,
                    name=?,updated_at=CURRENT_TIMESTAMP WHERE telegram_id=? AND status='pending' AND bitrix_id=?""",
                    (administrator_id, employee["name"], telegram_id, employee["id"])).rowcount
                if changed and work_role is not None:
                    self.db.execute('''INSERT INTO fom_request_members(telegram_id,role,granted_by) VALUES(?,?,?)
                        ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role,granted_by=excluded.granted_by,
                        updated_at=CURRENT_TIMESTAMP''', (telegram_id, work_role, administrator_id))
            return bool(changed)
        except sqlite3.IntegrityError:
            return False

    def deny_registration(self, telegram_id: int, administrator_id: int, status: str) -> bool:
        if status not in {"rejected", "revoked"}:
            raise ValueError("Invalid registration status")
        expected = "pending" if status == "rejected" else "approved"
        with self.db:
            changed = self.db.execute("""UPDATE registrations SET status=?,approved_by=?,updated_at=CURRENT_TIMESTAMP
                WHERE telegram_id=? AND status=?""", (status, administrator_id, telegram_id, expected)).rowcount
            if changed:
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (telegram_id,))
                self.db.execute("DELETE FROM registration_sessions WHERE telegram_id=?", (telegram_id,))
        return bool(changed)

    def registration_session(self, telegram_id: int) -> dict | None:
        row = self.db.execute("SELECT value FROM registration_sessions WHERE telegram_id=?", (telegram_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_registration_session(self, telegram_id: int, state: dict | None) -> None:
        with self.db:
            if state is None:
                self.db.execute("DELETE FROM registration_sessions WHERE telegram_id=?", (telegram_id,))
            else:
                self.db.execute("INSERT INTO registration_sessions VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET value=excluded.value",
                                (telegram_id, json.dumps(state, ensure_ascii=False)))

    def recipient(self, bitrix_id: int) -> int | None:
        row = self.db.execute("SELECT chat_id FROM registrations WHERE bitrix_id=? AND status='approved'", (bitrix_id,)).fetchone()
        return int(row[0]) if row else None

    def operation(self, request_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["value"] = json.loads(result["value"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def prepare(self, user_id: int, state: dict, now: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO operations(request_id,user_id,kind,status,value,created_at) VALUES(?,?,?,'prepared',?,?)",
                            (state["request_id"], user_id, state["kind"], json.dumps(state, ensure_ascii=False), now))

    def status(self, request_id: str, status: str, result: dict | None = None, completed_at: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE operations SET status=?,result=?,completed_at=? WHERE request_id=?",
                            (status, json.dumps(result) if result is not None else None, completed_at, request_id))

    def operation_step(self, request_id: str, name: str) -> dict | None:
        row = self.db.execute("SELECT status,result FROM operation_steps WHERE request_id=? AND name=?", (request_id, name)).fetchone()
        return {"status": row[0], "result": json.loads(row[1]) if row[1] else None} if row else None

    def set_operation_step(self, request_id: str, name: str, status: str, result: dict | None = None) -> None:
        with self.db:
            self.db.execute("""INSERT INTO operation_steps VALUES(?,?,?,?) ON CONFLICT(request_id,name)
                DO UPDATE SET status=excluded.status,result=excluded.result""",
                (request_id, name, status, json.dumps(result, ensure_ascii=False) if result is not None else None))

    def other_inflight(self, request_id: str, field: str, value: str, *, phase: str | None = None) -> bool:
        for row in self.db.execute("SELECT request_id,value FROM operations WHERE request_id<>? AND status IN ('sending','created','uncertain')", (request_id,)):
            state = json.loads(row["value"])
            if state.get("workflow") != "okb" or state.get(field) != value:
                continue
            if phase:
                step = self.operation_step(row["request_id"], phase)
                if not step or step["status"] not in {"sending", "uncertain"}:
                    continue
            return True
        if field == 'phone' and phase == 'contact' and self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fom_support_contact_steps'").fetchone():
            if self.db.execute('''SELECT 1 FROM fom_support_contact_steps s JOIN fom_requests r ON r.id=s.ticket
                    WHERE r.request_id<>? AND s.phone=? AND s.phase='contact'
                    AND s.state IN ('sending','uncertain') LIMIT 1''', (request_id,value)).fetchone():
                return True
        return False

    def operation_wrote(self, request_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM operation_steps WHERE request_id=? AND status='succeeded_write' LIMIT 1", (request_id,)).fetchone() is not None

    def statistics(self, start: str, end: str, user_id: int | None) -> dict[str, int]:
        sql = "SELECT kind,COUNT(*) AS n FROM operations WHERE status='succeeded' AND completed_at>=? AND completed_at<?"
        parameters = [start, end]
        if user_id is not None:
            sql += " AND user_id=?"
            parameters.append(user_id)
        sql += " GROUP BY kind"
        return {row["kind"]: row["n"] for row in self.db.execute(sql, parameters)}

    def unfinished(self, user_id: int) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT request_id,kind,status FROM operations WHERE user_id=? AND status IN ('sending','uncertain','created')", (user_id,))]


@contextmanager
def process_lock(database: Path):
    """One writer per database on Windows and Linux; lock releases after a crash."""
    database.parent.mkdir(parents=True, exist_ok=True)
    handle = (database.parent / (database.name + ".lock")).open("a+b")
    handle.seek(0)
    handle.write(b"0")
    handle.flush()
    handle.seek(0)
    import os
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Уже запущен процесс с этой базой бота") from None
    try:
        yield
    finally:
        handle.close()
