import os
import sys
import json
import time
import uuid
import hmac
import base64
import hashlib
import threading
import urllib.parse
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from db import DB, ensure_schema
import nlp

HOST, PORT = "127.0.0.1", 5000
DB_PATH = os.path.join(os.path.dirname(__file__), "support_chat.db")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
ASSOCIATE_PASSWORD = os.environ.get("ASSOCIATE_PASSWORD")
ASSOCIATE_RESET_CODE = os.environ.get("ASSOCIATE_RESET_CODE")
SESSION_COOKIE = "sc_uid"
ASSOC_COOKIE = "sc_assoc"
associate_sessions = {}
associate_sessions_lock = threading.Lock()

db = DB(DB_PATH)
ensure_schema(db)
nlp.configure_database(db)

def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240000)
    return f"240000${base64.b64encode(salt).decode('ascii')}${digest.hex()}"

def verify_password(password, encoded):
    try:
        iterations, salt_text, digest_text = encoded.split("$", 2)
        salt = base64.b64decode(salt_text.encode("ascii"))
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(digest.hex(), digest_text)
    except (ValueError, TypeError):
        return False

if ASSOCIATE_PASSWORD:
    for configured_associate in db.get_associates():
        if not configured_associate.get("username") or not configured_associate.get("password_hash"):
            db.update_associate_credentials(
                configured_associate["id"],
                configured_associate.get("username") or configured_associate["id"],
                configured_associate.get("password_hash") or hash_password(ASSOCIATE_PASSWORD)
            )

def parse_cookies(header):
    if not header:
        return {}
    c = cookies.SimpleCookie()
    c.load(header)
    return {k: v.value for k, v in c.items()}

def set_cookie(handler, key, value, path="/", http_only=True, max_age=None):
    c = cookies.SimpleCookie()
    c[key] = value
    c[key]["path"] = path
    if http_only:
        c[key]["httponly"] = True
    c[key]["samesite"] = "Lax"
    if max_age:
        c[key]["max-age"] = str(max_age)
    handler.send_header("Set-Cookie", c.output(header="").strip())

def clear_cookie(handler, key, path="/"):
    c = cookies.SimpleCookie()
    c[key] = ""
    c[key]["path"] = path
    c[key]["max-age"] = "0"
    handler.send_header("Set-Cookie", c.output(header="").strip())

def get_user(handler):
    cookies_ = parse_cookies(handler.headers.get("Cookie"))
    uid = cookies_.get(SESSION_COOKIE)
    if not uid:
        return None
    user = db.get_user(uid)
    return user

def get_associate_session(handler):
    cookies_ = parse_cookies(handler.headers.get("Cookie"))
    token = cookies_.get(ASSOC_COOKIE)
    if not token:
        return None
    with associate_sessions_lock:
        session = associate_sessions.get(token)
        if not session or session["expires_at"] <= time.time():
            associate_sessions.pop(token, None)
            return None
        associate_id = session.get("associate_id")
    return db.get_associate(associate_id) if associate_id else None

def require_auth(handler):
    cookies_ = parse_cookies(handler.headers.get("Cookie"))
    token = cookies_.get(ASSOC_COOKIE)
    if not token:
        return None
    with associate_sessions_lock:
        session = associate_sessions.get(token)
        if not session or session["expires_at"] <= time.time():
            associate_sessions.pop(token, None)
            return None
        return session

def get_ticket_for_request(handler, tid):
    ticket = db.get_ticket(tid)
    if not ticket:
        return None
    user = get_user(handler)
    if user and ticket["user_id"] == user["id"]:
        return ticket
    if require_auth(handler):
        return ticket
    return None

def get_associate_ticket(handler, tid):
    if not get_associate_session(handler):
        return None
    return db.get_ticket(tid)

def send_json(handler, payload, status=200):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(payload, default=str).encode("utf-8"))

def render_template(name, **ctx):
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, encoding="utf-8") as f:
        html = f.read()
    for k, v in ctx.items():
        html = html.replace("{{ " + k + " }}", str(v))
    return html

