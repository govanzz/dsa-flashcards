from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from html import escape
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import streamlit as st

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Local SQLite mode does not need psycopg installed.
    psycopg = None
    dict_row = None


APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "flashcards.db"
LOCAL_DEFAULT_EMAIL = "local@dsa-flashcards.dev"
DEFAULT_GITHUB_IMPORTS_PER_HOUR = 5
DEFAULT_GITHUB_IMPORTS_PER_DAY = 25

COMMON_TOPICS = [
    "Arrays & Hashing",
    "Two Pointers",
    "Sliding Window",
    "Stack",
    "Binary Search",
    "Linked List",
    "Trees",
    "Tries",
    "Heap / Priority Queue",
    "Backtracking",
    "Graphs",
    "Advanced Graphs",
    "1-D Dynamic Programming",
    "2-D Dynamic Programming",
    "Greedy",
    "Intervals",
    "Math & Geometry",
    "Bit Manipulation",
]

DIFFICULTIES = ["Easy", "Medium", "Hard", "Mixed", "Unknown"]
SOURCES = ["Neetcode 150", "Neetcode GitHub Sync", "LeetCode", "Blind 75", "Custom", "Other"]
SUPPORTED_IMPORT_EXTENSIONS = {
    "Python (.py)": ".py",
    "JavaScript (.js)": ".js",
    "TypeScript (.ts)": ".ts",
    "Java (.java)": ".java",
    "C++ (.cpp)": ".cpp",
    "C# (.cs)": ".cs",
    "Go (.go)": ".go",
    "Rust (.rs)": ".rs",
    "Kotlin (.kt)": ".kt",
    "Swift (.swift)": ".swift",
    "SQL (.sql)": ".sql",
}
GENERIC_GITHUB_REPO_EXAMPLE = "https://github.com/your-username/neetcode-submissions"

