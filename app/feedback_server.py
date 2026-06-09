from __future__ import annotations

import argparse
import html as html_lib
import json
import os
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from app.db import (
    admin_rows,
    append_feedback_event,
    apply_rule_overlays,
    article_detail,
    connect_database,
    init_database,
    rule_terms,
    set_rule_term_enabled,
    upsert_rule_term,
)
from app.settings import (
    DEFAULT_ENV_FILE,
    DEFAULT_HN_HOT_QUERIES,
    DEFAULT_HN_HOT_QUERIES_FILE,
    DEFAULT_RULES_FILE,
    env_or_default,
    load_env_file,
    load_json_config,
    load_rules_config,
)

ALLOWED_ACTIONS = {"keep", "save", "mute", "mute-domain", "promote-profile", "demote-profile", "boost-topic"}


def append_feedback(state_dir: Path, event: Dict[str, Any]) -> None:
    path = state_dir / "feedback.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: list[Dict[str, Any]] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                payload = [item for item in loaded if isinstance(item, dict)]
        except Exception:
            payload = []
    payload.append(event)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class FeedbackRequestHandler(SimpleHTTPRequestHandler):
    state_dir: Path
    database_path: Path
    admin_token: str

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") == "/feedback":
            self.handle_feedback(parsed.query)
            return
        if parsed.path.startswith("/api/"):
            self.handle_api_get(parsed)
            return
        if parsed.path.startswith("/admin"):
            self.handle_admin_page(parsed)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/feedback"):
            self.handle_api_feedback(parsed)
            return
        if parsed.path.startswith("/api/save"):
            self.handle_api_save(parsed)
            return
        if parsed.path.startswith("/api/rules"):
            self.handle_api_rules_post(parsed)
            return
        if parsed.path.startswith("/admin/rules"):
            self.handle_admin_rules_post(parsed)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def db(self):
        conn = connect_database(self.database_path)
        hn_hot_config = load_json_config(DEFAULT_HN_HOT_QUERIES_FILE, DEFAULT_HN_HOT_QUERIES)
        init_database(conn, self.state_dir, hn_hot_queries=list(hn_hot_config.get("queries") or []))
        return conn

    def handle_feedback(self, query: str) -> None:
        params = parse_qs(query)
        action = (params.get("action") or [""])[0]
        guid = (params.get("guid") or [""])[0]
        source = (params.get("source") or [""])[0]
        url = (params.get("url") or [""])[0]

        if action not in ALLOWED_ACTIONS or not guid:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Invalid feedback request.\n")
            return

        append_feedback(
            self.state_dir,
            {
                "action": action,
                "guid": guid,
                "source": source,
                "url": url,
                "remote_addr": self.client_address[0],
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        try:
            conn = self.db()
            append_feedback_event(
                conn,
                {
                    "action": action,
                    "guid": guid,
                    "source": source,
                    "url": url,
                    "remote_addr": self.client_address[0],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            conn.close()
        except Exception as exc:
            print(f"[WARN] DB feedback append failed: {exc}", flush=True)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<!doctype html><meta charset=\"utf-8\"><title>Feedback saved</title>"
            b"<p>Feedback saved. You can close this page.</p>\n"
        )

    def read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def admin_authorized(self, parsed: Any) -> bool:
        token = self.admin_token
        if not token:
            return False
        query_token = (parse_qs(parsed.query).get("token") or [""])[0]
        header_token = self.headers.get("X-Admin-Token", "")
        auth = self.headers.get("Authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
        return token in {query_token, header_token, bearer}

    def require_admin(self, parsed: Any) -> bool:
        if self.admin_authorized(parsed):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Missing or invalid admin token.\n")
        return False

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_get(self, parsed: Any) -> None:
        if not self.require_admin(parsed):
            return
        conn = self.db()
        try:
            path = parsed.path.rstrip("/")
            if path == "/api/articles":
                self.send_json(admin_rows(conn, "articles", limit=200))
            elif path.startswith("/api/articles/"):
                guid = path.rsplit("/", 1)[-1]
                detail = article_detail(conn, guid)
                self.send_json(detail or {"error": "not found"}, HTTPStatus.OK if detail else HTTPStatus.NOT_FOUND)
            elif path == "/api/saved":
                self.send_json(admin_rows(conn, "saved_articles", limit=200))
            elif path == "/api/feedback":
                self.send_json(admin_rows(conn, "feedback_events", limit=200))
            elif path == "/api/hn-hot":
                self.send_json(admin_rows(conn, "hn_hot", limit=200))
            elif path == "/api/fetch-runs":
                self.send_json(admin_rows(conn, "fetch_runs", limit=100))
            elif path == "/api/sources":
                self.send_json(admin_rows(conn, "sources", limit=200))
            elif path == "/api/rules":
                base = load_rules_config(DEFAULT_RULES_FILE)
                effective = apply_rule_overlays(conn, base)
                self.send_json({"overlay_terms": rule_terms(conn), "effective_rules": effective})
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        finally:
            conn.close()

    def handle_api_rules_post(self, parsed: Any) -> None:
        if not self.require_admin(parsed):
            return
        payload = self.read_json_body()
        conn = self.db()
        try:
            action = str(payload.get("action") or "upsert")
            if action == "disable":
                ok = set_rule_term_enabled(conn, int(payload.get("id") or 0), False)
                self.send_json({"ok": ok})
                return
            if action == "enable":
                ok = set_rule_term_enabled(conn, int(payload.get("id") or 0), True)
                self.send_json({"ok": ok})
                return
            profile = str(payload.get("profile") or "").strip()
            term_type = str(payload.get("term_type") or "").strip()
            term = str(payload.get("term") or "").strip()
            if not profile or not term_type or not term:
                self.send_json({"error": "profile, term_type and term are required"}, HTTPStatus.BAD_REQUEST)
                return
            upsert_rule_term(
                conn,
                profile=profile,
                term_type=term_type,
                term=term,
                enabled=payload.get("enabled", True) is not False,
                notes=str(payload.get("notes") or ""),
            )
            self.send_json({"ok": True})
        finally:
            conn.close()

    def handle_api_feedback(self, parsed: Any) -> None:
        if not self.require_admin(parsed):
            return
        payload = self.read_json_body()
        action = str(payload.get("action") or "")
        guid = str(payload.get("guid") or payload.get("article_guid") or "")
        if action not in ALLOWED_ACTIONS or not guid:
            self.send_json({"error": "invalid feedback"}, HTTPStatus.BAD_REQUEST)
            return
        payload["remote_addr"] = self.client_address[0]
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        conn = self.db()
        try:
            append_feedback_event(conn, payload)
            self.send_json({"ok": True})
        finally:
            conn.close()

    def handle_api_save(self, parsed: Any) -> None:
        if not self.require_admin(parsed):
            return
        payload = self.read_json_body()
        guid = str(payload.get("guid") or payload.get("article_guid") or "")
        if not guid:
            self.send_json({"error": "missing guid"}, HTTPStatus.BAD_REQUEST)
            return
        payload["action"] = "save"
        payload["guid"] = guid
        payload["remote_addr"] = self.client_address[0]
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        conn = self.db()
        try:
            append_feedback_event(conn, payload)
            self.send_json({"ok": True})
        finally:
            conn.close()

    def handle_admin_page(self, parsed: Any) -> None:
        if not self.require_admin(parsed):
            return
        conn = self.db()
        try:
            token_qs = f"?token={parse_qs(parsed.query).get('token', [''])[0]}" if parse_qs(parsed.query).get("token") else ""
            path = parsed.path.rstrip("/") or "/admin"
            if path == "/admin":
                articles = admin_rows(conn, "articles", limit=20)
                runs = admin_rows(conn, "fetch_runs", limit=5)
                body = self.render_table("Latest Articles", articles, token_qs) + self.render_table("Fetch Runs", runs, token_qs)
            elif path == "/admin/articles":
                body = self.render_table("Articles", admin_rows(conn, "articles", limit=200), token_qs)
            elif path.startswith("/admin/article/"):
                guid = path.rsplit("/", 1)[-1]
                detail = article_detail(conn, guid)
                body = f"<pre>{json.dumps(detail or {'error': 'not found'}, ensure_ascii=False, indent=2)}</pre>"
            elif path == "/admin/saved":
                body = self.render_table("Saved Articles", admin_rows(conn, "saved_articles", limit=200), token_qs)
            elif path == "/admin/feedback":
                body = self.render_table("Feedback", admin_rows(conn, "feedback_events", limit=200), token_qs)
            elif path == "/admin/hn-hot":
                body = self.render_table("HN Hot", admin_rows(conn, "hn_hot", limit=200), token_qs)
            elif path == "/admin/sources":
                body = self.render_table("Sources", admin_rows(conn, "sources", limit=200), token_qs)
            elif path == "/admin/rules":
                body = self.render_rules_page(conn, token_qs)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            html = self.admin_shell(body, token_qs)
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        finally:
            conn.close()

    def admin_shell(self, body: str, token_qs: str) -> str:
        nav = " · ".join(
            f'<a href="{path}{token_qs}">{label}</a>'
            for path, label in (
                ("/admin/", "Overview"),
                ("/admin/articles", "Articles"),
                ("/admin/saved", "Saved"),
                ("/admin/feedback", "Feedback"),
                ("/admin/hn-hot", "HN Hot"),
                ("/admin/sources", "Sources"),
                ("/admin/rules", "Rules"),
            )
        )
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>RSSNews Admin</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; color: #1f2937; }}
a {{ color: #0f766e; }} table {{ border-collapse: collapse; width: 100%; margin: 16px 0 32px; font-size: 14px; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #f8fafc; position: sticky; top: 0; }} code {{ white-space: nowrap; }}
input, select, button {{ font: inherit; padding: 6px 8px; margin: 4px; }}
form.inline {{ display: inline; }}
</style></head><body><h1>RSSNews Admin</h1><nav>{nav}</nav>{body}</body></html>"""

    def render_table(self, title: str, rows: list[Dict[str, Any]], token_qs: str) -> str:
        if not rows:
            return f"<h2>{title}</h2><p>No rows.</p>"
        keys = list(rows[0].keys())[:10]
        head = "".join(f"<th>{html_lib.escape(str(key))}</th>" for key in keys)
        body_rows = []
        for row in rows:
            cells = []
            for key in keys:
                value = row.get(key)
                if key == "action":
                    text = str(value if value is not None else "")
                else:
                    text = html_lib.escape(str(value if value is not None else ""))
                if key == "guid":
                    text = f'<a href="/admin/article/{text}{token_qs}"><code>{text[:12]}</code></a>'
                elif key in {"url", "link"} and text.startswith("http"):
                    text = f'<a href="{text}">{text[:80]}</a>'
                else:
                    text = text[:300]
                cells.append(f"<td>{text}</td>")
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        return f"<h2>{title}</h2><table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"

    def render_rules_page(self, conn: Any, token_qs: str) -> str:
        overlays = rule_terms(conn)
        form = f"""
<h2>Add Rule Term</h2>
<form method="post" action="/admin/rules{token_qs}">
  <select name="profile">
    <option value="tech_industry_major">tech_industry_major</option>
    <option value="devops_security">devops_security</option>
    <option value="major_events">major_events</option>
    <option value="feeds">feeds</option>
    <option value="queries">queries</option>
  </select>
  <select name="term_type">
    <option value="must_track">must_track</option>
    <option value="watch">watch</option>
    <option value="exclude">exclude</option>
    <option value="watched_entity">watched_entity</option>
    <option value="newsapi">newsapi</option>
    <option value="gdelt">gdelt</option>
  </select>
  <input name="term" placeholder="term" required>
  <input name="notes" placeholder="notes">
  <button type="submit">Save</button>
</form>
"""
        rows = []
        for row in overlays:
            action = "disable" if int(row.get("enabled") or 0) else "enable"
            row_id = int(row["id"])
            button = f"""
<form class="inline" method="post" action="/admin/rules{token_qs}">
  <input type="hidden" name="action" value="{action}">
  <input type="hidden" name="id" value="{row_id}">
  <button type="submit">{action}</button>
</form>
"""
            decorated = dict(row)
            decorated["action"] = button
            rows.append(decorated)
        return form + self.render_table("Rule Overlays", rows, token_qs)

    def handle_admin_rules_post(self, parsed: Any) -> None:
        if not self.require_admin(parsed):
            return
        length = int(self.headers.get("Content-Length") or 0)
        params = parse_qs(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        conn = self.db()
        try:
            action = (params.get("action") or ["upsert"])[0]
            if action in {"enable", "disable"}:
                set_rule_term_enabled(conn, int((params.get("id") or ["0"])[0]), action == "enable")
            else:
                upsert_rule_term(
                    conn,
                    profile=(params.get("profile") or [""])[0],
                    term_type=(params.get("term_type") or [""])[0],
                    term=(params.get("term") or [""])[0],
                    notes=(params.get("notes") or [""])[0],
                )
            self.send_response(HTTPStatus.SEE_OTHER)
            location = "/admin/rules"
            if parsed.query:
                location += "?" + parsed.query
            self.send_header("Location", location)
            self.end_headers()
        finally:
            conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve RSS static files and accept feedback links")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--directory")
    parser.add_argument("--bind")
    parser.add_argument("--port", type=int)
    parser.add_argument("--state-dir")
    parser.add_argument("--database-path")
    parser.add_argument("--admin-token")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    publish_dir = Path(args.directory or env_or_default("PUBLISH_DIR", "/opt/rssnews/public"))
    bind = args.bind or env_or_default("RSS_BIND", "0.0.0.0")
    port = args.port if args.port is not None else env_or_default("RSS_PORT", 8081, int)
    state_dir = Path(args.state_dir or env_or_default("STATE_DIR", "state"))
    database_path = Path(args.database_path or env_or_default("DATABASE_PATH", str(state_dir / "rssnews.db")))
    admin_token = args.admin_token if args.admin_token is not None else env_or_default("ADMIN_TOKEN", "")

    publish_dir.mkdir(parents=True, exist_ok=True)
    hn_hot_config = load_json_config(DEFAULT_HN_HOT_QUERIES_FILE, DEFAULT_HN_HOT_QUERIES)
    init_conn = connect_database(database_path)
    init_database(init_conn, state_dir, hn_hot_queries=list(hn_hot_config.get("queries") or []))
    init_conn.close()
    handler = lambda *handler_args, **handler_kwargs: FeedbackRequestHandler(  # noqa: E731
        *handler_args,
        directory=str(publish_dir),
        **handler_kwargs,
    )
    FeedbackRequestHandler.state_dir = state_dir
    FeedbackRequestHandler.database_path = database_path
    FeedbackRequestHandler.admin_token = str(admin_token)
    server = ThreadingHTTPServer((bind, port), handler)
    print(f"[OK] Serving {publish_dir} on {bind}:{port}; feedback state={state_dir}; database={database_path}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