def sse_headers(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

def sse_event(handler, event, data):
    msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    handler.wfile.write(msg.encode("utf-8"))
    handler.wfile.flush()

def static_path(path):
    # Prevent path traversal
    rel = os.path.normpath(path).replace("\\", "/")
    if ".." in rel or rel.startswith("/"):
        return None
    return os.path.join(STATIC_DIR, rel)

class Handler(BaseHTTPRequestHandler):
    server_version = "SupportChat/1.0"

    def log_message(self, fmt, *args):
        # METHOD /path -> STATUS
        sys.stdout.write(f"{self.command} {self.path} -> {args[1]}\n")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        # Static files
        if path.startswith("/static/"):
            spath = static_path(path[len("/static/" ):])
            if not spath or not os.path.isfile(spath):
                self.send_error(404)
                return
            ext = os.path.splitext(spath)[1]
            ctype = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".ico": "image/x-icon",
                ".svg": "image/svg+xml"
            }.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            with open(spath, "rb") as f:
                self.wfile.write(f.read())
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        # Templates
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(render_template("index.html").encode("utf-8"))
            return
        if path == "/associate":
            if not require_auth(self):
                self.send_response(302)
                self.send_header("Location", "/associate/login")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(render_template("associate.html").encode("utf-8"))
            return
        if path == "/associate/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(render_template("associate_login.html").encode("utf-8"))
            return
        if path == "/associate/logout":
            token = parse_cookies(self.headers.get("Cookie")).get(ASSOC_COOKIE)
            if token:
                with associate_sessions_lock:
                    associate_sessions.pop(token, None)
            self.send_response(302)
            clear_cookie(self, ASSOC_COOKIE)
            self.send_header("Location", "/associate/login")
            self.end_headers()
            return
        if path == "/articles":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(render_template("articles.html").encode("utf-8"))
            return
        if path == "/my-tickets":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(render_template("my_tickets.html").encode("utf-8"))
            return
        if path == "/metrics":
            if not require_auth(self):
                self.send_response(302)
                self.send_header("Location", "/associate/login")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(render_template("metrics.html").encode("utf-8"))
            return
        # API endpoints
        if path == "/api/me":
            user = get_user(self)
            new_user = False

            if not user:
                # Create new user
                uid = str(uuid.uuid4())
                db.create_user(uid)
                user = db.get_user(uid)
                new_user = True

            self.send_response(200)
            self.send_header("Content-Type", "application/json")

            if new_user:
                set_cookie(
                    self,
                    SESSION_COOKIE,
                    user["id"],
                    http_only=True,
                    max_age=60 * 60 * 24 * 365
                )

            self.end_headers()

            response = {
                "id": user["id"],
                "name": user["name"]
            }

            self.wfile.write(
                json.dumps(response).encode("utf-8")
            )
            return
        if path == "/api/my/tickets":
            user = get_user(self)
            if not user:
                self.send_error(401)
                return
            tickets = db.get_tickets_by_user(user["id"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(tickets, default=str).encode("utf-8"))
            return
        if path == "/api/articles":
            articles = db.get_articles()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(articles, default=str).encode("utf-8"))
            return
        if path.startswith("/api/articles/"):
            try:
                aid = int(path.split("/")[-1])
            except Exception:
                self.send_error(400)
                return
            article = db.get_article(aid)
            if not article:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(article, default=str).encode("utf-8"))
            return
        if path.startswith("/api/tickets/") and path.endswith("/messages"):
            try:
                tid = int(path.split("/")[3])
            except Exception:
                self.send_error(400)
                return
            ticket = get_ticket_for_request(self, tid)
            if not ticket:
                self.send_error(401)
                return
            messages = db.get_messages(tid)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ticket": ticket, "messages": messages}, default=str).encode("utf-8"))
            return
        if path.startswith("/api/associate/tickets/") and path.endswith("/messages"):
            try:
                tid = int(path.split("/")[4])
            except Exception:
                self.send_error(400)
                return
            ticket = get_associate_ticket(self, tid)
            if not ticket:
                self.send_error(401)
                return
            messages = db.get_messages(tid)
            send_json(self, {"ticket": ticket, "messages": messages})
            return
        if path.startswith("/api/tickets/") and "/stream" in path:
            try:
                tid = int(path.split("/")[3])
            except Exception:
                self.send_error(400)
                return
            try:
                after = int(query.get("after", ["0"])[0] or "0")
            except (ValueError, TypeError):
                after = 0
            sse_headers(self)
            try:
                if not get_associate_session(self) and not get_user(self):
                    return
                last_status = None
                while True:
                    ticket = get_ticket_for_request(self, tid)
                    if not ticket:
                        break
                    msgs = db.get_messages_after(tid, after)
                    for m in msgs:
                        sse_event(self, "message", m)
                        after = max(after, m["id"])
                    if ticket["status"] != last_status:
                        sse_event(self, "status", {"status": ticket["status"]})
                        last_status = ticket["status"]
                    if ticket["status"] == "resolved":
                        sse_event(self, "done", {})
                        break
                    for _ in range(20):
                        time.sleep(0.75)
                        if self.wfile.closed:
                            return
                    sse_event(self, "ping", {})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            return
        if path == "/api/associate/tickets":
            if not require_auth(self):
                self.send_error(401)
                return
            tickets = db.get_escalated_tickets()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(tickets, default=str).encode("utf-8"))
            return
        if path == "/api/associate/tickets/stream":
            if not require_auth(self):
                self.send_error(401)
                return
            sse_headers(self)
            try:
                last_ids = None
                while True:
                    tickets = db.get_escalated_tickets()
                    queue_state = tuple(
                        (t["id"], t["status"], t["assigned_to"])
                        for t in tickets
                    )
                    if queue_state != last_ids:
                        sse_event(self, "tickets", tickets)
                        last_ids = queue_state
                    for _ in range(20):
                        time.sleep(1)
                        if self.wfile.closed:
                            return
                    sse_event(self, "ping", {})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            return
        if path == "/api/associates":
            if not require_auth(self):
                self.send_error(401)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(db.get_associates(), default=str).encode("utf-8"))
            return
        if path == "/api/associate/me":
            associate = get_associate_session(self)
            if not associate:
                self.send_error(401)
                return
            send_json(self, {"ok": True, "associate": associate})
            return
        if path == "/api/associate/articles":
            if not get_associate_session(self):
                self.send_error(401)
                return
            send_json(self, {"articles": db.get_articles()})
            return
        if path == "/api/metrics":
            if not require_auth(self):
                self.send_error(401)
                return
            metrics = db.get_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(metrics, default=str).encode("utf-8"))
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        # API endpoints
        if path == "/api/me":
            user = get_user(self)
            if not user:
                self.send_error(401)
                return
            name = data.get("name", "").strip()
            if not name:
                self.send_error(400)
                return
            db.update_user_name(user["id"], name)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            return
        if path == "/api/tickets":
            user = get_user(self)
            if not user:
                self.send_error(401)
                return
            summary = data.get("summary", "").strip()
            if not summary:
                self.send_error(400)
                return
            # Greeting detection
            if nlp.is_greeting(summary):
                tid = db.create_ticket(user["id"], summary, "open")
                initial_message_id = db.add_message(tid, "user", user["name"], summary)
                db.add_message(tid, "bot", "Assistant", "Hi! How can I help you today?")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ticket_id": tid, "phase": "describe", "initial_message_id": initial_message_id}).encode("utf-8"))
                return
            # Human handoff detection
            if nlp.wants_human(summary):
                tid = db.create_ticket(user["id"], summary, "escalated")
                initial_message_id = db.add_message(tid, "user", user["name"], summary)
                db.add_message(tid, "bot", "Assistant", "Connecting you to a support associate...")
                escalate_to = nlp.route_specialty(summary)
                associate = db.pick_associate(escalate_to)
                if associate:
                    db.assign_ticket(tid, associate["id"])
                    db.add_message(tid, "bot", "Assistant", f"🧑‍💻 {associate['name']} joined the conversation.")
                    db.add_message(tid, "associate", associate["name"], f"Hi, I'm {associate['name']} ({associate['specialty']}). How can I help?")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ticket_id": tid, "phase": "escalated", "initial_message_id": initial_message_id}).encode("utf-8"))
                return
            # Hybrid RAG search with confidence-based escalation.
            rag_result = nlp.answer_from_knowledge_base(summary)
            if rag_result["trace"].get("errors"):
                print(f"[rag] query={summary!r} errors={rag_result['trace']['errors']}")
            if rag_result["decision"] == "answer":
                article = rag_result["trace"]["sources"][0]
                tid = db.create_ticket(user["id"], summary, "open")
                initial_message_id = db.add_message(tid, "user", user["name"], summary)
                db.add_message(tid, "bot", "Assistant", rag_result["answer"])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ticket_id": tid, "phase": "feedback", "auto_answer": True, "article_id": article["id"], "sources": rag_result["trace"]["sources"], "initial_message_id": initial_message_id}).encode("utf-8"))
                return
            # Low-confidence questions are escalated with retrieval context.
            tid = db.create_ticket(user["id"], summary, "escalated")
            initial_message_id = db.add_message(tid, "user", user["name"], summary)
            db.add_message(tid, "bot", "Assistant", "I'm not confident I can answer that accurately, so I'm connecting you with a support associate.")
            associate = db.pick_associate(nlp.route_specialty(summary))
            if associate:
                db.assign_ticket(tid, associate["id"])
                db.set_ticket_status(tid, "associate_active")
                db.add_message(tid, "bot", "Assistant", f"🧑‍💻 {associate['name']} has the conversation context and will help next.")
                db.add_message(tid, "associate", associate["name"], f"Hi, I'm {associate['name']} ({associate['specialty']}). How can I help?")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ticket_id": tid, "phase": "escalated", "initial_message_id": initial_message_id, "sources": rag_result["trace"]["sources"], "escalation_reason": "Low-confidence knowledge-base match"}).encode("utf-8"))
            return
        if path.startswith("/api/tickets/") and path.endswith("/feedback"):
            try:
                tid = int(path.split("/")[3])
            except Exception:
                self.send_error(400)
                return
            ticket = get_ticket_for_request(self, tid)
            user = get_user(self)
            if not ticket or not user or ticket["user_id"] != user["id"]:
                self.send_error(401)
                return
            helped = data.get("helped")
            article_id = data.get("article_id")
            if helped:
                if article_id:
                    if not db.get_article(article_id):
                        self.send_error(400)
                        return
                    db.increment_article_helpful(article_id)
                db.resolve_ticket(tid)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"resolved": True}).encode("utf-8"))
                return
            else:
                db.escalate_ticket(tid)
                # Assign associate
                ticket = db.get_ticket(tid)
                escalate_to = nlp.route_specialty(ticket["summary"])
                associate = db.pick_associate(escalate_to)
                if associate:
                    db.assign_ticket(tid, associate["id"])
                    db.add_message(tid, "bot", "Assistant", f"🧑‍💻 {associate['name']} joined the conversation.")
                    db.add_message(tid, "associate", associate["name"], f"Hi, I'm {associate['name']} ({associate['specialty']}). How can I help?")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"escalated": True}).encode("utf-8"))
                return
        if path.startswith("/api/tickets/") and path.endswith("/messages"):
            try:
                tid = int(path.split("/")[3])
            except Exception:
                self.send_error(400)
                return
            ticket = db.get_ticket(tid)
            if not ticket:
                self.send_error(404)
                return
            sender = data.get("sender")
            body = data.get("body", "").strip()
            if not body or sender not in ("user", "associate"):
                self.send_error(400)
                return
            if sender == "user":
                user = get_user(self)
                if not user or ticket["user_id"] != user["id"]:
                    self.send_error(401)
                    return
                author_name = user["name"]
            else:
                associate = get_associate_session(self)
                if not associate or ticket["assigned_to"] != associate["id"]:
                    self.send_error(401)
                    return
                author_name = associate["name"]
            message_id = db.add_message(tid, sender, author_name, body)
            # Only open tickets are handled by the bot. Associate-active tickets
            # must deliver follow-up messages to the assigned associate.
            if sender == "user" and ticket["status"] == "open":
                if nlp.is_greeting(body):
                    db.add_message(tid, "bot", "Assistant", "Hi! How can I help you today?")
                elif nlp.wants_human(body):
                    db.escalate_ticket(tid)
                    escalate_to = nlp.route_specialty(body)
                    associate = db.pick_associate(escalate_to)
                    if associate:
                        db.assign_ticket(tid, associate["id"])
                        db.add_message(tid, "bot", "Assistant", f"🧑‍💻 {associate['name']} joined the conversation.")
                        db.add_message(tid, "associate", associate["name"], f"Hi, I'm {associate['name']} ({associate['specialty']}). How can I help?")
                else:
                    rag_result = nlp.answer_from_knowledge_base(body)
                    if rag_result["trace"].get("errors"):
                        print(f"[rag] query={body!r} errors={rag_result['trace']['errors']}")
                    if rag_result["decision"] == "answer":
                        db.add_message(tid, "bot", "Assistant", rag_result["answer"])
                    else:
                        db.escalate_ticket(tid)
                        associate = db.pick_associate(nlp.route_specialty(body))
                        if associate:
                            db.assign_ticket(tid, associate["id"])
                            db.add_message(tid, "bot", "Assistant", "I'm not confident I can answer that accurately, so I've escalated this to a support associate.")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "message_id": message_id}).encode("utf-8"))
            return
        if path.startswith("/api/tickets/") and path.endswith("/join"):
            associate = get_associate_session(self)
            if not associate:
                self.send_error(401)
                return
            try:
                tid = int(path.split("/")[3])
            except Exception:
                self.send_error(400)
                return
            ticket = db.get_ticket(tid)
            if not ticket:
                self.send_error(404)
                return
            was_assigned_to = ticket["assigned_to"]
            db.assign_ticket(tid, associate["id"])
            db.set_ticket_status(tid, "associate_active")
            if was_assigned_to != associate["id"]:
                db.add_message(tid, "associate", associate["name"], f"Hi, I'm {associate['name']} ({associate['specialty']}). How can I help?")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            return
        if path == "/api/associate/login":
            username = str(data.get("username", "")).strip().lower()
            password = data.get("password", "")
            associate = db.get_associate_by_username(username) if username else None

            if associate and verify_password(str(password), associate.get("password_hash")):
                token = uuid.uuid4().hex
                with associate_sessions_lock:
                    associate_sessions[token] = {
                        "associate_id": associate["id"],
                        "expires_at": time.time() + 60 * 60 * 8
                    }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")

                set_cookie(
                    self,
                    ASSOC_COOKIE,
                    token,
                    http_only=True,
                    max_age=60 * 60 * 8
                )

                self.end_headers()

                self.wfile.write(
                    json.dumps({"ok": True, "associate": associate}).encode("utf-8")
                )
                return
            else:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "ok": False,
                        "error": "Invalid password"
                    }).encode("utf-8")
                )
                return

        if path == "/api/associate/me":
            associate = get_associate_session(self)
            if not associate:
                self.send_error(401)
                return
            send_json(self, {"ok": True, "associate": associate})
            return

        if path == "/api/associate/reset":
            username = str(data.get("username", "")).strip().lower()
            reset_code = str(data.get("reset_code", ""))
            new_password = str(data.get("new_password", ""))
            associate = db.get_associate_by_username(username) if username else None
            if not ASSOCIATE_RESET_CODE or not hmac.compare_digest(reset_code, ASSOCIATE_RESET_CODE):
                self.send_error(401)
                return
            if not associate or len(new_password) < 8:
                self.send_error(400)
                return
            db.update_associate_credentials(
                associate["id"],
                associate["username"],
                hash_password(new_password)
            )
            send_json(self, {"ok": True})
            return
        if path.startswith("/api/associate/tickets/") and path.endswith("/claim"):
            associate = get_associate_session(self)
            if not associate:
                self.send_error(401)
                return
            try:
                tid = int(path.split("/")[4])
            except Exception:
                self.send_error(400)
                return
            ticket = db.get_ticket(tid)
            if not ticket:
                self.send_error(404)
                return
            db.assign_ticket(tid, associate["id"])
            db.set_ticket_status(tid, "associate_active")
            if ticket["assigned_to"] != associate["id"]:
                db.add_message(tid, "associate", associate["name"], f"Hi, I'm {associate['name']} ({associate['specialty']}). How can I help?")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            return
        if path.startswith("/api/associate/tickets/") and path.endswith("/resolve"):
            associate = get_associate_session(self)
            if not associate:
                self.send_error(401)
                return
            try:
                tid = int(path.split("/")[4])
            except Exception:
                self.send_error(400)
                return
            ticket = db.get_ticket(tid)
            if not ticket:
                self.send_error(404)
                return
            if ticket["assigned_to"] != associate["id"]:
                self.send_error(403)
                return
            title = data.get("title", "").strip()
            steps = data.get("steps", "").strip()
            tags = data.get("tags", "").strip()
            publish = data.get("publish", False)
            article_id = None
            if publish and title and steps:
                article_id = db.create_article(title, steps, tags, "resolution")
            db.create_resolution(tid, title, steps, publish, article_id)
            db.resolve_ticket(tid)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "article_id": article_id}).encode("utf-8"))
            return
        self.send_error(404)

if __name__ == "__main__":
    nlp.print_model_status()
    print(f"Support Chat running at http://{HOST}:{PORT}/")
    print(f"Associate console at http://{HOST}:{PORT}/associate")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