RATING_HELP = {
    "Again": "I could not recall the idea.",
    "Hard": "I remembered pieces, but needed help.",
    "Good": "I solved the main idea cleanly.",
    "Easy": "The pattern felt automatic.",
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def normalize_email(value: str | None) -> str:
    email = (value or "").strip().lower()
    return email or LOCAL_DEFAULT_EMAIL


def active_user_email() -> str:
    return normalize_email(st.session_state.get("user_email", LOCAL_DEFAULT_EMAIL))


def active_user_name() -> str:
    return str(st.session_state.get("user_name") or active_user_email().split("@")[0])


def auth_configured() -> bool:
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def sync_user_session() -> bool:
    if auth_configured():
        try:
            if st.user.is_logged_in:
                st.session_state["user_email"] = normalize_email(st.user.get("email"))
                st.session_state["user_name"] = st.user.get("name") or st.session_state["user_email"]
                return True
        except Exception:
            pass
        return False

    st.session_state.setdefault("user_email", LOCAL_DEFAULT_EMAIL)
    st.session_state.setdefault("user_name", "Local learner")
    return True


def secret_value(key: str) -> str:
    value = os.getenv(key)
    if value:
        return value
    try:
        return str(st.secrets.get(key, "") or "")
    except Exception:
        return ""


def secret_int(key: str, default: int) -> int:
    raw = secret_value(key)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def parse_email_list(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        parts = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return {normalize_email(part) for part in parts if normalize_email(part)}


def admin_emails() -> set[str]:
    emails = parse_email_list(os.getenv("ADMIN_EMAILS"))
    try:
        emails |= parse_email_list(st.secrets.get("ADMIN_EMAILS"))
        admin_section = st.secrets.get("admin", {})
        if hasattr(admin_section, "get"):
            emails |= parse_email_list(admin_section.get("emails"))
    except Exception:
        pass
    if not emails and not auth_configured():
        emails.add(LOCAL_DEFAULT_EMAIL)
    return emails


def is_admin() -> bool:
    return active_user_email() in admin_emails()


def database_url() -> str:
    return secret_value("DATABASE_URL").strip()


def using_postgres() -> bool:
    url = database_url()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def query_for_backend(query: str) -> str:
    if using_postgres():
        return query.replace("?", "%s")
    return query


class DatabaseConnection:
    def __init__(self) -> None:
        self.conn: Any = None

    def __enter__(self) -> "DatabaseConnection":
        if using_postgres():
            if psycopg is None or dict_row is None:
                raise RuntimeError("Postgres mode requires psycopg. Run `pip install -r requirements.txt`.")
            self.conn = psycopg.connect(database_url(), row_factory=dict_row)
        else:
            DATA_DIR.mkdir(exist_ok=True)
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.conn is None:
            return
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        return self.conn.execute(query_for_backend(query), params)


def connect() -> DatabaseConnection:
    return DatabaseConnection()


def ensure_column(conn: DatabaseConnection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_sqlite_operational_tables(conn: DatabaseConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT DEFAULT '',
            action TEXT NOT NULL,
            entity TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT DEFAULT '',
            repo_url TEXT DEFAULT '',
            branch TEXT DEFAULT '',
            language TEXT DEFAULT '',
            status TEXT NOT NULL,
            total_files INTEGER DEFAULT 0,
            created_count INTEGER DEFAULT 0,
            updated_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            error_message TEXT DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT DEFAULT '',
            location TEXT DEFAULT '',
            message TEXT NOT NULL,
            traceback TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_import_runs_user_started ON import_runs(user_email, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_errors_created ON app_errors(created_at)")


def init_postgres_operational_tables(conn: DatabaseConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGSERIAL PRIMARY KEY,
            user_email TEXT DEFAULT '',
            action TEXT NOT NULL,
            entity TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id BIGSERIAL PRIMARY KEY,
            user_email TEXT DEFAULT '',
            repo_url TEXT DEFAULT '',
            branch TEXT DEFAULT '',
            language TEXT DEFAULT '',
            status TEXT NOT NULL,
            total_files INTEGER DEFAULT 0,
            created_count INTEGER DEFAULT 0,
            updated_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            error_message TEXT DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_errors (
            id BIGSERIAL PRIMARY KEY,
            user_email TEXT DEFAULT '',
            location TEXT DEFAULT '',
            message TEXT NOT NULL,
            traceback TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_import_runs_user_started ON import_runs(user_email, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_errors_created ON app_errors(created_at)")


def init_sqlite_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS problems (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source TEXT DEFAULT '',
                url TEXT DEFAULT '',
                difficulty TEXT DEFAULT 'Unknown',
                topics TEXT DEFAULT '',
                status TEXT DEFAULT 'Solved',
                question TEXT DEFAULT '',
                intuition TEXT DEFAULT '',
                solution TEXT DEFAULT '',
                user_email TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                solved_at TEXT,
                updated_at TEXT NOT NULL,
                last_reviewed_at TEXT,
                next_review_at TEXT NOT NULL,
                interval_days REAL DEFAULT 0,
                ease_factor REAL DEFAULT 2.5,
                repetitions INTEGER DEFAULT 0,
                lapses INTEGER DEFAULT 0,
                review_count INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0
            )
            """
        )
        ensure_column(conn, "problems", "user_email", f"TEXT DEFAULT '{LOCAL_DEFAULT_EMAIL}'")
        ensure_column(conn, "problems", "external_source", "TEXT DEFAULT ''")
        ensure_column(conn, "problems", "external_id", "TEXT DEFAULT ''")
        ensure_column(conn, "problems", "imported_at", "TEXT")
        conn.execute(
            "UPDATE problems SET user_email = ? WHERE user_email IS NULL OR user_email = ''",
            (LOCAL_DEFAULT_EMAIL,),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id INTEGER NOT NULL,
                user_email TEXT DEFAULT '',
                reviewed_at TEXT NOT NULL,
                rating TEXT NOT NULL,
                previous_interval REAL NOT NULL,
                new_interval REAL NOT NULL,
                previous_ease REAL NOT NULL,
                new_ease REAL NOT NULL,
                next_review_at TEXT NOT NULL,
                FOREIGN KEY(problem_id) REFERENCES problems(id)
            )
            """
        )
        ensure_column(conn, "reviews", "user_email", f"TEXT DEFAULT '{LOCAL_DEFAULT_EMAIL}'")
        conn.execute(
            "UPDATE reviews SET user_email = ? WHERE user_email IS NULL OR user_email = ''",
            (LOCAL_DEFAULT_EMAIL,),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_sources (
                user_email TEXT PRIMARY KEY,
                repo_url TEXT NOT NULL,
                branch TEXT NOT NULL,
                language TEXT NOT NULL,
                max_cards INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        init_sqlite_operational_tables(conn)


def init_postgres_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS problems (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                source TEXT DEFAULT '',
                url TEXT DEFAULT '',
                difficulty TEXT DEFAULT 'Unknown',
                topics TEXT DEFAULT '',
                status TEXT DEFAULT 'Solved',
                question TEXT DEFAULT '',
                intuition TEXT DEFAULT '',
                solution TEXT DEFAULT '',
                user_email TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                solved_at TEXT,
                updated_at TEXT NOT NULL,
                last_reviewed_at TEXT,
                next_review_at TEXT NOT NULL,
                interval_days DOUBLE PRECISION DEFAULT 0,
                ease_factor DOUBLE PRECISION DEFAULT 2.5,
                repetitions INTEGER DEFAULT 0,
                lapses INTEGER DEFAULT 0,
                review_count INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                external_source TEXT DEFAULT '',
                external_id TEXT DEFAULT '',
                imported_at TEXT
            )
            """
        )
        conn.execute("ALTER TABLE problems ADD COLUMN IF NOT EXISTS user_email TEXT DEFAULT ''")
        conn.execute("ALTER TABLE problems ADD COLUMN IF NOT EXISTS external_source TEXT DEFAULT ''")
        conn.execute("ALTER TABLE problems ADD COLUMN IF NOT EXISTS external_id TEXT DEFAULT ''")
        conn.execute("ALTER TABLE problems ADD COLUMN IF NOT EXISTS imported_at TEXT")
        conn.execute(
            "UPDATE problems SET user_email = ? WHERE user_email IS NULL OR user_email = ''",
            (LOCAL_DEFAULT_EMAIL,),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id BIGSERIAL PRIMARY KEY,
                problem_id BIGINT NOT NULL REFERENCES problems(id),
                user_email TEXT DEFAULT '',
                reviewed_at TEXT NOT NULL,
                rating TEXT NOT NULL,
                previous_interval DOUBLE PRECISION NOT NULL,
                new_interval DOUBLE PRECISION NOT NULL,
                previous_ease DOUBLE PRECISION NOT NULL,
                new_ease DOUBLE PRECISION NOT NULL,
                next_review_at TEXT NOT NULL
            )
            """
        )
        conn.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS user_email TEXT DEFAULT ''")
        conn.execute(
            "UPDATE reviews SET user_email = ? WHERE user_email IS NULL OR user_email = ''",
            (LOCAL_DEFAULT_EMAIL,),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_sources (
                user_email TEXT PRIMARY KEY,
                repo_url TEXT NOT NULL,
                branch TEXT NOT NULL,
                language TEXT NOT NULL,
                max_cards INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        init_postgres_operational_tables(conn)


def init_db() -> None:
    if using_postgres():
        init_postgres_db()
    else:
        init_sqlite_db()


def row_to_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return row_to_dict(row)


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def utc_since(**kwargs: int) -> str:
    return (datetime.now(UTC) - timedelta(**kwargs)).replace(microsecond=0).isoformat()


def redact_sensitive(value: Any) -> str:
    text = str(value or "")
    secret_candidates = [
        database_url(),
        secret_value("DATABASE_URL"),
    ]
    try:
        auth_section = st.secrets.get("auth", {})
        if hasattr(auth_section, "get"):
            secret_candidates.extend(
                [
                    str(auth_section.get("client_secret", "") or ""),
                    str(auth_section.get("cookie_secret", "") or ""),
                ]
            )
    except Exception:
        pass

    for secret in secret_candidates:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")

    text = re.sub(r"postgres(?:ql)?://[^@\s]+@", "postgresql://[redacted]@", text)
    text = re.sub(r"GOCSPX-[A-Za-z0-9_\-]+", "GOCSPX-[redacted]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [redacted]", text)
    return text


def details_to_json(details: Any = None) -> str:
    if details is None:
        return "{}"
    try:
        return redact_sensitive(json.dumps(details, sort_keys=True, default=str))
    except TypeError:
        return redact_sensitive(details)


def log_audit(action: str, entity: str = "", details: Any = None, user_email: str | None = None) -> None:
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (user_email, action, entity, details, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalize_email(user_email or active_user_email()),
                    action,
                    entity,
                    details_to_json(details),
                    utc_now(),
                ),
            )
    except Exception:
        pass


def record_app_error(location: str, exc: BaseException, user_email: str | None = None) -> None:
    message = redact_sensitive(f"{type(exc).__name__}: {exc}")
    trace = redact_sensitive(traceback.format_exc())
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO app_errors (user_email, location, message, traceback, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalize_email(user_email or active_user_email()),
                    location,
                    message,
                    trace,
                    utc_now(),
                ),
            )
    except Exception:
        pass


def github_import_limits() -> tuple[int, int]:
    per_hour = secret_int("GITHUB_IMPORTS_PER_HOUR", DEFAULT_GITHUB_IMPORTS_PER_HOUR)
    per_day = secret_int("GITHUB_IMPORTS_PER_DAY", DEFAULT_GITHUB_IMPORTS_PER_DAY)
    return per_hour, per_day


def github_import_usage() -> tuple[int, int]:
    hour_count = fetch_one(
        "SELECT COUNT(*) AS count FROM import_runs WHERE user_email = ? AND started_at >= ?",
        (active_user_email(), utc_since(hours=1)),
    ) or {"count": 0}
    day_count = fetch_one(
        "SELECT COUNT(*) AS count FROM import_runs WHERE user_email = ? AND started_at >= ?",
        (active_user_email(), utc_since(days=1)),
    ) or {"count": 0}
    return int(hour_count["count"] or 0), int(day_count["count"] or 0)


def check_github_import_rate_limit() -> tuple[bool, str]:
    per_hour, per_day = github_import_limits()
    hour_count, day_count = github_import_usage()
    if hour_count >= per_hour:
        return False, f"Import limit reached: {per_hour} GitHub import(s) per hour. Try again later."
    if day_count >= per_day:
        return False, f"Import limit reached: {per_day} GitHub import(s) per day. Try again tomorrow."
    return True, ""


def create_import_run(repo_url: str, branch: str, language: str) -> int:
    started_at = utc_now()
    with connect() as conn:
        query = """
        INSERT INTO import_runs (user_email, repo_url, branch, language, status, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        if using_postgres():
            query += " RETURNING id"
        cur = conn.execute(
            query,
            (active_user_email(), repo_url.strip(), branch.strip(), language, "started", started_at),
        )
        if using_postgres():
            row = cur.fetchone()
            return int(row["id"])
        return int(cur.lastrowid)


def finish_import_run(
    run_id: int | None,
    status: str,
    created: int = 0,
    updated: int = 0,
    skipped: int = 0,
    total_files: int = 0,
    error_message: str = "",
) -> None:
    if run_id is None:
        return
    try:
        with connect() as conn:
            conn.execute(
                """
                UPDATE import_runs
                SET status = ?,
                    total_files = ?,
                    created_count = ?,
                    updated_count = ?,
                    skipped_count = ?,
                    error_message = ?,
                    finished_at = ?
                WHERE id = ? AND user_email = ?
                """,
                (
                    status,
                    total_files,
                    created,
                    updated,
                    skipped,
                    redact_sensitive(error_message)[:1200],
                    utc_now(),
                    run_id,
                    active_user_email(),
                ),
            )
    except Exception:
        pass


def insert_problem(data: dict[str, Any]) -> int:
    now = utc_now()
    with connect() as conn:
        query = """
        INSERT INTO problems (
            title, source, url, difficulty, topics, status, question, intuition,
            solution, user_email, created_at, solved_at, updated_at, next_review_at,
            external_source, external_id, imported_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if using_postgres():
            query += " RETURNING id"
        cur = conn.execute(
            query,
            (
                data["title"].strip(),
                data.get("source", ""),
                data.get("url", "").strip(),
                data.get("difficulty", "Unknown"),
                data.get("topics", ""),
                data.get("status", "Solved"),
                data.get("question", "").strip(),
                data.get("intuition", "").strip(),
                data.get("solution", "").rstrip(),
                normalize_email(data.get("user_email") or active_user_email()),
                now,
                data.get("solved_at") or today_iso(),
                now,
                today_iso(),
                data.get("external_source", ""),
                data.get("external_id", ""),
                data.get("imported_at"),
            ),
        )
        if using_postgres():
            row = cur.fetchone()
            return int(row["id"])
        return int(cur.lastrowid)


def update_problem(problem_id: int, data: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE problems
            SET title = ?,
                source = ?,
                url = ?,
                difficulty = ?,
                topics = ?,
                status = ?,
                question = ?,
                intuition = ?,
                solution = ?,
                solved_at = ?,
                updated_at = ?
            WHERE id = ? AND user_email = ?
            """,
            (
                data["title"].strip(),
                data.get("source", ""),
                data.get("url", "").strip(),
                data.get("difficulty", "Unknown"),
                data.get("topics", ""),
                data.get("status", "Solved"),
                data.get("question", "").strip(),
                data.get("intuition", "").strip(),
                data.get("solution", "").rstrip(),
                data.get("solved_at") or today_iso(),
                utc_now(),
                problem_id,
                active_user_email(),
            ),
        )
    log_audit(
        "problem_updated",
        "problem",
        {"problem_id": problem_id, "title": data.get("title", "").strip()},
    )


def update_problem_notes(problem_id: int, intuition: str, question: str | None = None) -> None:
    with connect() as conn:
        if question is None:
            conn.execute(
                """
                UPDATE problems
                SET intuition = ?, updated_at = ?
                WHERE id = ? AND user_email = ?
                """,
                (intuition.strip(), utc_now(), problem_id, active_user_email()),
            )
        else:
            conn.execute(
                """
                UPDATE problems
                SET intuition = ?, question = ?, updated_at = ?
                WHERE id = ? AND user_email = ?
                """,
                (intuition.strip(), question.strip(), utc_now(), problem_id, active_user_email()),
            )
    log_audit(
        "problem_notes_updated",
        "problem",
        {"problem_id": problem_id, "updated_prompt": question is not None},
    )


def archive_problem(problem_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE problems SET archived = 1, updated_at = ? WHERE id = ? AND user_email = ?",
            (utc_now(), problem_id, active_user_email()),
        )
    log_audit("problem_archived", "problem", {"problem_id": problem_id})


def restore_problem(problem_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE problems SET archived = 0, updated_at = ? WHERE id = ? AND user_email = ?",
            (utc_now(), problem_id, active_user_email()),
        )
    log_audit("problem_restored", "problem", {"problem_id": problem_id})


def get_problem(problem_id: int) -> dict[str, Any] | None:
    return fetch_one(
        "SELECT * FROM problems WHERE id = ? AND user_email = ?",
        (problem_id, active_user_email()),
    )


def get_problem_by_external(external_source: str, external_id: str) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT *
        FROM problems
        WHERE external_source = ? AND external_id = ? AND user_email = ?
        """,
        (external_source, external_id, active_user_email()),
    )


def update_imported_problem(problem_id: int, data: dict[str, Any]) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE problems
            SET solution = ?,
                external_source = ?,
                external_id = ?,
                imported_at = CASE
                    WHEN imported_at IS NULL OR imported_at = '' THEN ?
                    ELSE imported_at
                END,
                updated_at = ?
            WHERE id = ? AND user_email = ?
            """,
            (
                data.get("solution", "").rstrip(),
                data.get("external_source", ""),
                data.get("external_id", ""),
                data.get("imported_at") or now,
                now,
                problem_id,
                active_user_email(),
            ),
        )


def get_github_source() -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT repo_url, branch, language, max_cards
        FROM github_sources
        WHERE user_email = ?
        """,
        (active_user_email(),),
    )


def save_github_source(repo_url: str, branch: str, language: str, max_cards: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO github_sources (user_email, repo_url, branch, language, max_cards, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_email) DO UPDATE SET
                repo_url = excluded.repo_url,
                branch = excluded.branch,
                language = excluded.language,
                max_cards = excluded.max_cards,
                updated_at = excluded.updated_at
            """,
            (active_user_email(), repo_url.strip(), branch.strip(), language, int(max_cards), utc_now()),
        )


def list_problems(include_archived: bool = False) -> list[dict[str, Any]]:
    where = "WHERE user_email = ?" if include_archived else "WHERE user_email = ? AND archived = 0"
    return fetch_all(
        f"""
        SELECT *
        FROM problems
        {where}
        ORDER BY archived ASC, next_review_at ASC, lower(title) ASC
        """,
        (active_user_email(),),
    )


def get_due_cards(limit: int = 50) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT *
        FROM problems
        WHERE user_email = ? AND archived = 0 AND next_review_at <= ?
        ORDER BY next_review_at ASC, repetitions ASC, lower(title) ASC
        LIMIT ?
        """,
        (active_user_email(), today_iso(), limit),
    )


def get_upcoming_cards(limit: int = 10) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT id, title, difficulty, topics, next_review_at, interval_days
        FROM problems
        WHERE user_email = ? AND archived = 0 AND next_review_at > ?
        ORDER BY next_review_at ASC, lower(title) ASC
        LIMIT ?
        """,
        (active_user_email(), today_iso(), limit),
    )


def get_review_history(problem_id: int, limit: int = 12) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT reviewed_at, rating, previous_interval, new_interval, previous_ease, new_ease, next_review_at
        FROM reviews
        WHERE problem_id = ? AND user_email = ?
        ORDER BY reviewed_at DESC
        LIMIT ?
        """,
        (problem_id, active_user_email(), limit),
    )


def schedule_review(card: dict[str, Any], rating: str) -> dict[str, Any]:
    previous_interval = float(card.get("interval_days") or 0)
    previous_ease = float(card.get("ease_factor") or 2.5)
    repetitions = int(card.get("repetitions") or 0)
    lapses = int(card.get("lapses") or 0)

    ease = previous_ease
    interval = previous_interval

    if rating == "Again":
        ease = max(1.3, ease - 0.2)
        repetitions = 0
        lapses += 1
        interval = 1
    elif rating == "Hard":
        ease = max(1.3, ease - 0.15)
        repetitions = max(1, repetitions)
        interval = 1 if previous_interval < 1 else max(1, round(previous_interval * 1.2, 1))
    elif rating == "Good":
        repetitions += 1
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 3
        else:
            interval = max(1, round(previous_interval * ease, 1))
    elif rating == "Easy":
        ease = min(3.2, ease + 0.15)
        repetitions += 1
        if repetitions == 1:
            interval = 4
        elif repetitions == 2:
            interval = 7
        else:
            interval = max(4, round(previous_interval * ease * 1.25, 1))
    else:
        raise ValueError(f"Unknown rating: {rating}")

    days_until_due = max(1, math.ceil(interval))
    next_review = date.today() + timedelta(days=days_until_due)

    return {
        "rating": rating,
        "previous_interval": previous_interval,
        "new_interval": interval,
        "previous_ease": previous_ease,
        "new_ease": ease,
        "repetitions": repetitions,
        "lapses": lapses,
        "next_review_at": next_review.isoformat(),
    }


def apply_review(problem_id: int, rating: str) -> None:
    card = get_problem(problem_id)
    if not card:
        return

    result = schedule_review(card, rating)
    reviewed_at = utc_now()

    with connect() as conn:
        conn.execute(
            """
            UPDATE problems
            SET last_reviewed_at = ?,
                next_review_at = ?,
                interval_days = ?,
                ease_factor = ?,
                repetitions = ?,
                lapses = ?,
                review_count = review_count + 1,
                updated_at = ?
            WHERE id = ? AND user_email = ?
            """,
            (
                reviewed_at,
                result["next_review_at"],
                result["new_interval"],
                result["new_ease"],
                result["repetitions"],
                result["lapses"],
                reviewed_at,
                problem_id,
                active_user_email(),
            ),
        )
        conn.execute(
            """
            INSERT INTO reviews (
                problem_id, user_email, reviewed_at, rating, previous_interval, new_interval,
                previous_ease, new_ease, next_review_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                problem_id,
                active_user_email(),
                reviewed_at,
                result["rating"],
                result["previous_interval"],
                result["new_interval"],
                result["previous_ease"],
                result["new_ease"],
                result["next_review_at"],
            ),
        )
    log_audit(
        "review_recorded",
        "review",
        {
            "problem_id": problem_id,
            "rating": rating,
            "next_review_at": result["next_review_at"],
            "new_interval": result["new_interval"],
        },
    )


def stats() -> dict[str, Any]:
    today = today_iso()
    base = fetch_one(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN archived = 0 THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN archived = 0 AND next_review_at <= ? THEN 1 ELSE 0 END) AS due,
            SUM(CASE WHEN archived = 0 AND review_count = 0 THEN 1 ELSE 0 END) AS new_cards,
            SUM(CASE WHEN archived = 0 AND interval_days >= 21 THEN 1 ELSE 0 END) AS mature,
            AVG(CASE WHEN archived = 0 THEN ease_factor END) AS avg_ease
        FROM problems
        WHERE user_email = ?
        """,
        (today, active_user_email()),
    ) or {}
    if base.get("avg_ease") is not None:
        base["avg_ease"] = round(float(base["avg_ease"]), 2)
    seven_days_ago = (date.today() - timedelta(days=6)).isoformat()
    review_count = fetch_one(
        "SELECT COUNT(*) AS count FROM reviews WHERE user_email = ? AND reviewed_at >= ?",
        (active_user_email(), seven_days_ago),
    ) or {"count": 0}
    base["reviews_7d"] = review_count["count"]
    return base


def decode_uploaded_file(file: Any) -> str:
    if file is None:
        return ""
    return file.getvalue().decode("utf-8", errors="replace")


def topics_to_text(selected: list[str], custom: str) -> str:
    values = selected + [item.strip() for item in custom.split(",") if item.strip()]
    deduped: list[str] = []
    for topic in values:
        if topic not in deduped:
            deduped.append(topic)
    return ", ".join(deduped)


def topic_badges(topics: str) -> str:
    if not topics:
        return "No topics"
    return " | ".join(topic.strip() for topic in topics.split(",") if topic.strip())


def format_due(value: str | None) -> str:
    due = parse_date(value)
    if due is None:
        return "No date"

    delta = (due - date.today()).days
    if delta < 0:
        return f"{abs(delta)} day(s) overdue"
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    return f"In {delta} days"


def is_web_url(value: str | None) -> bool:
    if not value:
        return False
    return value.startswith("http://") or value.startswith("https://")


class GithubImportError(Exception):
    pass


def parse_github_repo(value: str) -> tuple[str, str]:
    cleaned = value.strip().strip("/").removesuffix(".git")
    if not cleaned:
        raise GithubImportError("Enter a GitHub repository URL or owner/repo.")

    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        parsed = urlparse(cleaned)
        if parsed.netloc.lower() != "github.com":
            raise GithubImportError("The repository URL must be from github.com.")
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = [part for part in cleaned.split("/") if part]

    if len(parts) < 2:
        raise GithubImportError("Use a repo like your-username/neetcode-submissions.")
    return parts[0], parts[1]


def github_headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "dsa-flashcards-importer",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def github_read_json(url: str, token: str = "") -> dict[str, Any]:
    request = Request(url, headers=github_headers(token))
    try:
        with urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 403:
            raise GithubImportError("GitHub rate limited the request. Try again later or add a GitHub token.") from exc
        if exc.code == 404:
            raise GithubImportError("GitHub could not find that repo, branch, or file.") from exc
        raise GithubImportError(f"GitHub returned HTTP {exc.code}.") from exc
    except URLError as exc:
        raise GithubImportError(f"Could not reach GitHub: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise GithubImportError("GitHub returned data the app could not parse.") from exc


def github_read_text(url: str, token: str = "") -> str:
    request = Request(url, headers=github_headers(token))
    try:
        with urlopen(request, timeout=25) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise GithubImportError(f"Could not download a submission file. GitHub returned HTTP {exc.code}.") from exc
    except URLError as exc:
        raise GithubImportError(f"Could not download a submission file: {exc.reason}") from exc


def github_default_branch(owner: str, repo: str, token: str = "") -> str:
    data = github_read_json(f"https://api.github.com/repos/{owner}/{repo}", token)
    return str(data.get("default_branch") or "main")


def github_tree(owner: str, repo: str, branch: str, token: str = "") -> list[dict[str, Any]]:
    encoded_branch = quote(branch, safe="")
    data = github_read_json(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{encoded_branch}?recursive=1",
        token,
    )
    if data.get("truncated"):
        raise GithubImportError("GitHub truncated the repo tree. Narrow the repo or import fewer files.")
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]


def submission_index(path: str) -> int:
    match = re.search(r"submission-(\d+)\.[^.]+$", path)
    return int(match.group(1)) if match else -1


def slug_to_title(slug: str) -> str:
    words = re.split(r"[-_]+", slug.strip())
    return " ".join(word.upper() if word in {"sql", "api"} else word.capitalize() for word in words if word)


def select_latest_submissions(
    tree: list[dict[str, Any]],
    extension: str,
    max_cards: int,
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in tree:
        path = str(item.get("path") or "")
        parts = path.split("/")
        if len(parts) < 3:
            continue
        if not path.endswith(extension):
            continue
        if not re.search(r"/submission-\d+\.[^.]+$", f"/{path}"):
            continue

        problem_dir = "/".join(parts[:-1])
        current = {
            "path": path,
            "problem_dir": problem_dir,
            "problem_slug": parts[-2],
            "topic": " / ".join(parts[:-2]),
            "submission_number": submission_index(path),
            "extension": extension,
        }
        previous = latest.get(problem_dir)
        if previous is None or current["submission_number"] > previous["submission_number"]:
            latest[problem_dir] = current

    return sorted(latest.values(), key=lambda item: item["path"])[:max_cards]


def build_import_problem(
    owner: str,
    repo: str,
    branch: str,
    submission: dict[str, Any],
    solution: str,
) -> dict[str, Any]:
    title = slug_to_title(submission["problem_slug"])
    problem_url = f"https://neetcode.io/problems/{submission['problem_slug']}"
    source_path = submission["path"]
    imported_at = utc_now()
    question = (
        f"Try to solve {title} from memory.\n\n"
        "This card was imported from your NeetCode GitHub Sync repo. "
        "Open the problem link if you need the exact wording, then compare your approach with the saved solution.\n\n"
        f"Source file: {source_path}"
    )
    intuition = "Add the core pattern, edge cases, and mistake notes here after your next review."
    external_source = f"github:{owner}/{repo}"
    external_id = f"{branch}:{submission['problem_dir']}:{submission['extension']}"
    return {
        "title": title,
        "source": "Neetcode GitHub Sync",
        "url": problem_url,
        "difficulty": "Unknown",
        "topics": submission["topic"],
        "status": "Solved",
        "question": question,
        "intuition": intuition,
        "solution": solution,
        "solved_at": today_iso(),
        "external_source": external_source,
        "external_id": external_id,
        "imported_at": imported_at,
        "user_email": active_user_email(),
    }


def raw_github_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch, safe='')}/{quote(path, safe='/')}"


def html_text(value: Any) -> str:
    return escape(str(value or "")).replace("\n", "<br>")


def render_page_intro(title: str, body: str, eyebrow: str = "Workspace") -> None:
    st.markdown(
        f"""
        <section class="page-intro">
            <div>
                <p class="eyebrow">{html_text(eyebrow)}</p>
                <h1>{html_text(title)}</h1>
                <p>{html_text(body)}</p>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_stat_card(label: str, value: Any, helper: str = "", accent: str = "teal") -> None:
    helper_markup = f"<span>{html_text(helper)}</span>" if helper else ""
    st.markdown(
        f"""
        <div class="metric-card metric-card-{accent}">
            <p>{html_text(label)}</p>
            <strong>{html_text(value)}</strong>
            {helper_markup}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_tags(topics: str) -> str:
    tags = [topic.strip() for topic in topics.split(",") if topic.strip()] if topics else []
    if not tags:
        return '<span class="tag tag-muted">No topics</span>'
    return "".join(f'<span class="tag">{html_text(topic)}</span>' for topic in tags[:5])


def render_notice(title: str, body: str, tone: str = "neutral") -> None:
    st.markdown(
        f"""
        <div class="notice notice-{tone}">
            <strong>{html_text(title)}</strong>
            <p>{html_text(body)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_text_panel(title: str, body: str, meta: str = "") -> None:
    meta_markup = f'<span class="panel-meta">{html_text(meta)}</span>' if meta else ""
    body_markup = html_text(body) or "No content saved yet."
    st.markdown(
        f"""
        <section class="text-panel">
            <div class="panel-heading">
                <h3>{html_text(title)}</h3>
                {meta_markup}
            </div>
            <div class="panel-body">{body_markup}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def is_import_placeholder(card: dict[str, Any]) -> bool:
    question = card.get("question") or ""
    return (
        question.startswith("Imported from NeetCode GitHub Sync.")
        or "imported from your NeetCode GitHub Sync repo" in question
        or (card.get("source") == "Neetcode GitHub Sync" and not question.strip())
    )


def exact_prompt_value(card: dict[str, Any]) -> str:
    if is_import_placeholder(card):
        return ""
    return card.get("question") or ""


def render_review_prompt(card: dict[str, Any]) -> None:
    if is_import_placeholder(card):
        st.markdown(
            f"""
            <section class="text-panel friendly-prompt">
                <div class="panel-heading">
                    <h3>No exact prompt saved yet</h3>
                    <span class="panel-meta">Step 1</span>
                </div>
                <div class="panel-body">
                    <p class="friendly-question">Try to recall <strong>{html_text(card['title'])}</strong>.</p>
                    <p>This card came from your saved solution code. Paste the exact problem wording below whenever you want this card to test the full prompt.</p>
                    <div style="margin-top: 0.75rem;">{render_tags(card.get("topics") or "")}</div>
                </div>
            </section>
            """,
            unsafe_allow_html=True,
        )
        return

    render_text_panel("Problem wording", card.get("question") or "No problem prompt saved yet.", "Step 1")


def render_progress(label: str, value: int, helper: str) -> None:
    bounded_value = max(0, min(100, value))
    st.markdown(
        f"""
        <div class="progress-shell">
            <div class="progress-row">
                <span>{html_text(label)}</span>
                <strong>{bounded_value}%</strong>
            </div>
            <div class="progress-track">
                <div class="progress-fill" style="width: {bounded_value}%;"></div>
            </div>
            <div class="progress-row" style="margin-top: 0.7rem; margin-bottom: 0;">
                <span>{html_text(helper)}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_upcoming_list(cards: list[dict[str, Any]]) -> None:
    if not cards:
        render_notice("No future reviews", "Add a solved problem or clear today's due queue to populate the schedule.")
        return

    rows = []
    for card in cards:
        rows.append(
            '<div class="upcoming-row">'
            "<div>"
            f"<strong>{html_text(card['title'])}</strong>"
            f"<span>{html_text(card['difficulty'])} | {html_text(topic_badges(card['topics']))}</span>"
            "</div>"
            f'<div class="upcoming-date">{html_text(format_due(card["next_review_at"]))}</div>'
            "</div>"
        )

    st.markdown(
        (
            '<section class="content-card">'
            "<h3>Upcoming Reviews</h3>"
            "<p>The next cards scheduled by your recall history.</p>"
            f"{''.join(rows)}"
            "</section>"
        ),
        unsafe_allow_html=True,
    )


def render_learning_panel() -> None:
    st.markdown(
        """
        <section class="content-card">
            <h3>Memory Strategy</h3>
            <p>Keep each card focused on the recognition signal: problem shape, pattern, trap, and clean Python solution.</p>
            <div style="margin-top: 0.85rem;">
                <span class="tag">Pattern first</span>
                <span class="tag">Recall before reveal</span>
                <span class="tag">Review by due date</span>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_sign_in() -> None:
    st.markdown(
        """
        <section class="auth-shell">
            <div class="auth-hero">
                <div class="auth-copy">
                    <p class="eyebrow">DSA Flashcards</p>
                    <h1>Remember the patterns you already solved.</h1>
                    <p class="auth-lede">
                        Import solved NeetCode submissions, write memory notes, and review each problem on a schedule
                        that adapts to how well you recall it.
                    </p>
                    <div class="auth-pill-row">
                        <span class="auth-pill auth-pill-teal">GitHub import</span>
                        <span class="auth-pill auth-pill-indigo">Private queue</span>
                        <span class="auth-pill auth-pill-gold">Recall schedule</span>
                    </div>
                </div>
                <div class="auth-preview" aria-hidden="true">
                    <div class="auth-preview-top">
                        <span>Review Queue</span>
                        <strong>Today</strong>
                    </div>
                    <div class="auth-preview-card auth-preview-active">
                        <div>
                            <strong>Anagram Groups</strong>
                            <span>Arrays & Hashing</span>
                        </div>
                        <em>Due now</em>
                    </div>
                    <div class="auth-preview-card">
                        <div>
                            <strong>Longest Substring</strong>
                            <span>Sliding Window</span>
                        </div>
                        <em>3d</em>
                    </div>
                    <div class="auth-flow">
                        <span>Recall</span>
                        <span>Work</span>
                        <span>Compare</span>
                    </div>
                </div>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        """
        <section class="auth-action-panel">
            <div>
                <p class="eyebrow">Personal Workspace</p>
                <h2>Your cards stay tied to your email.</h2>
                <p>Each learner gets a separate flashcard library, GitHub source, notes, and review history.</p>
            </div>
            <div class="auth-feature-grid">
                <div>
                    <strong>1</strong>
                    <span>Add or import solved problems</span>
                </div>
                <div>
                    <strong>2</strong>
                    <span>Recall before revealing code</span>
                </div>
                <div>
                    <strong>3</strong>
                    <span>Rate recall to schedule the next review</span>
                </div>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="google-signin-anchor" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    if st.button("Sign in with Google", type="primary", key="google_sign_in"):
        st.login()


def render_sidebar_account() -> None:
    st.sidebar.markdown(
        f"""
        <div class="sidebar-db">
            <span>Signed in as</span>
            <strong>{html_text(active_user_name())}</strong>
            <code>{html_text(active_user_email())}</code>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if auth_configured():
        if st.sidebar.button("Sign out", width="stretch"):
            st.logout()
    else:
        with st.sidebar.expander("Switch local user"):
            email = st.text_input("Email", value=active_user_email(), key="local_user_email_input")
            name = st.text_input("Name", value=active_user_name(), key="local_user_name_input")
            if st.button("Use this user", key="switch_local_user", width="stretch"):
                st.session_state["user_email"] = normalize_email(email)
                st.session_state["user_name"] = name.strip() or normalize_email(email)
                st.rerun()


def render_review_steps(solution_visible: bool) -> None:
    reveal_state = "step-done" if solution_visible else "step-active"
    rate_state = "step-active" if solution_visible else "step-idle"
    st.markdown(
        f"""
        <section class="study-flow">
            <div class="study-step step-done">
                <span>1</span>
                <strong>Recall</strong>
                <p>Read the card and think of the pattern.</p>
            </div>
            <div class="study-step {reveal_state}">
                <span>2</span>
                <strong>Work</strong>
                <p>Write your attempt in the practice space.</p>
            </div>
            <div class="study-step {rate_state}">
                <span>3</span>
                <strong>Compare</strong>
                <p>Reveal, rate recall, and schedule.</p>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_practice_workspace(card: dict[str, Any]) -> None:
    st.markdown(
        """
        <section class="practice-shell">
            <div>
                <p class="eyebrow">Practice Space</p>
                <h3>Your Attempt</h3>
                <p>Write the approach or code before revealing the saved solution.</p>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )

    attempt_key = f"attempt_{card['id']}"
    st.text_area(
        "Your attempt",
        key=attempt_key,
        height=330,
        label_visibility="collapsed",
        placeholder=(
            "Example:\n"
            "1. Identify the pattern\n"
            "2. Write the key invariant\n"
            "3. Code the solution from memory"
        ),
    )
    action_cols = st.columns([1, 1])
    if is_web_url(card.get("url")):
        action_cols[0].link_button("Open exact problem", card["url"], width="stretch")
    if action_cols[1].button("Clear attempt", key=f"clear_attempt_{card['id']}", width="stretch"):
        st.session_state[attempt_key] = ""
        st.rerun()


def render_header() -> None:
    st.set_page_config(page_title="DSA Flashcards", page_icon="[]", layout="wide")
    st.markdown(
        """
        <style>
        :root {
            --bg: #f5f8f6;
            --surface: #ffffff;
            --surface-soft: #eef5f1;
            --ink: #17211d;
            --muted: #66736e;
            --border: #dce6e0;
            --teal: #0f766e;
            --teal-soft: #d9f3ed;
            --coral: #d45d48;
            --coral-soft: #fae4de;
            --indigo: #4656a5;
            --indigo-soft: #e5e8fb;
            --gold: #b98717;
            --gold-soft: #faefd1;
        }

        .stApp {
            background:
                radial-gradient(circle at top left, rgba(15, 118, 110, 0.08), transparent 34rem),
                linear-gradient(180deg, #f8faf9 0%, var(--bg) 48%, #f2f6f4 100%);
            color: var(--ink);
            color-scheme: light;
        }

        .block-container {
            max-width: 1180px;
            padding: 1.35rem 2.25rem 3rem;
        }

        header[data-testid="stHeader"] {
            background: transparent;
            height: 2.25rem;
        }

        [data-testid="stSidebar"] {
            background: #111a17;
            border-right: 1px solid rgba(255, 255, 255, 0.08);
        }

        [data-testid="stSidebar"] * {
            color: #eef6f2;
        }

        [data-testid="stSidebar"] .stRadio > label {
            color: #9fb2aa;
            font-size: 0.84rem;
        }

        [data-testid="stSidebar"] div[role="radiogroup"] {
            gap: 0.45rem;
        }

        [data-testid="stSidebar"] div[role="radiogroup"] label {
            min-height: 2.55rem;
            padding: 0.48rem 0.65rem;
            border-radius: 8px;
            border: 1px solid transparent;
            transition: 140ms ease;
        }

        [data-testid="stSidebar"] div[role="radiogroup"] label:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.12);
        }

        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
            background: rgba(15, 118, 110, 0.26);
            border-color: rgba(134, 239, 172, 0.22);
        }

        [data-testid="stSidebar"] hr {
            border-color: rgba(255, 255, 255, 0.1);
        }

        .sidebar-brand {
            padding: 0.55rem 0.15rem 1.2rem;
        }

        .sidebar-brand strong {
            display: block;
            font-size: 1.16rem;
            color: #ffffff;
        }

        .sidebar-brand span {
            display: block;
            margin-top: 0.25rem;
            color: #aebeb7;
            font-size: 0.88rem;
        }

        .sidebar-db {
            padding: 0.8rem 0.75rem;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }

        .sidebar-db span {
            display: block;
            color: #9fb2aa;
            font-size: 0.76rem;
            font-weight: 720;
        }

        .sidebar-db strong {
            display: block;
            margin-top: 0.18rem;
            color: #ffffff;
            font-size: 0.9rem;
            line-height: 1.25;
            overflow-wrap: anywhere;
        }

        .sidebar-db code {
            display: block;
            margin-top: 0.45rem;
            padding: 0;
            background: transparent;
            color: #aebeb7;
            font-size: 0.72rem;
            line-height: 1.35;
            white-space: normal;
            word-break: break-word;
        }

        .page-intro {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 1rem;
            padding: 1.15rem 0 0.8rem;
            border-bottom: 1px solid var(--border);
            margin-bottom: 1.15rem;
        }

        .page-intro h1 {
            margin: 0;
            font-size: clamp(2rem, 3vw, 2.85rem);
            line-height: 1.02;
            font-weight: 760;
            color: var(--ink);
        }

        .page-intro p {
            max-width: 720px;
            margin: 0.55rem 0 0;
            color: var(--muted);
            font-size: 1rem;
            line-height: 1.55;
        }

        .page-intro .eyebrow {
            margin: 0 0 0.35rem;
            color: var(--teal);
            font-size: 0.82rem;
            font-weight: 720;
        }

        .auth-shell {
            max-width: 1180px;
            margin: 1.15rem auto 0;
            padding-bottom: 2rem;
        }

        .auth-hero {
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
            gap: 2rem;
            align-items: stretch;
            min-height: 28rem;
            padding: clamp(1.4rem, 3vw, 2.35rem);
            border-radius: 8px;
            background:
                linear-gradient(135deg, rgba(17, 26, 23, 0.98), rgba(20, 53, 48, 0.96) 54%, rgba(43, 48, 86, 0.94)),
                repeating-linear-gradient(90deg, rgba(255,255,255,0.055) 0 1px, transparent 1px 56px);
            border: 1px solid rgba(255, 255, 255, 0.14);
            box-shadow: 0 28px 70px rgba(23, 33, 29, 0.18);
            overflow: hidden;
            position: relative;
        }

        .auth-hero::before {
            content: "";
            position: absolute;
            inset: auto 0 0;
            height: 6px;
            background: linear-gradient(90deg, var(--teal), var(--gold), var(--coral), var(--indigo));
        }

        .auth-copy,
        .auth-preview {
            position: relative;
            z-index: 1;
        }

        .auth-copy {
            display: flex;
            flex-direction: column;
            justify-content: center;
            max-width: 690px;
        }

        .auth-copy .eyebrow,
        .auth-action-panel .eyebrow {
            margin: 0 0 0.65rem;
            color: #93e3d6 !important;
            font-size: 0.8rem;
            font-weight: 760;
            letter-spacing: 0;
            text-transform: uppercase;
        }

        .auth-copy h1 {
            max-width: 730px;
            margin: 0;
            color: #ffffff !important;
            font-size: clamp(2.6rem, 5vw, 4.8rem);
            line-height: 0.98;
            letter-spacing: 0;
            font-weight: 790;
        }

        .auth-lede {
            max-width: 650px;
            margin: 1.15rem 0 0;
            color: #dbe7e3 !important;
            font-size: clamp(1rem, 1.5vw, 1.18rem);
            line-height: 1.58;
        }

        .auth-pill-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
            margin-top: 1.35rem;
        }

        .auth-pill {
            display: inline-flex;
            align-items: center;
            min-height: 2rem;
            padding: 0.34rem 0.7rem;
            border-radius: 999px;
            font-size: 0.84rem;
            font-weight: 720;
            border: 1px solid rgba(255, 255, 255, 0.16);
            color: #ffffff;
        }

        .auth-pill-teal { background: rgba(15, 118, 110, 0.35); }
        .auth-pill-indigo { background: rgba(70, 86, 165, 0.36); }
        .auth-pill-gold { background: rgba(185, 135, 23, 0.32); }

        .auth-preview {
            align-self: center;
            padding: 1rem;
            border-radius: 8px;
            background: rgba(248, 250, 249, 0.96);
            border: 1px solid rgba(255, 255, 255, 0.58);
            box-shadow: 0 24px 55px rgba(3, 7, 18, 0.24);
        }

        .auth-preview-top,
        .auth-preview-card,
        .auth-flow {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 0.9rem;
        }

        .auth-preview-top {
            margin-bottom: 0.85rem;
            color: var(--muted);
            font-size: 0.84rem;
            font-weight: 720;
        }

        .auth-preview-top strong {
            color: var(--coral);
        }

        .auth-preview-card {
            min-height: 4.7rem;
            margin-bottom: 0.72rem;
            padding: 0.85rem;
            border-radius: 8px;
            background: #ffffff;
            border: 1px solid var(--border);
        }

        .auth-preview-active {
            border-color: rgba(15, 118, 110, 0.34);
            box-shadow: inset 4px 0 0 var(--teal);
        }

        .auth-preview-card strong,
        .auth-preview-card span {
            display: block;
        }

        .auth-preview-card strong {
            color: var(--ink);
            font-size: 0.98rem;
        }

        .auth-preview-card span {
            margin-top: 0.25rem;
            color: var(--muted);
            font-size: 0.83rem;
        }

        .auth-preview-card em {
            font-style: normal;
            color: var(--teal);
            font-size: 0.82rem;
            font-weight: 760;
            white-space: nowrap;
        }

        .auth-flow {
            margin-top: 0.95rem;
            padding-top: 0.95rem;
            border-top: 1px solid var(--border);
        }

        .auth-flow span {
            flex: 1;
            min-height: 2rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 8px;
            background: var(--surface-soft);
            color: var(--ink);
            font-size: 0.78rem;
            font-weight: 740;
        }

        .auth-action-panel {
            display: grid;
            grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
            gap: 1.1rem;
            margin-top: 1rem;
            padding: 1rem;
            border-radius: 8px;
            background: var(--surface);
            border: 1px solid var(--border);
            box-shadow: 0 16px 38px rgba(23, 33, 29, 0.08);
        }

        .auth-action-panel .eyebrow {
            color: var(--teal);
        }

        .auth-action-panel h2 {
            margin: 0;
            color: var(--ink);
            font-size: 1.28rem;
            line-height: 1.2;
        }

        .auth-action-panel p {
            margin: 0.45rem 0 0;
            color: var(--muted);
            line-height: 1.5;
        }

        .auth-feature-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.65rem;
        }

        .auth-feature-grid div {
            min-height: 6rem;
            padding: 0.8rem;
            border-radius: 8px;
            background: #fbfdfc;
            border: 1px solid var(--border);
        }

        .auth-feature-grid strong {
            width: 1.75rem;
            height: 1.75rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            background: var(--teal-soft);
            color: var(--teal);
            font-size: 0.86rem;
        }

        .auth-feature-grid span {
            display: block;
            margin-top: 0.7rem;
            color: var(--ink);
            line-height: 1.35;
            font-size: 0.9rem;
            font-weight: 680;
        }

        [data-testid="stVerticalBlock"]:has(.google-signin-anchor) .stButton > button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.62rem;
            min-width: 15rem;
            min-height: 3rem;
            margin-top: 0.85rem;
            font-size: 1rem;
        }

        [data-testid="stVerticalBlock"]:has(.google-signin-anchor) .stButton > button::before {
            content: "";
            width: 1.35rem;
            height: 1.35rem;
            flex: 0 0 auto;
            border-radius: 999px;
            background-color: #ffffff;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%234285F4' d='M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z'/%3E%3Cpath fill='%2334A853' d='M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z'/%3E%3Cpath fill='%23FBBC05' d='M5.84 14.1c-.22-.66-.35-1.36-.35-2.1s.13-1.44.35-2.1V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l3.66-2.84z'/%3E%3Cpath fill='%23EA4335' d='M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06L5.84 9.9C6.71 7.3 9.14 5.38 12 5.38z'/%3E%3C/svg%3E");
            background-position: center;
            background-repeat: no-repeat;
            background-size: 0.94rem 0.94rem;
        }

        .metric-card {
            min-height: 8.5rem;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
            box-shadow: 0 10px 28px rgba(23, 33, 29, 0.06);
            position: relative;
            overflow: hidden;
        }

        .metric-card::before {
            content: "";
            position: absolute;
            inset: 0 0 auto 0;
            height: 4px;
            background: var(--teal);
        }

        .metric-card-coral::before { background: var(--coral); }
        .metric-card-indigo::before { background: var(--indigo); }
        .metric-card-gold::before { background: var(--gold); }

        .metric-card p {
            margin: 0 0 0.75rem;
            color: var(--muted);
            font-size: 0.88rem;
        }

        .metric-card strong {
            display: block;
            color: var(--ink);
            font-size: clamp(1.65rem, 3vw, 2.25rem);
            line-height: 1;
        }

        .metric-card span {
            display: block;
            margin-top: 0.65rem;
            color: var(--muted);
            font-size: 0.86rem;
            line-height: 1.35;
        }

        .progress-shell {
            margin: 0.45rem 0 1.4rem;
            padding: 1rem;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            box-shadow: 0 10px 28px rgba(23, 33, 29, 0.045);
        }

        .progress-row {
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            margin-bottom: 0.7rem;
            color: var(--muted);
            font-size: 0.9rem;
        }

        .progress-row strong {
            color: var(--ink);
        }

        .progress-track {
            height: 0.68rem;
            background: #e4ece8;
            border-radius: 999px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, var(--teal), var(--indigo));
        }

        .content-card,
        .text-panel,
        .notice {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            box-shadow: 0 10px 28px rgba(23, 33, 29, 0.045);
        }

        .content-card {
            padding: 1rem;
            margin-bottom: 1rem;
        }

        .content-card h3,
        .text-panel h3 {
            margin: 0;
            color: var(--ink);
            font-size: 1.05rem;
            line-height: 1.25;
        }

        .content-card p {
            margin: 0.35rem 0 0;
            color: var(--muted);
            line-height: 1.5;
        }

        .text-panel {
            overflow: hidden;
            margin: 0.75rem 0 1rem;
        }

        .panel-heading {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            padding: 0.9rem 1rem;
            border-bottom: 1px solid var(--border);
            background: #fbfdfc;
        }

        .panel-meta {
            color: var(--muted);
            font-size: 0.86rem;
            white-space: nowrap;
        }

        .panel-body {
            padding: 1rem;
            color: var(--ink);
            font-size: 0.98rem;
            line-height: 1.58;
        }

        .friendly-prompt .panel-body {
            background: #fbfdfc;
        }

        .friendly-question {
            margin-top: 0;
            font-size: 1.08rem;
            color: var(--ink);
        }

        .friendly-question strong {
            color: var(--teal);
        }

        .tag {
            display: inline-flex;
            align-items: center;
            min-height: 1.65rem;
            padding: 0.25rem 0.55rem;
            margin: 0.2rem 0.25rem 0 0;
            border-radius: 999px;
            background: var(--teal-soft);
            color: #0b5753;
            border: 1px solid rgba(15, 118, 110, 0.14);
            font-size: 0.82rem;
            line-height: 1.1;
        }

        .tag-muted {
            background: #eef2f0;
            color: var(--muted);
        }

        .notice {
            padding: 1rem;
            margin: 0.75rem 0 1rem;
        }

        .notice strong {
            display: block;
            color: var(--ink);
            font-size: 1rem;
        }

        .notice p {
            margin: 0.35rem 0 0;
            color: var(--muted);
            line-height: 1.5;
        }

        .notice-success {
            background: #f1fbf7;
            border-color: #c7ecdf;
        }

        .notice-warning {
            background: #fff8e8;
            border-color: #ecd89e;
        }

        .review-title {
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            align-items: flex-start;
            margin: 1.1rem 0 0.5rem;
        }

        .review-title h2 {
            margin: 0;
            color: var(--ink);
            font-size: clamp(1.55rem, 2.5vw, 2.25rem);
            line-height: 1.08;
        }

        .review-title p {
            margin: 0.45rem 0 0;
            color: var(--muted);
        }

        .due-chip {
            flex: 0 0 auto;
            padding: 0.45rem 0.7rem;
            border-radius: 999px;
            background: var(--coral-soft);
            color: #9f3526;
            border: 1px solid rgba(212, 93, 72, 0.2);
            font-size: 0.86rem;
            font-weight: 700;
        }

        .study-flow {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.75rem;
            margin: 1rem 0;
        }

        .study-step {
            display: grid;
            grid-template-columns: 2rem 1fr;
            gap: 0.15rem 0.65rem;
            align-items: center;
            min-height: 5.4rem;
            padding: 0.85rem;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            box-shadow: 0 10px 28px rgba(23, 33, 29, 0.04);
        }

        .study-step span {
            grid-row: span 2;
            width: 2rem;
            height: 2rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            background: #eef2f0;
            color: var(--muted);
            font-weight: 800;
        }

        .study-step strong {
            color: var(--ink);
            font-size: 0.96rem;
        }

        .study-step p {
            margin: 0;
            color: var(--muted);
            font-size: 0.84rem;
            line-height: 1.35;
        }

        .study-step.step-active {
            border-color: rgba(15, 118, 110, 0.35);
            background: #f2fbf8;
        }

        .study-step.step-active span {
            background: var(--teal);
            color: #ffffff;
        }

        .study-step.step-done span {
            background: var(--teal-soft);
            color: #0b5753;
        }

        .solution-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin: 1.3rem 0 0.65rem;
        }

        .solution-header h3 {
            margin: 0;
            color: var(--ink);
            font-size: clamp(1.25rem, 2vw, 1.75rem);
        }

        .solution-header p {
            margin: 0.2rem 0 0;
            color: var(--muted);
            font-size: 0.92rem;
        }

        .rating-intro {
            margin: 1.35rem 0 0.75rem;
        }

        .rating-intro h3 {
            margin: 0;
            color: var(--ink);
            font-size: clamp(1.25rem, 2vw, 1.65rem);
        }

        .rating-intro p {
            margin: 0.25rem 0 0;
            color: var(--muted);
        }

        .quick-note {
            margin: 0.85rem 0 1rem;
            padding: 1rem;
            background: #ffffff;
            border: 1px solid var(--border);
            border-radius: 8px;
            box-shadow: 0 10px 28px rgba(23, 33, 29, 0.04);
        }

        .quick-note h3 {
            margin: 0;
            color: var(--ink);
            font-size: 1.05rem;
        }

        .quick-note p {
            margin: 0.35rem 0 0.85rem;
            color: var(--muted);
            font-size: 0.9rem;
            line-height: 1.45;
        }

        .practice-shell {
            padding: 1rem;
            margin: 0 0 0.75rem;
            background: #ffffff;
            border: 1px solid var(--border);
            border-radius: 8px;
            box-shadow: 0 10px 28px rgba(23, 33, 29, 0.04);
        }

        .practice-shell h3 {
            margin: 0;
            color: var(--ink);
            font-size: 1.25rem;
        }

        .practice-shell p {
            margin: 0.35rem 0 0;
            color: var(--muted);
            line-height: 1.45;
        }

        .practice-shell .eyebrow {
            margin: 0 0 0.25rem;
            color: var(--teal);
            font-size: 0.78rem;
            font-weight: 800;
        }

        .upcoming-row {
            display: grid;
            grid-template-columns: minmax(0, 1.4fr) 8rem;
            gap: 0.9rem;
            align-items: center;
            padding: 0.8rem 0;
            border-bottom: 1px solid var(--border);
        }

        .upcoming-row:last-child {
            border-bottom: 0;
        }

        .upcoming-row strong {
            display: block;
            color: var(--ink);
            font-size: 0.98rem;
            line-height: 1.25;
        }

        .upcoming-row span {
            color: var(--muted);
            font-size: 0.86rem;
        }

        .upcoming-date {
            justify-self: end;
            color: var(--teal);
            font-weight: 720;
            font-size: 0.9rem;
            text-align: right;
        }

        .stButton > button,
        .stFormSubmitButton > button,
        .stDownloadButton > button,
        .stLinkButton > a {
            border-radius: 8px;
            min-height: 2.65rem;
            font-weight: 700;
            border: 1px solid var(--border);
            box-shadow: none;
        }

        .stButton > button[kind="primary"],
        .stFormSubmitButton > button,
        [data-testid="stBaseButton-primary"],
        .stDownloadButton > button[kind="primary"] {
            background: var(--teal) !important;
            border-color: var(--teal) !important;
            color: #ffffff !important;
        }

        .stButton > button[kind="primary"]:hover,
        .stFormSubmitButton > button:hover,
        [data-testid="stBaseButton-primary"]:hover {
            background: #0c625d !important;
            border-color: #0c625d !important;
        }

        [data-testid="stSidebar"] .stButton > button {
            background: rgba(255, 255, 255, 0.07) !important;
            border: 1px solid rgba(255, 255, 255, 0.16) !important;
            color: #eef6f2 !important;
            min-height: 2.55rem;
            box-shadow: none !important;
        }

        [data-testid="stSidebar"] .stButton > button:hover {
            background: rgba(212, 93, 72, 0.18) !important;
            border-color: rgba(248, 113, 113, 0.48) !important;
            color: #ffffff !important;
        }

        [data-testid="stSidebar"] .stButton > button:focus {
            box-shadow: 0 0 0 2px rgba(248, 113, 113, 0.18) !important;
            outline: none !important;
        }

        .stTextInput input,
        .stTextArea textarea,
        .stSelectbox div[data-baseweb="select"] > div,
        .stMultiSelect div[data-baseweb="select"] > div,
        .stDateInput input {
            border-radius: 8px;
            border-color: var(--border);
            background: #ffffff;
            color: var(--ink);
            caret-color: var(--ink);
        }

        .stTextArea textarea {
            line-height: 1.48;
        }

        [data-testid="stMain"] input,
        [data-testid="stMain"] textarea,
        [data-testid="stAppViewContainer"] input,
        [data-testid="stAppViewContainer"] textarea {
            caret-color: #111a17 !important;
            -webkit-text-fill-color: var(--ink) !important;
        }

        [data-testid="stMain"] input:focus,
        [data-testid="stMain"] textarea:focus,
        [data-testid="stAppViewContainer"] input:focus,
        [data-testid="stAppViewContainer"] textarea:focus,
        [data-testid="stMain"] div[data-baseweb="select"]:focus-within > div,
        [data-testid="stAppViewContainer"] div[data-baseweb="select"]:focus-within > div {
            border-color: var(--teal) !important;
            box-shadow: 0 0 0 2px rgba(15, 118, 110, 0.18) !important;
            outline: none !important;
        }

        [data-testid="stMain"] input::selection,
        [data-testid="stMain"] textarea::selection {
            background: rgba(15, 118, 110, 0.2);
            color: var(--ink);
        }

        [data-testid="stMain"] label,
        [data-testid="stMain"] label *,
        [data-testid="stAppViewContainer"] label,
        [data-testid="stAppViewContainer"] label *,
        [data-testid="stMain"] [data-testid="stWidgetLabel"],
        [data-testid="stMain"] [data-testid="stWidgetLabel"] *,
        [data-testid="stAppViewContainer"] [data-testid="stWidgetLabel"],
        [data-testid="stAppViewContainer"] [data-testid="stWidgetLabel"] *,
        [data-testid="stMain"] [data-testid="stFileUploader"] small,
        [data-testid="stMain"] [data-testid="stFileUploader"] p,
        [data-testid="stMain"] [data-testid="stFileUploaderDropzoneInstructions"] *,
        [data-testid="stAppViewContainer"] [data-testid="stFileUploader"] small,
        [data-testid="stAppViewContainer"] [data-testid="stFileUploader"] p,
        [data-testid="stAppViewContainer"] [data-testid="stFileUploaderDropzoneInstructions"] *,
        [data-testid="stMain"] [data-testid="stMarkdownContainer"] p {
            color: var(--ink) !important;
            opacity: 1 !important;
        }

        [data-testid="stMain"] [data-testid="stWidgetLabel"] {
            margin-bottom: 0.35rem;
        }

        [data-testid="stMain"] [data-testid="stWidgetLabel"] p,
        [data-testid="stMain"] label p {
            font-size: 0.86rem;
            font-weight: 720;
            line-height: 1.2;
        }

        [data-testid="stMain"] input,
        [data-testid="stMain"] textarea,
        [data-testid="stMain"] div[data-baseweb="select"] *,
        [data-testid="stMain"] div[data-baseweb="input"] *,
        [data-testid="stAppViewContainer"] input,
        [data-testid="stAppViewContainer"] textarea,
        [data-testid="stAppViewContainer"] div[data-baseweb="select"] *,
        [data-testid="stAppViewContainer"] div[data-baseweb="input"] * {
            color: var(--ink) !important;
            opacity: 1 !important;
            color-scheme: light;
        }

        [data-testid="stMain"] input::placeholder,
        [data-testid="stMain"] textarea::placeholder {
            color: #7d8b86 !important;
            opacity: 1 !important;
            -webkit-text-fill-color: #7d8b86 !important;
        }

        [data-testid="stFileUploader"] section {
            border-radius: 8px;
            border-color: var(--border);
            background: #fbfdfc;
        }

        [data-testid="stMain"] [data-testid="stFileUploader"] section {
            min-height: 4.3rem;
            border-style: solid;
        }

        [data-testid="stMain"] [data-testid="stFileUploader"] button {
            background: #17211d;
            color: #ffffff !important;
            border-color: #17211d;
        }

        [data-testid="stMain"] [data-testid="stFileUploader"] svg {
            color: var(--teal);
            opacity: 1;
        }

        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] label *,
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"],
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"] * {
            color: #eef6f2 !important;
        }

        [data-testid="stForm"] {
            background: #ffffff;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1.2rem;
            box-shadow: 0 16px 36px rgba(23, 33, 29, 0.08);
        }

        [data-testid="stDataFrame"] {
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 10px 28px rgba(23, 33, 29, 0.035);
        }

        code,
        pre {
            white-space: pre-wrap;
            border-radius: 8px;
        }

        @media (max-width: 760px) {
            .block-container {
                padding-left: 1rem;
                padding-right: 1rem;
            }

            .page-intro {
                align-items: flex-start;
            }

            .auth-shell {
                margin-top: 0.4rem;
            }

            .auth-hero,
            .auth-action-panel {
                grid-template-columns: 1fr;
            }

            .auth-hero {
                min-height: auto;
                padding: 1.15rem;
            }

            .auth-copy h1 {
                font-size: clamp(2.25rem, 12vw, 3.25rem);
            }

            .auth-preview {
                width: 100%;
            }

            .auth-feature-grid {
                grid-template-columns: 1fr;
            }

            [data-testid="stVerticalBlock"]:has(.google-signin-anchor) .stButton > button {
                width: 100%;
                min-width: 0;
            }

            .review-title,
            .progress-row {
                flex-direction: column;
            }

            .upcoming-row {
                grid-template-columns: 1fr;
            }

            .study-flow {
                grid-template-columns: 1fr;
            }

            .solution-header {
                align-items: flex-start;
                flex-direction: column;
            }

            .upcoming-date {
                justify-self: start;
                text-align: left;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def dashboard_screen() -> None:
    data = stats()
    active = data.get("active") or 0
    due = data.get("due") or 0
    mature = data.get("mature") or 0
    retention = round((mature / active) * 100) if active else 0

    render_page_intro(
        "Review Command Center",
        "A focused view of your solved DSA problems, due cards, and long-term retention.",
        "DSA Flashcards",
    )

    cols = st.columns(5)
    with cols[0]:
        render_stat_card("Active cards", active, "Problems in rotation", "teal")
    with cols[1]:
        render_stat_card("Due today", due, "Cards ready now", "coral")
    with cols[2]:
        render_stat_card("New cards", data.get("new_cards") or 0, "Never reviewed", "indigo")
    with cols[3]:
        render_stat_card("Mature cards", mature, "21+ day interval", "gold")
    with cols[4]:
        render_stat_card("Reviews, 7 days", data.get("reviews_7d") or 0, "Recent reps", "teal")

    render_progress(
        "Long-term retention",
        retention,
        f"{mature} of {active} active cards have reached a 21+ day interval.",
    )

    left, right = st.columns([1.2, 1])
    with left:
        render_upcoming_list(get_upcoming_cards(12))

    with right:
        render_learning_panel()


def add_problem_screen() -> None:
    render_page_intro(
        "Add Solved Problem",
        "Capture the prompt, pattern, and Python solution while the solution is still fresh.",
        "Intake",
    )

    upload_cols = st.columns(2)
    question_file = upload_cols[0].file_uploader("Question prompt file", type=["txt", "md"], key="question_file")
    solution_file = upload_cols[1].file_uploader("Python solution file", type=["py", "txt"], key="solution_file")
    question_seed = decode_uploaded_file(question_file)
    solution_seed = decode_uploaded_file(solution_file)

    with st.form("add_problem_form", clear_on_submit=False):
        title = st.text_input("Problem title", placeholder="Two Sum")
        url = st.text_input("Problem URL", placeholder="https://leetcode.com/problems/two-sum/")

        meta_cols = st.columns(4)
        source = meta_cols[0].selectbox("Source", SOURCES, index=0)
        difficulty = meta_cols[1].selectbox("Difficulty", DIFFICULTIES, index=1)
        status = meta_cols[2].selectbox("Status", ["Solved", "Needs revisit", "Learning", "Reference"], index=0)
        solved_at = meta_cols[3].date_input("Solved date", value=date.today())

        selected_topics = st.multiselect("Topics", COMMON_TOPICS)
        custom_topics = st.text_input("Extra topics", placeholder="Union Find, Prefix Sum")
        question = st.text_area("Question prompt", value=question_seed, height=220)
        intuition = st.text_area(
            "Pattern / intuition",
            placeholder="What is the key idea? What should future-you remember before seeing code?",
            height=150,
        )
        solution = st.text_area("Python solution", value=solution_seed, height=300)

        submitted = st.form_submit_button("Add card", type="primary")

    if submitted:
        if not title.strip():
            st.error("Add a title before saving the card.")
            return
        problem_id = insert_problem(
            {
                "title": title,
                "url": url,
                "source": source,
                "difficulty": difficulty,
                "status": status,
                "topics": topics_to_text(selected_topics, custom_topics),
                "question": question,
                "intuition": intuition,
                "solution": solution,
                "solved_at": solved_at.isoformat(),
            }
        )
        log_audit("problem_created", "problem", {"problem_id": problem_id, "title": title.strip(), "source": source})
        st.success(f"Saved card #{problem_id}: {title.strip()}")


def github_import_screen() -> None:
    saved_source = get_github_source() or {}
    language_options = list(SUPPORTED_IMPORT_EXTENSIONS.keys())
    saved_language = saved_source.get("language") or "Python (.py)"
    language_index = language_options.index(saved_language) if saved_language in language_options else 0

    render_page_intro(
        "GitHub Import",
        f"Pull synced NeetCode submissions into {active_user_email()}'s personal review queue.",
        "Automation",
    )

    st.markdown(
        """
        <section class="content-card">
            <h3>Repository Source</h3>
            <p>Imports the latest submission file per problem for the selected language.</p>
        </section>
        """,
        unsafe_allow_html=True,
    )

    repo_value = st.text_input(
        "GitHub repository",
        value=saved_source.get("repo_url") or "",
        placeholder=GENERIC_GITHUB_REPO_EXAMPLE,
    )
    meta_cols = st.columns([1, 1, 1])
    branch_value = meta_cols[0].text_input("Branch", value=saved_source.get("branch") or "main", placeholder="main")
    language_label = meta_cols[1].selectbox("Language", language_options, index=language_index)
    max_cards = int(
        meta_cols[2].number_input(
            "Max cards",
            min_value=1,
            max_value=500,
            value=int(saved_source.get("max_cards") or 150),
            step=10,
        )
    )

    option_cols = st.columns([1, 1])
    update_existing = option_cols[0].checkbox(
        "Refresh solutions for existing cards",
        value=True,
        help=(
            "Keeps your prompt, notes, topics, solved date, and review schedule. "
            "Only the saved solution/code is refreshed from GitHub."
        ),
    )
    token = option_cols[1].text_input("GitHub token", type="password", placeholder="Optional for private repos or rate limits")
    per_hour, per_day = github_import_limits()
    hour_count, day_count = github_import_usage()
    st.caption(
        f"Import guardrail: {hour_count}/{per_hour} used this hour, "
        f"{day_count}/{per_day} used in the last 24 hours."
    )

    if not st.button("Import from GitHub", type="primary"):
        return

    run_id: int | None = None
    try:
        owner, repo = parse_github_repo(repo_value)
        allowed, limit_message = check_github_import_rate_limit()
        if not allowed:
            log_audit(
                "github_import_rate_limited",
                "github_import",
                {"repo": f"{owner}/{repo}", "reason": limit_message},
            )
            render_notice("Import paused", limit_message, "warning")
            return

        branch = branch_value.strip() or github_default_branch(owner, repo, token)
        extension = SUPPORTED_IMPORT_EXTENSIONS[language_label]
        run_id = create_import_run(repo_value, branch, language_label)
        save_github_source(repo_value, branch, language_label, max_cards)
        log_audit(
            "github_import_started",
            "github_import",
            {"repo": f"{owner}/{repo}", "branch": branch, "language": language_label, "max_cards": max_cards},
        )

        with st.spinner("Reading GitHub repository tree..."):
            tree = github_tree(owner, repo, branch, token)
            submissions = select_latest_submissions(tree, extension, max_cards)

        if not submissions:
            finish_import_run(run_id, "no_submissions")
            log_audit(
                "github_import_empty",
                "github_import",
                {"repo": f"{owner}/{repo}", "branch": branch, "extension": extension},
            )
            render_notice(
                "No submissions found",
                f"No {extension} submission files were found under {owner}/{repo} on branch {branch}.",
                "warning",
            )
            return

        progress = st.progress(0, text="Downloading submissions...")
        created = 0
        updated = 0
        skipped = 0
        rows: list[dict[str, Any]] = []

        for index, submission in enumerate(submissions, start=1):
            progress.progress(index / len(submissions), text=f"Importing {submission['path']}")
            solution = github_read_text(raw_github_url(owner, repo, branch, submission["path"]), token)
            data = build_import_problem(owner, repo, branch, submission, solution)
            existing = get_problem_by_external(data["external_source"], data["external_id"])

            if existing and update_existing:
                update_imported_problem(int(existing["id"]), data)
                updated += 1
                action = "Solution refreshed"
            elif existing:
                skipped += 1
                action = "Skipped"
            else:
                insert_problem(data)
                created += 1
                action = "Created"

            rows.append(
                {
                    "Action": action,
                    "Problem": data["title"],
                    "Topic": data["topics"],
                    "File": submission["path"],
                }
            )

        progress.empty()
        finish_import_run(run_id, "success", created, updated, skipped, len(submissions))
        log_audit(
            "github_import_completed",
            "github_import",
            {
                "repo": f"{owner}/{repo}",
                "branch": branch,
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "files": len(submissions),
            },
        )
        render_notice(
            "Import complete",
            f"Created {created} card(s), updated {updated}, skipped {skipped}.",
            "success",
        )
        st.dataframe(rows, width="stretch", hide_index=True)

    except GithubImportError as exc:
        finish_import_run(run_id, "failed", error_message=str(exc))
        log_audit("github_import_failed", "github_import", {"error": str(exc)})
        render_notice("Import failed", str(exc), "warning")
    except Exception as exc:
        finish_import_run(run_id, "failed", error_message=f"{type(exc).__name__}: {exc}")
        record_app_error("github_import", exc)
        log_audit("github_import_error", "github_import", {"error": f"{type(exc).__name__}: {exc}"})
        render_notice(
            "Import failed",
            "Something unexpected happened during the import. The error was recorded for the admin dashboard.",
            "warning",
        )


def review_screen() -> None:
    due_cards = get_due_cards()
    render_page_intro(
        "Review Queue",
        "Recall the approach first, then reveal the saved solution and grade the memory trace.",
        "Spaced repetition",
    )

    if not due_cards:
        render_notice("Queue clear", "No cards are due right now.", "success")
        upcoming = get_upcoming_cards(8)
        if upcoming:
            render_upcoming_list(upcoming)
        return

    card = due_cards[0]

    top = st.columns([1, 1, 1, 1])
    with top[0]:
        render_stat_card("Current interval", f"{float(card['interval_days'] or 0):.1f}d", "Spacing now", "teal")
    with top[1]:
        render_stat_card("Ease", f"{float(card['ease_factor'] or 2.5):.2f}", "Recall strength", "indigo")
    with top[2]:
        render_stat_card("Reviews", int(card["review_count"] or 0), f"{len(due_cards)} due in queue", "gold")
    with top[3]:
        render_stat_card("Lapses", int(card["lapses"] or 0), "Forgotten recalls", "coral")

    st.markdown(
        f"""
        <div class="review-title">
            <div>
                <h2>{html_text(card['title'])}</h2>
                <p>{html_text(card['difficulty'])} | {render_tags(card['topics'])}</p>
            </div>
            <div class="due-chip">{html_text(format_due(card['next_review_at']))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    answer_key = f"answer_visible_{card['id']}"
    solution_visible = bool(st.session_state.get(answer_key))
    render_review_steps(solution_visible)

    study_left, study_right = st.columns([1.05, 0.95], gap="large")
    with study_left:
        render_review_prompt(card)
        with st.expander("Add or edit exact problem wording"):
            st.markdown(
                """
                <div class="quick-note">
                    <h3>Exact problem wording</h3>
                    <p>Paste the full prompt here if you want this card to test the exact wording instead of only the title and pattern.</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            prompt_key = f"exact_prompt_{card['id']}"
            exact_prompt = st.text_area(
                "Exact problem wording",
                value=exact_prompt_value(card),
                height=220,
                key=prompt_key,
                label_visibility="collapsed",
                placeholder="Paste the full LeetCode/NeetCode problem statement here.",
            )
            if st.button("Save problem wording", key=f"save_prompt_{card['id']}"):
                update_problem_notes(int(card["id"]), card.get("intuition") or "", exact_prompt)
                st.success("Problem wording saved.")
                st.rerun()

        with st.expander("Need a nudge? Pattern / intuition"):
            st.write(card.get("intuition") or "No intuition note saved yet.")

        with st.expander("Add or edit memory notes"):
            st.markdown(
                """
                <div class="quick-note">
                    <h3>Memory notes</h3>
                    <p>Write the pattern, mistake, edge case, or one-line trick you want future-you to recall.</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            note_key = f"notes_{card['id']}"
            new_notes = st.text_area(
                "Pattern notes",
                value=card.get("intuition") or "",
                height=150,
                key=note_key,
                label_visibility="collapsed",
                placeholder="Example: Group by 26-letter frequency tuple. Avoid sorting every word when optimizing.",
            )
            if st.button("Save notes", key=f"save_notes_{card['id']}"):
                update_problem_notes(int(card["id"]), new_notes)
                st.success("Notes saved.")
                st.rerun()

        if solution_visible:
            if st.button("Hide solution", key=f"hide_{card['id']}", width="stretch"):
                st.session_state[answer_key] = False
                st.rerun()
        else:
            if st.button(
                "Reveal solution",
                type="primary",
                key=f"reveal_{card['id']}",
                width="stretch",
            ):
                st.session_state[answer_key] = True
                st.rerun()

    with study_right:
        render_practice_workspace(card)

    if solution_visible:
        st.markdown(
            """
            <div class="solution-header">
                <div>
                    <h3>Saved Solution</h3>
                    <p>Compare this with what you remembered.</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.code(card.get("solution") or "# No solution saved yet.", language="python")

        st.markdown(
            """
            <div class="rating-intro">
                <h3>Rate Recall</h3>
                <p>Choose the button that best matches how much help you needed.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        cols = st.columns(4)
        rating_buttons = [
            ("Again", "Again - forgot"),
            ("Hard", "Hard - partial"),
            ("Good", "Good - solved"),
            ("Easy", "Easy - automatic"),
        ]
        for col, (rating, label) in zip(cols, rating_buttons):
            if col.button(label, help=RATING_HELP[rating], width="stretch", key=f"{rating}_{card['id']}"):
                apply_review(int(card["id"]), rating)
                st.session_state[answer_key] = False
                st.rerun()


def problem_matches(problem: dict[str, Any], search: str, difficulty: str, topic: str, archived_filter: str) -> bool:
    if difficulty != "All" and problem["difficulty"] != difficulty:
        return False
    if topic != "All" and topic.lower() not in (problem.get("topics") or "").lower():
        return False
    if archived_filter == "Active only" and problem["archived"]:
        return False
    if archived_filter == "Archived only" and not problem["archived"]:
        return False

    if not search:
        return True

    haystack = " ".join(
        str(problem.get(field) or "")
        for field in ["title", "url", "source", "difficulty", "topics", "question", "intuition", "solution"]
    ).lower()
    return search.lower() in haystack


def browse_screen() -> None:
    render_page_intro(
        "Problem Library",
        "Search your solved set, inspect review state, and refine saved prompts or solutions.",
        "Library",
    )
    all_problems = list_problems(include_archived=True)

    if not all_problems:
        render_notice("No cards yet", "Add your first solved problem from the Add Problem tab.")
        return

    filters = st.columns([1.3, 0.8, 1, 0.8])
    search = filters[0].text_input("Search", placeholder="title, topic, code, note...")
    difficulty = filters[1].selectbox("Difficulty", ["All"] + DIFFICULTIES)
    all_topics = sorted(
        {
            topic.strip()
            for problem in all_problems
            for topic in (problem.get("topics") or "").split(",")
            if topic.strip()
        }
    )
    topic = filters[2].selectbox("Topic", ["All"] + all_topics)
    archived_filter = filters[3].selectbox("Cards", ["Active only", "Archived only", "All"])

    filtered = [
        problem
        for problem in all_problems
        if problem_matches(problem, search, difficulty, topic, archived_filter)
    ]

    st.caption(f"{len(filtered)} of {len(all_problems)} card(s) shown")
    if not filtered:
        render_notice("No matches", "No cards match the current filters.")
        return

    st.dataframe(
        [
            {
                "ID": problem["id"],
                "Title": problem["title"],
                "Difficulty": problem["difficulty"],
                "Topics": topic_badges(problem["topics"]),
                "Status": problem["status"],
                "Due": format_due(problem["next_review_at"]),
                "Interval": f"{float(problem['interval_days'] or 0):.1f}d",
                "Archived": "Yes" if problem["archived"] else "No",
            }
            for problem in filtered
        ],
        width="stretch",
        hide_index=True,
    )

    selected_id = st.selectbox(
        "Edit card",
        [problem["id"] for problem in filtered],
        format_func=lambda problem_id: next(
            f"#{problem['id']} - {problem['title']}" for problem in filtered if problem["id"] == problem_id
        ),
    )
    problem = get_problem(int(selected_id))
    if not problem:
        render_notice("Card unavailable", "That card could not be loaded.", "warning")
        return

    existing_topics = [topic.strip() for topic in (problem.get("topics") or "").split(",") if topic.strip()]
    common_selected = [topic for topic in existing_topics if topic in COMMON_TOPICS]
    custom_existing = ", ".join(topic for topic in existing_topics if topic not in COMMON_TOPICS)

    with st.form(f"edit_form_{problem['id']}"):
        st.markdown(f"### Editing #{problem['id']}: {problem['title']}")
        title = st.text_input("Problem title", value=problem["title"])
        url = st.text_input("Problem URL", value=problem.get("url") or "")

        meta_cols = st.columns(4)
        source_index = SOURCES.index(problem["source"]) if problem["source"] in SOURCES else len(SOURCES) - 1
        diff_index = DIFFICULTIES.index(problem["difficulty"]) if problem["difficulty"] in DIFFICULTIES else len(DIFFICULTIES) - 1
        source = meta_cols[0].selectbox("Source", SOURCES, index=source_index)
        difficulty_value = meta_cols[1].selectbox("Difficulty", DIFFICULTIES, index=diff_index)
        status = meta_cols[2].selectbox(
            "Status",
            ["Solved", "Needs revisit", "Learning", "Reference"],
            index=["Solved", "Needs revisit", "Learning", "Reference"].index(problem["status"])
            if problem["status"] in ["Solved", "Needs revisit", "Learning", "Reference"]
            else 0,
        )
        solved_date = parse_date(problem.get("solved_at")) or date.today()
        solved_at = meta_cols[3].date_input("Solved date", value=solved_date)

        selected_topics = st.multiselect("Topics", COMMON_TOPICS, default=common_selected)
        custom_topics = st.text_input("Extra topics", value=custom_existing)
        question = st.text_area("Question prompt", value=problem.get("question") or "", height=220)
        intuition = st.text_area("Pattern / intuition", value=problem.get("intuition") or "", height=150)
        solution = st.text_area("Python solution", value=problem.get("solution") or "", height=300)

        saved = st.form_submit_button("Save changes", type="primary")

    if saved:
        if not title.strip():
            st.error("The title cannot be empty.")
        else:
            update_problem(
                int(problem["id"]),
                {
                    "title": title,
                    "url": url,
                    "source": source,
                    "difficulty": difficulty_value,
                    "status": status,
                    "topics": topics_to_text(selected_topics, custom_topics),
                    "question": question,
                    "intuition": intuition,
                    "solution": solution,
                    "solved_at": solved_at.isoformat(),
                },
            )
            st.success("Card updated.")
            st.rerun()

    archive_cols = st.columns([1, 5])
    if not problem["archived"]:
        if archive_cols[0].button("Archive card"):
            archive_problem(int(problem["id"]))
            st.rerun()
    else:
        if archive_cols[0].button("Restore card"):
            restore_problem(int(problem["id"]))
            st.rerun()

    history = get_review_history(int(problem["id"]))
    with st.expander("Review history"):
        if not history:
            st.write("No reviews recorded yet.")
        else:
            st.dataframe(
                [
                    {
                        "Reviewed": item["reviewed_at"][:10],
                        "Rating": item["rating"],
                        "Interval": f"{item['previous_interval']:.1f}d -> {item['new_interval']:.1f}d",
                        "Ease": f"{item['previous_ease']:.2f} -> {item['new_ease']:.2f}",
                        "Next due": item["next_review_at"],
                    }
                    for item in history
                ],
                width="stretch",
                hide_index=True,
            )


def import_export_screen() -> None:
    render_page_intro(
        "Backup",
        "Download a JSON snapshot of every card and review event stored on this machine.",
        "Data",
    )

    problems = list_problems(include_archived=True)
    reviews = fetch_all(
        "SELECT * FROM reviews WHERE user_email = ? ORDER BY reviewed_at DESC",
        (active_user_email(),),
    )
    payload = {
        "exported_at": utc_now(),
        "user_email": active_user_email(),
        "problems": problems,
        "reviews": reviews,
    }

    st.download_button(
        "Download JSON backup",
        data=json.dumps(payload, indent=2),
        file_name=f"dsa_flashcards_{today_iso()}.json",
        mime="application/json",
        disabled=not problems,
    )

    render_notice(
        "Local database",
        "Your working database lives in the data folder. Import is manual for now to avoid accidental duplicates.",
    )


def count_value(query: str, params: tuple[Any, ...] = ()) -> int:
    row = fetch_one(query, params) or {"count": 0}
    return int(row.get("count") or 0)


def render_admin_table(title: str, rows: list[dict[str, Any]], empty_message: str) -> None:
    st.markdown(f"### {title}")
    if not rows:
        render_notice(title, empty_message, "success")
        return
    st.dataframe(rows, width="stretch", hide_index=True)


def admin_screen() -> None:
    render_page_intro(
        "Admin Console",
        "Operational view for users, imports, audit events, and captured app errors.",
        "Production",
    )

    if not is_admin():
        render_notice("Admin only", "Your account is not listed as an admin for this deployment.", "warning")
        return

    since_24h = utc_since(days=1)
    since_7d = utc_since(days=7)
    user_count = count_value(
        """
        SELECT COUNT(*) AS count
        FROM (
            SELECT user_email FROM problems WHERE user_email <> ''
            UNION
            SELECT user_email FROM reviews WHERE user_email <> ''
            UNION
            SELECT user_email FROM github_sources WHERE user_email <> ''
        ) AS users
        """
    )
    active_cards = count_value("SELECT COUNT(*) AS count FROM problems WHERE archived = 0")
    reviews_7d = count_value("SELECT COUNT(*) AS count FROM reviews WHERE reviewed_at >= ?", (since_7d,))
    imports_24h = count_value("SELECT COUNT(*) AS count FROM import_runs WHERE started_at >= ?", (since_24h,))
    errors_24h = count_value("SELECT COUNT(*) AS count FROM app_errors WHERE created_at >= ?", (since_24h,))

    metric_cols = st.columns(5)
    with metric_cols[0]:
        render_stat_card("Users", user_count, "Known accounts", "teal")
    with metric_cols[1]:
        render_stat_card("Active cards", active_cards, "Across all users", "indigo")
    with metric_cols[2]:
        render_stat_card("Reviews, 7 days", reviews_7d, "Recent reps", "gold")
    with metric_cols[3]:
        render_stat_card("Imports, 24h", imports_24h, "GitHub runs", "teal")
    with metric_cols[4]:
        render_stat_card("Errors, 24h", errors_24h, "Captured exceptions", "coral")

    tabs = st.tabs(["Monitoring", "Import Runs", "Audit Log", "Errors"])

    with tabs[0]:
        per_hour, per_day = github_import_limits()
        st.markdown(
            f"""
            <section class="content-card">
                <h3>Runtime Guardrails</h3>
                <p>GitHub imports are limited to {per_hour} per user per hour and {per_day} per user per 24 hours.</p>
            </section>
            """,
            unsafe_allow_html=True,
        )
        busiest_users = fetch_all(
            """
            SELECT user_email, COUNT(*) AS imports_24h
            FROM import_runs
            WHERE started_at >= ?
            GROUP BY user_email
            ORDER BY imports_24h DESC, user_email ASC
            LIMIT 20
            """,
            (since_24h,),
        )
        render_admin_table("Import usage, last 24h", busiest_users, "No imports in the last 24 hours.")

        recent_failures = fetch_all(
            """
            SELECT started_at, user_email, repo_url, status, error_message
            FROM import_runs
            WHERE status <> 'success'
            ORDER BY started_at DESC
            LIMIT 20
            """
        )
        render_admin_table("Recent import warnings", recent_failures, "No failed or limited imports recorded.")

    with tabs[1]:
        runs = fetch_all(
            """
            SELECT
                started_at,
                finished_at,
                user_email,
                repo_url,
                branch,
                language,
                status,
                total_files,
                created_count,
                updated_count,
                skipped_count,
                error_message
            FROM import_runs
            ORDER BY started_at DESC
            LIMIT 100
            """
        )
        render_admin_table("GitHub import runs", runs, "No GitHub imports have run yet.")

    with tabs[2]:
        logs = fetch_all(
            """
            SELECT created_at, user_email, action, entity, details
            FROM audit_logs
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        render_admin_table("Audit events", logs, "No audit events have been recorded yet.")

    with tabs[3]:
        errors = fetch_all(
            """
            SELECT id, created_at, user_email, location, message, traceback
            FROM app_errors
            ORDER BY created_at DESC
            LIMIT 100
            """
        )
        if not errors:
            render_notice("No captured errors", "The app has not recorded any unexpected exceptions.", "success")
        for error in errors:
            with st.expander(f"#{error['id']} {error['created_at']} - {error['location']}"):
                st.write(f"User: {error.get('user_email') or 'unknown'}")
                st.code(error.get("message") or "", language="text")
                if error.get("traceback"):
                    st.code(error["traceback"], language="python")


def main() -> None:
    init_db()
    render_header()

    if not sync_user_session():
        render_sign_in()
        st.stop()

    st.sidebar.markdown(
        """
        <div class="sidebar-brand">
            <strong>DSA Flashcards</strong>
            <span>Retention system for solved problems</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_sidebar_account()
    st.sidebar.markdown("---")
    nav_options = ["Dashboard", "Add Problem", "GitHub Import", "Review", "Browse / Edit", "Backup"]
    if is_admin():
        nav_options.append("Admin")
    page = st.sidebar.radio(
        "Navigate",
        nav_options,
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown(
        f"""
        <div class="sidebar-db">
            <span>Local database</span>
            <strong>Ready</strong>
            <code>{html_text(DB_PATH.name)}</code>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if page == "Dashboard":
        dashboard_screen()
    elif page == "Add Problem":
        add_problem_screen()
    elif page == "GitHub Import":
        github_import_screen()
    elif page == "Review":
        review_screen()
    elif page == "Browse / Edit":
        browse_screen()
    elif page == "Backup":
        import_export_screen()
    elif page == "Admin":
        admin_screen()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        record_app_error("main", exc)
        st.error("Something went wrong. The error has been recorded for the admin dashboard.")
        with st.expander("Technical detail"):
            st.code(redact_sensitive(f"{type(exc).__name__}: {exc}"), language="text")
