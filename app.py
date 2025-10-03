# app.py
import os
import time
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# ------------- Configuration -------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.environ.get("DATABASE_URL") or f"sqlite:///{os.path.join(BASE_DIR, 'comments.db')}"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # для простых admin-операций (удаление коммента)

# Rate limiter settings (in-memory)
RATE_LIMIT_WINDOW = 60        # seconds
RATE_LIMIT_MAX = 5            # max posts per window per IP

# ------------- Init extensions -------------
db = SQLAlchemy()


def create_app():
    app = Flask(__name__, static_folder="frontend", static_url_path="/")
    app.config["SQLALCHEMY_DATABASE_URI"] = DB_PATH
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    CORS(app, resources={r"/api/*": {"origins": "*"}})
    db.init_app(app)

    # --- Models ---
    class Comment(db.Model):
        __tablename__ = "comments"
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(80), nullable=False, default="Аноним")
        msg = db.Column(db.Text, nullable=False)
        ts = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        approved = db.Column(db.Boolean, default=True)  # можно использовать для модерации

        def to_dict(self):
            # возвращаем время в ms unix, чтобы JS корректно делал new Date(...)
            return {
                "id": self.id,
                "name": self.name,
                "msg": self.msg,
                "ts": int(self.ts.timestamp() * 1000),
                "approved": bool(self.approved),
            }

    # --- In-memory rate limiter (very simple) ---
    # { ip: [timestamps...] }
    rate_store = {}

    def rate_limited(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            now = time.time()
            arr = rate_store.get(ip, [])
            # remove old timestamps
            arr = [t for t in arr if now - t < RATE_LIMIT_WINDOW]
            if len(arr) >= RATE_LIMIT_MAX:
                return jsonify({"error": "rate_limit_exceeded", "detail": f"Max {RATE_LIMIT_MAX} posts per {RATE_LIMIT_WINDOW}s"}), 429
            arr.append(now)
            rate_store[ip] = arr
            return func(*args, **kwargs)
        return wrapper

    # --- API endpoints ---
    @app.route("/api/ping", methods=["GET"])
    def ping():
        return jsonify({"ok": True, "time": int(datetime.now(timezone.utc).timestamp() * 1000)}), 200

    @app.route("/api/comments", methods=["GET"])
    def get_comments():
        # pagination: ?limit=50&offset=0
        try:
            limit = min(200, int(request.args.get("limit", 100)))
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            return jsonify({"error": "invalid pagination"}), 400

        CommentModel = db.get_model("comments") if hasattr(db, "get_model") else Comment  # compatibility
        items = CommentModel.query.filter_by(approved=True).order_by(CommentModel.ts.desc()).offset(offset).limit(limit).all()
        return jsonify([c.to_dict() for c in items]), 200

    @app.route("/api/comments", methods=["POST"])
    @rate_limited
    def post_comment():
        # Accept JSON body; also support form fallback
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form.to_dict()

        name = (data.get("name") or "Аноним").strip()[:80]
        msg = (data.get("msg") or data.get("message") or "").strip()[:2000]

        if not msg:
            return jsonify({"error": "empty_message"}), 400

        c = Comment(name=name or "Аноним", msg=msg, ts=datetime.now(timezone.utc))
        db.session.add(c)
        db.session.commit()
        return jsonify(c.to_dict()), 201

    @app.route("/api/comments/<int:comment_id>", methods=["DELETE"])
    def delete_comment(comment_id):
        # simple admin deletion protected by ADMIN_KEY
        key = request.headers.get("X-ADMIN-KEY") or request.args.get("admin_key") or ""
        if not ADMIN_KEY or key != ADMIN_KEY:
            return jsonify({"error": "forbidden"}), 403
        c = Comment.query.get(comment_id)
        if not c:
            return jsonify({"error": "not_found"}), 404
        db.session.delete(c)
        db.session.commit()
        return jsonify({"ok": True}), 200

    # --- Serve frontend (SPA-friendly) ---
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        # if path exists in static folder - serve it, else serve index.html
        full = os.path.join(app.static_folder, path)
        if path != "" and os.path.exists(full) and os.path.isfile(full):
            return send_from_directory(app.static_folder, path)
        return send_from_directory(app.static_folder, "index.html")

    # --- Error handlers (nice JSON for /api/*) ---
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_found"}), 404
        # otherwise let the SPA handle routing (fallback to index)
        return send_from_directory(app.static_folder, "index.html")

    @app.errorhandler(500)
    def internal_err(e):
        app.logger.exception("Server error")
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal_error"}), 500
        return send_from_directory(app.static_folder, "index.html")

    # Create tables at startup (no before_first_request)
    with app.app_context():
        db.create_all()

    # expose model for potential external use
    app.Comment = Comment

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
