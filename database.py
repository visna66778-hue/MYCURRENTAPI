"""Хранение путешествий и операций в локальной SQLite базе."""
import sqlite3
import logging
from functools import wraps
from pathlib import Path

DB_PATH = Path(__file__).with_name("wallet.sqlite3")
logger = logging.getLogger(__name__)
EXPENSE_CATEGORIES = (
    "Еда и кафе",
    "Жильё",
    "Транспорт",
    "Покупки",
    "Развлечения и экскурсии",
    "Здоровье",
    "Связь и интернет",
    "Прочее",
)

PERIOD_SQL = {
    "day": "created_at >= datetime('now', 'start of day')",
    "week": "created_at >= datetime('now', '-7 days')",
    "month": "created_at >= datetime('now', '-1 month')",
    "all": "1 = 1",
}


def logged_db(function):
    """Пишет входные параметры и результат операций SQLite."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        logger.info("SQLite %s inputs args=%r kwargs=%r", function.__name__, args, kwargs)
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            logger.exception("SQLite operation failed function=%s error_type=%s", function.__name__, type(exc).__name__)
            raise
        if isinstance(result, sqlite3.Row):
            logged_result = dict(result)
        elif isinstance(result, list) and result and isinstance(result[0], sqlite3.Row):
            logged_result = [dict(row) for row in result]
        else:
            logged_result = result
        logger.info("SQLite %s returned=%r", function.__name__, logged_result)
        return result
    return wrapped


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@logged_db
def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            origin TEXT NOT NULL, destination TEXT NOT NULL,
            home_currency TEXT NOT NULL, local_currency TEXT NOT NULL,
            rate REAL NOT NULL, initial_home REAL NOT NULL,
            balance_home REAL NOT NULL, balance_local REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
            amount REAL NOT NULL, currency TEXT NOT NULL,
            home_amount REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS active_trips (
            user_id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_trips_user ON trips(user_id);
        CREATE INDEX IF NOT EXISTS idx_expenses_user_trip ON expenses(user_id, trip_id, id);
        """)
        # Аддитивная миграция: старые строки получают категорию «Прочее».
        columns = {row["name"] for row in db.execute("PRAGMA table_info(expenses)")}
        if "category" not in columns:
            db.execute("ALTER TABLE expenses ADD COLUMN category TEXT NOT NULL DEFAULT 'Прочее'")
            logger.info("SQLite migration added expenses.category; existing rows default to Прочее")
            columns.add("category")
        db.execute("CREATE INDEX IF NOT EXISTS idx_expenses_trip_category ON expenses(user_id, trip_id, category, id)")


@logged_db
def trips(user_id):
    with connect() as db:
        return db.execute("SELECT * FROM trips WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()


@logged_db
def active_trip(user_id):
    with connect() as db:
        return db.execute("SELECT t.* FROM trips t JOIN active_trips a ON a.trip_id=t.id WHERE a.user_id=? AND t.user_id=?", (user_id, user_id)).fetchone()


@logged_db
def set_active(user_id, trip_id):
    with connect() as db:
        if not db.execute("SELECT 1 FROM trips WHERE id=? AND user_id=?", (trip_id, user_id)).fetchone():
            return False
        db.execute("INSERT INTO active_trips(user_id,trip_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET trip_id=excluded.trip_id", (user_id, trip_id))
        return True


@logged_db
def create_trip(user_id, origin, destination, home, local, rate, initial_home, initial_local):
    with connect() as db:
        cur = db.execute("INSERT INTO trips(user_id,origin,destination,home_currency,local_currency,rate,initial_home,balance_home,balance_local) VALUES(?,?,?,?,?,?,?,?,?)",
                         (user_id, origin, destination, home, local, rate, initial_home, initial_home, initial_local))
        trip_id = cur.lastrowid
        db.execute("INSERT INTO active_trips(user_id,trip_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET trip_id=excluded.trip_id", (user_id, trip_id))
        return trip_id


@logged_db
def add_expense(user_id, trip_id, amount, home_amount, category):
    if category not in EXPENSE_CATEGORIES:
        raise ValueError("Неизвестная категория расхода")
    with connect() as db:
        trip = db.execute("SELECT * FROM trips WHERE id=? AND user_id=?", (trip_id, user_id)).fetchone()
        if not trip:
            return False
        db.execute(
            "INSERT INTO expenses(user_id,trip_id,amount,currency,home_amount,category) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, trip_id, amount, trip["local_currency"], home_amount, category),
        )
        db.execute("UPDATE trips SET balance_local=balance_local-?, balance_home=balance_home-? WHERE id=? AND user_id=?", (amount, home_amount, trip_id, user_id))
        return True


@logged_db
def set_rate(user_id, trip_id, rate):
    with connect() as db:
        trip = db.execute("SELECT * FROM trips WHERE id=? AND user_id=?", (trip_id, user_id)).fetchone()
        if not trip:
            return False
        local_balance = trip["balance_home"] * rate
        db.execute("UPDATE trips SET rate=?, balance_local=? WHERE id=? AND user_id=?", (rate, local_balance, trip_id, user_id))
        return True


def _period_clause(period):
    try:
        return PERIOD_SQL[period]
    except KeyError as exc:
        raise ValueError("Неизвестный период истории") from exc


@logged_db
def history(user_id, trip_id, limit=None, category=None, period="all"):
    clauses = ["user_id=?", "trip_id=?", _period_clause(period)]
    params = [user_id, trip_id]
    if category is not None:
        if category not in EXPENSE_CATEGORIES:
            raise ValueError("Неизвестная категория расхода")
        clauses.append("category=?")
        params.append(category)
    sql = "SELECT * FROM expenses WHERE " + " AND ".join(clauses) + " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with connect() as db:
        return db.execute(sql, params).fetchall()


@logged_db
def expense_summary(user_id, trip_id, period="all"):
    clause = _period_clause(period)
    with connect() as db:
        return db.execute(
            f"SELECT category, COUNT(*) AS count, SUM(amount) AS local_total, SUM(home_amount) AS home_total "
            f"FROM expenses WHERE user_id=? AND trip_id=? AND {clause} GROUP BY category ORDER BY category",
            (user_id, trip_id),
        ).fetchall()


@logged_db
def expense_total(user_id, trip_id, period="all"):
    clause = _period_clause(period)
    with connect() as db:
        return db.execute(
            f"SELECT COUNT(*) AS count, COALESCE(SUM(amount),0) AS local_total, "
            f"COALESCE(SUM(home_amount),0) AS home_total FROM expenses "
            f"WHERE user_id=? AND trip_id=? AND {clause}",
            (user_id, trip_id),
        ).fetchone()
