import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta

class DB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur

    def executemany(self, sql, seq):
        with self.lock:
            self.conn.executemany(sql, seq)

    def get_user(self, uid):
        cur = self.execute("SELECT * FROM users WHERE id = ?", (uid,))
        return dict(cur.fetchone() or {})

    def create_user(self, uid):
        now = int(time.time())
        self.execute("INSERT OR IGNORE INTO users (id, token, name, created_at) VALUES (?, ?, ?, ?)",
                     (uid, uid, "Guest", now))

    def update_user_name(self, uid, name):
        self.execute("UPDATE users SET name = ? WHERE id = ?", (name, uid))

    def get_tickets_by_user(self, uid):
        cur = self.execute("SELECT * FROM tickets WHERE user_id = ? ORDER BY created_at DESC", (uid,))
        return [dict(row) for row in cur.fetchall()]

    def get_articles(self):
        cur = self.execute("SELECT * FROM articles ORDER BY helpful_count DESC, created_at DESC")
        return [dict(row) for row in cur.fetchall()]

    def get_article(self, aid):
        cur = self.execute("SELECT * FROM articles WHERE id = ?", (aid,))
        return dict(cur.fetchone() or {})

    def get_article_by_document_id(self, document_id):
        cur = self.execute("SELECT * FROM articles WHERE document_id = ?", (document_id,))
        return dict(cur.fetchone() or {})

    def upsert_knowledge_article(self, document_id, title, body, tags, source):
        existing = self.get_article_by_document_id(document_id)
        now = int(time.time())
        if existing:
            self.execute(
                "UPDATE articles SET title = ?, body = ?, tags = ?, source = ?, created_at = ? WHERE document_id = ?",
                (title, body, tags, source, now, document_id)
            )
            return existing["id"]
        cur = self.execute(
            "INSERT INTO articles (document_id, title, body, tags, source, helpful_count, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (document_id, title, body, tags, source, now)
        )
        return cur.lastrowid

    def get_ticket(self, tid):
        cur = self.execute("SELECT * FROM tickets WHERE id = ?", (tid,))
        return dict(cur.fetchone() or {})

    def get_messages(self, tid):
        cur = self.execute("SELECT * FROM messages WHERE ticket_id = ? ORDER BY created_at ASC", (tid,))
        return [dict(row) for row in cur.fetchall()]

    def get_messages_after(self, tid, after):
        cur = self.execute("SELECT * FROM messages WHERE ticket_id = ? AND id > ? ORDER BY id ASC", (tid, after))
        return [dict(row) for row in cur.fetchall()]

    def create_ticket(self, user_id, summary, status):
        now = int(time.time())
        cur = self.execute("INSERT INTO tickets (summary, status, user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (summary, status, user_id, now, now))
        return cur.lastrowid

    def add_message(self, ticket_id, sender, author_name, body):
        now = int(time.time())
        cur = self.execute("INSERT INTO messages (ticket_id, sender, author_name, body, created_at) VALUES (?, ?, ?, ?, ?)",
                           (ticket_id, sender, author_name, body, now))
        return cur.lastrowid

    def increment_article_helpful(self, aid):
        self.execute("UPDATE articles SET helpful_count = helpful_count + 1 WHERE id = ?", (aid,))

    def resolve_ticket(self, tid):
        now = int(time.time())
        self.execute("UPDATE tickets SET status = 'resolved', updated_at = ? WHERE id = ?", (now, tid))

    def escalate_ticket(self, tid):
        now = int(time.time())
        self.execute("UPDATE tickets SET status = 'escalated', updated_at = ? WHERE id = ?", (now, tid))

    def assign_ticket(self, tid, associate_id):
        self.execute("UPDATE tickets SET assigned_to = ? WHERE id = ?", (associate_id, tid))

    def set_ticket_status(self, tid, status):
        now = int(time.time())
        self.execute("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (status, now, tid))

    def get_escalated_tickets(self):
        cur = self.execute("SELECT * FROM tickets WHERE status IN ('escalated', 'associate_active') ORDER BY updated_at ASC")
        return [dict(row) for row in cur.fetchall()]

    def get_associates(self):
        cur = self.execute("SELECT * FROM associates")
        return [dict(row) for row in cur.fetchall()]

    def get_associate(self, aid):
        cur = self.execute("SELECT * FROM associates WHERE id = ?", (aid,))
        return dict(cur.fetchone() or {})

    def get_associate_by_username(self, username):
        cur = self.execute("SELECT * FROM associates WHERE username = ?", (username,))
        return dict(cur.fetchone() or {})

    def update_associate_credentials(self, aid, username, password_hash):
        self.execute("UPDATE associates SET username = ?, password_hash = ? WHERE id = ?",
                     (username, password_hash, aid))

    def pick_associate(self, specialty=None):
        # Load balancing: pick associate with fewest active tickets, matching specialty if possible
        associates = self.get_associates()
        if specialty:
            associates = [a for a in associates if a["specialty"] == specialty]
        if not associates:
            associates = self.get_associates()
        if not associates:
            return None
        counts = {a["id"]: 0 for a in associates}
        cur = self.execute("SELECT assigned_to, COUNT(*) as c FROM tickets WHERE status IN ('escalated', 'associate_active') GROUP BY assigned_to")
        for row in cur.fetchall():
            if row["assigned_to"] in counts:
                counts[row["assigned_to"]] = row["c"]
        min_count = min(counts.values())
        for a in associates:
            if counts[a["id"]] == min_count:
                return a
        return associates[0]

    def create_article(self, title, body, tags, source):
        now = int(time.time())
        cur = self.execute("INSERT INTO articles (title, body, tags, source, helpful_count, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                           (title, body, tags, source, now))
        return cur.lastrowid

    def create_resolution(self, ticket_id, title, steps, published, article_id):
        now = int(time.time())
        self.execute("INSERT INTO resolutions (ticket_id, title, steps, published, article_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (ticket_id, title, steps, int(bool(published)), article_id, now))

    def get_metrics(self):
        total = self.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        by_status = {row["status"]: row["c"] for row in self.execute("SELECT status, COUNT(*) as c FROM tickets GROUP BY status")}
        cur = self.execute("SELECT strftime('%Y-%m-%d', datetime(created_at, 'unixepoch')) as day, COUNT(*) as c FROM tickets GROUP BY day ORDER BY day DESC LIMIT 14")
        daily = [{"day": row["day"], "count": row["c"]} for row in cur.fetchall()]
        cur = self.execute("""
            SELECT MIN(m1.created_at) as first_reply, t.created_at as ticket_created
            FROM messages m1
            JOIN tickets t ON m1.ticket_id = t.id
            WHERE m1.sender = 'associate'
            GROUP BY t.id
        """)
        reply_times = [row["first_reply"] - row["ticket_created"] for row in cur.fetchall() if row["first_reply"] and row["ticket_created"]]
        avg_first_reply_seconds = int(sum(reply_times) / len(reply_times)) if reply_times else 0
        top_articles = [dict(row) for row in self.execute("SELECT * FROM articles ORDER BY helpful_count DESC LIMIT 5")]
        # Top failing queries: tickets that escalated without a helpful article
        cur = self.execute("""
            SELECT summary, COUNT(*) as c FROM tickets
            WHERE status IN ('escalated', 'associate_active')
            GROUP BY summary ORDER BY c DESC LIMIT 5
        """)
        top_failing_queries = [{"summary": row["summary"], "count": row["c"]} for row in cur.fetchall()]
        return {
            "total_tickets": total,
            "by_status": by_status,
            "daily": daily,
            "avg_first_reply_seconds": avg_first_reply_seconds,
            "top_articles": top_articles,
            "top_failing_queries": top_failing_queries
        }

def ensure_schema(db):
    # Create tables if not exist
    db.execute("""
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT UNIQUE,
        title TEXT, body TEXT, tags TEXT, source TEXT,
        helpful_count INTEGER DEFAULT 0, created_at INTEGER
    )""")
    db.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        summary TEXT, status TEXT, user_id TEXT, assigned_to TEXT,
        created_at INTEGER, updated_at INTEGER
    )""")
    db.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER, sender TEXT, body TEXT, author_name TEXT, created_at INTEGER
    )""")
    db.execute("""
    CREATE TABLE IF NOT EXISTS associates (
        id TEXT PRIMARY KEY, name TEXT, specialty TEXT, avatar TEXT,
        username TEXT UNIQUE, password_hash TEXT
    )""")
    db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, token TEXT, name TEXT, created_at INTEGER
    )""")
    db.execute("""
    CREATE TABLE IF NOT EXISTS resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER, title TEXT, steps TEXT, published INTEGER, article_id INTEGER, created_at INTEGER
    )""")
    # Lightweight migrations
    # Add assigned_to, user_id, author_name if missing
    for table, col, typ in [
        ("tickets", "assigned_to", "TEXT"),
        ("tickets", "user_id", "TEXT"),
        ("messages", "author_name", "TEXT"),
        ("associates", "username", "TEXT"),
        ("associates", "password_hash", "TEXT"),
        ("articles", "document_id", "TEXT"),
    ]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except Exception:
            pass
    # Seed associates if empty
    if not db.execute("SELECT COUNT(*) FROM associates").fetchone()[0]:
        associates = [
    ("alex", "Alex Morgan", "Accounts & Login", "👤"),
    ("priya", "Priya Shah", "Billing & Payments", "💳"),
    ("jordan", "Jordan Lee", "Security & 2FA", "🔒"),
    ("sam", "Sam Rivera", "General Support", "🛠️"),
]
        db.executemany("INSERT INTO associates (id, name, specialty, avatar) VALUES (?, ?, ?, ?)", associates)

    data_dir = os.path.join(os.path.dirname(__file__), "data")
    for filename in ("faqs.json", "process_questions.json", "tickets.json", "manuals.json"):
        path = os.path.join(data_dir, filename)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as data_file:
            records = json.load(data_file)
        for record in records:
            document_id = str(record["id"]).strip()
            title = str(record["title"]).strip()
            content = str(record.get("content", record.get("body", ""))).strip()
            tags = record.get("tags", [])
            if isinstance(tags, list):
                tags = ",".join(str(tag).strip() for tag in tags if str(tag).strip())
            db.upsert_knowledge_article(
                document_id,
                title,
                content,
                str(tags),
                str(record.get("source", filename.removesuffix(".json")))
            )
