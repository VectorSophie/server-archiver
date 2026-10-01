"""Private, auth-gated search web app. See
docs/superpowers/specs/2026-10-01-web-search-app-design.md. Every
route that can return archived content requires a session -- enforced
once, in before_request, not per-route."""
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session

from archiver.credentials import load_secret_key, verify_login
from archiver.db import connect_catalog
from archiver.search import search

STATIC_DIR = Path(__file__).parent / "static"


def create_app(config, credentials_path: Path | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    if credentials_path is None:
        credentials_path = Path(__file__).parent.parent / "credentials.json"
    app.secret_key = load_secret_key(credentials_path)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=config.secure_cookies,
    )

    @app.before_request
    def require_login():
        if request.path == "/login" or request.path.startswith("/static/"):
            return None
        if request.path.startswith("/api/"):
            if "codename" not in session:
                return jsonify({"error": "not authenticated"}), 401
            return None
        if "codename" not in session:
            return redirect("/login")
        return None

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/login")
    def login_page():
        return send_from_directory(STATIC_DIR, "login.html")

    @app.post("/login")
    def login_submit():
        codename = request.form.get("codename", "")
        password = request.form.get("password", "")
        if verify_login(codename, password, credentials_path):
            session["codename"] = codename
            return redirect("/")
        return redirect("/login?failed=1")

    @app.get("/api/whoami")
    def api_whoami():
        return jsonify({"codename": session.get("codename")})

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.post("/api/search")
    def api_search():
        body = request.get_json(silent=True) or {}
        raw_query = body.get("query", "")
        catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
        try:
            try:
                rows = search(catalog_conn, config.data_dir, raw_query, limit=200)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            results = []
            for row in rows:
                channel = catalog_conn.execute(
                    "SELECT name FROM channels WHERE id=?", (row["channel_id"],)
                ).fetchone()
                author = catalog_conn.execute(
                    "SELECT username FROM users WHERE id=?", (row["author_id"],)
                ).fetchone()
                results.append({
                    "time": row["created_utc"],
                    "channel": channel["name"] if channel else row["channel_id"],
                    "author": author["username"] if author else row["author_id"],
                    "content": row["content"],
                })
            return jsonify({"results": results, "count": len(results)})
        finally:
            catalog_conn.close()

    return app
