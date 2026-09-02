import sqlite3
from pathlib import Path
from app.config import DATABASE_PATH

Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_retry_count INTEGER NOT NULL DEFAULT 2,
    max_auto_amount REAL NOT NULL DEFAULT 10000,
    high_value_threshold REAL NOT NULL DEFAULT 50000,
    max_reminders INTEGER NOT NULL DEFAULT 2,
    auto_recovery_enabled INTEGER NOT NULL DEFAULT 1,
    default_delay_hours REAL NOT NULL DEFAULT 6,
    max_recovery_cost REAL NOT NULL DEFAULT 20
);
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
    total_transactions INTEGER NOT NULL, successful_transactions INTEGER NOT NULL,
    lifetime_value REAL NOT NULL DEFAULT 0, segment TEXT NOT NULL DEFAULT 'growth'
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, amount REAL NOT NULL, method TEXT NOT NULL,
    status TEXT NOT NULL, failure_reason TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
    reminder_count INTEGER NOT NULL DEFAULT 0, fraud_flag INTEGER NOT NULL DEFAULT 0,
    subscription_status TEXT, created_at TEXT NOT NULL, recovered_at TEXT,
    recoverable_label INTEGER NOT NULL DEFAULT 0, best_action TEXT NOT NULL DEFAULT 'ESCALATE',
    hidden_recovery_prob REAL NOT NULL DEFAULT 0.2, baseline_eligible INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS recovery_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id TEXT UNIQUE NOT NULL,
    recovery_score REAL NOT NULL, recommended_action TEXT NOT NULL, actual_action TEXT,
    status TEXT NOT NULL, expected_revenue REAL NOT NULL, revenue_recovered REAL NOT NULL DEFAULT 0,
    explanation TEXT, updated_at TEXT NOT NULL, next_action_at TEXT, channel TEXT,
    FOREIGN KEY(payment_id) REFERENCES payments(id)
);
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id TEXT NOT NULL, action TEXT NOT NULL,
    reason TEXT NOT NULL, confidence REAL NOT NULL, policy_result TEXT NOT NULL,
    api_result TEXT, success INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    FOREIGN KEY(payment_id) REFERENCES payments(id)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id TEXT, event TEXT NOT NULL,
    details TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sample_size INTEGER NOT NULL,
    baseline_revenue REAL NOT NULL, ai_revenue REAL NOT NULL, incremental_revenue REAL NOT NULL,
    baseline_rate REAL NOT NULL, ai_rate REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, action TEXT NOT NULL,
    audience TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    estimated_revenue REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
'''


def get_conn():
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db():
    conn = get_conn(); conn.executescript(SCHEMA)
    # Lightweight migrations for databases from previous RecoverAI versions.
    existing = {r[1] for r in conn.execute('PRAGMA table_info(settings)').fetchall()}
    for name, ddl in [
        ('default_delay_hours', 'ALTER TABLE settings ADD COLUMN default_delay_hours REAL NOT NULL DEFAULT 6'),
        ('max_recovery_cost', 'ALTER TABLE settings ADD COLUMN max_recovery_cost REAL NOT NULL DEFAULT 20'),
    ]:
        if name not in existing: conn.execute(ddl)
    existing_p = {r[1] for r in conn.execute('PRAGMA table_info(payments)').fetchall()}
    for name, ddl in [
        ('recoverable_label', 'ALTER TABLE payments ADD COLUMN recoverable_label INTEGER NOT NULL DEFAULT 0'),
        ('best_action', "ALTER TABLE payments ADD COLUMN best_action TEXT NOT NULL DEFAULT 'ESCALATE'"),
        ('hidden_recovery_prob', 'ALTER TABLE payments ADD COLUMN hidden_recovery_prob REAL NOT NULL DEFAULT 0.2'),
        ('baseline_eligible', 'ALTER TABLE payments ADD COLUMN baseline_eligible INTEGER NOT NULL DEFAULT 0'),
    ]:
        if name not in existing_p: conn.execute(ddl)
    existing_c = {r[1] for r in conn.execute('PRAGMA table_info(customers)').fetchall()}
    for name, ddl in [
        ('lifetime_value', 'ALTER TABLE customers ADD COLUMN lifetime_value REAL NOT NULL DEFAULT 0'),
        ('segment', "ALTER TABLE customers ADD COLUMN segment TEXT NOT NULL DEFAULT 'growth'"),
    ]:
        if name not in existing_c: conn.execute(ddl)
    existing_rc = {r[1] for r in conn.execute('PRAGMA table_info(recovery_cases)').fetchall()}
    for name, ddl in [
        ('next_action_at', 'ALTER TABLE recovery_cases ADD COLUMN next_action_at TEXT'),
        ('channel', 'ALTER TABLE recovery_cases ADD COLUMN channel TEXT'),
    ]:
        if name not in existing_rc: conn.execute(ddl)
    if not conn.execute('SELECT id FROM settings WHERE id=1').fetchone():
        conn.execute('INSERT INTO settings (id) VALUES (1)')
    conn.commit(); conn.close()


def q(sql, params=(), one=False):
    conn=get_conn(); cur=conn.execute(sql, params); rows=cur.fetchall(); conn.close()
    return rows[0] if one and rows else (None if one else rows)


def exec_sql(sql, params=()):
    conn=get_conn(); cur=conn.execute(sql, params); conn.commit(); rid=cur.lastrowid; conn.close(); return rid
