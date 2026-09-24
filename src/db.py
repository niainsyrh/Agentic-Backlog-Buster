"""Database connections for Backlog Buster (SQLite).

Two roles, on purpose:

* connect_admin() - used by the data generator and the evaluation scripts.
  Can read everything, including the ground-truth table `ticket_labels`.
* connect_agent() - used by the mock API and all agent code. SQLite's
  authorizer hook rejects ANY statement that touches `ticket_labels`
  (SELECT, JOIN, INSERT, UPDATE, DELETE, DROP) and rejects ATTACH, so the
  agent cannot read the answer key even by accident or via prompt injection.

The authorizer only protects connections opened through this module, so
find_bypasses() statically scans agent-side code for raw sqlite3.connect
calls or mentions of the protected table. Run it in tests / CI.

Run `python src/db.py` for the self-test.
"""
from __future__ import annotations

import re
import sqlite3
import tempfile
from pathlib import Path

from config import DB_PATH, ROOT

PROTECTED_TABLES = frozenset({"ticket_labels"})


def _authorizer(action: int, arg1: str | None, arg2: str | None,
                dbname: str | None, source: str | None) -> int:
    """SQLite calls this for every operation while a statement is compiled."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    # For READ / INSERT / UPDATE / DELETE / DROP_TABLE etc., arg1 is the table name.
    if arg1 is not None and arg1.lower() in PROTECTED_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect_admin(path: Path | str | None = None) -> sqlite3.Connection:
    """Full-access connection. Only generate_data.py and eval/ may use this."""
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_agent(path: Path | str | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Restricted connection for the mock API and agent code.

    readonly=True opens the file read-only (the mock API uses this).
    The agent still needs a writable connection later for audit_log and tickets.
    """
    p = Path(path or DB_PATH).resolve()
    if readonly:
        conn = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.set_authorizer(_authorizer)   # set last: nothing can remove it from agent code
    return conn


_BYPASS_PATTERNS = (
    (re.compile(r"sqlite3\s*\.\s*connect"), "raw sqlite3.connect (use db.connect_agent)"),
    (re.compile(r"ticket_labels", re.I), "mentions protected table ticket_labels"),
    (re.compile(r"set_authorizer"), "touches the authorizer"),
)


def find_bypasses() -> list[str]:
    """Scan agent-side code for ways around the label protection."""
    src = ROOT / "src"
    files = [src / "mock_api.py", *sorted((src / "agent").glob("**/*.py"))]
    problems = []
    for f in files:
        if not f.exists():
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for pattern, why in _BYPASS_PATTERNS:
                if pattern.search(code):
                    problems.append(f"{f.relative_to(ROOT)}:{lineno}: {why}")
    return problems


def _expect_denied(conn: sqlite3.Connection, sql: str) -> None:
    try:
        conn.execute(sql)
    except sqlite3.DatabaseError as e:
        msg = str(e).lower()
        assert "not authorized" in msg or "prohibited" in msg, f"unexpected error for {sql!r}: {e}"
        return
    raise AssertionError(f"should have been denied: {sql}")


def _self_test() -> None:
    conns: list[sqlite3.Connection] = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _run_connection_tests(Path(tmp) / "test dir with spaces.db", conns)   # spaces, like the real OneDrive path
        finally:
            for c in conns:
                c.close()   # Windows cannot delete an open file

    problems = find_bypasses()
    assert not problems, "bypass found:\n" + "\n".join(problems)
    print("ok  no raw sqlite3.connect / ticket_labels in mock_api.py or src/agent/")
    print("db.py self-test passed.")


def _run_connection_tests(db_file: Path, conns: list[sqlite3.Connection]) -> None:
    admin = connect_admin(db_file)
    conns.append(admin)
    admin.executescript("""
        CREATE TABLE tickets (ticket_id INTEGER PRIMARY KEY, text TEXT);
        CREATE TABLE ticket_labels (ticket_id INTEGER PRIMARY KEY REFERENCES tickets, intent TEXT);
        INSERT INTO tickets VALUES (1, 'parcel tak sampai');
        INSERT INTO ticket_labels VALUES (1, 'where_is_parcel');
    """)
    admin.commit()
    assert admin.execute("SELECT intent FROM ticket_labels").fetchone()[0] == "where_is_parcel"
    print("ok  admin can read ticket_labels")

    agent = connect_agent(db_file)
    conns.append(agent)
    assert agent.execute("SELECT text FROM tickets").fetchone()[0] == "parcel tak sampai"
    print("ok  agent can read tickets")
    _expect_denied(agent, "SELECT * FROM ticket_labels")
    _expect_denied(agent, "SELECT t.text, l.intent FROM tickets t JOIN ticket_labels l USING (ticket_id)")
    _expect_denied(agent, "SELECT text FROM tickets WHERE ticket_id IN (SELECT ticket_id FROM ticket_labels)")
    _expect_denied(agent, "UPDATE ticket_labels SET intent = 'x'")
    _expect_denied(agent, "DROP TABLE ticket_labels")
    _expect_denied(agent, f"ATTACH DATABASE '{db_file.as_posix()}' AS sneaky")
    print("ok  agent denied: select, join, subquery, update, drop, attach")

    ro = connect_agent(db_file, readonly=True)
    conns.append(ro)
    assert ro.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    try:
        ro.execute("INSERT INTO tickets VALUES (2, 'x')")
        raise AssertionError("read-only connection accepted a write")
    except sqlite3.OperationalError:
        pass
    _expect_denied(ro, "SELECT * FROM ticket_labels")
    print("ok  read-only connection reads tickets, refuses writes, still blocks labels")


if __name__ == "__main__":
    _self_test()
