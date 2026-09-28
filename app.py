#!/usr/bin/env python3
"""HeatStar shared API.

Standard-library-only service: SQLite persistence, atomic key/value state,
upload storage, and real XLSX personnel import preview/template generation.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import traceback
import urllib.parse
import zipfile
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth
import export_docx
import export_pptx
import export_access
import export_ledger
import organization
import proposals as proposal_rules
import company_workflow


APP_VERSION = "2.8.1"
ALLOWED_KEYS = {
    "heatstar_proposals",
    "heatstar_people",
    "heatstar_teams",
    "heatstar_departments",
    "heatstar_permissions",
    "heatstar_notices",
    "heatstar_settings",
    "heatstar_candidates",
    "heatstar_shares",
    "heatstar_categories",
    "heatstar_honor_results",
}
ALLOWED_ORIGINS = {
    "https://gy.rechuangxing.xyz",
    "https://gyht.rechuangxing.xyz",
    "http://gy.rechuangxing.xyz",
    "http://gyht.rechuangxing.xyz",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
}
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HEATSTAR_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "heatstar.sqlite3"
SEED_PATH = ROOT / "seed_state.json"
WRITE_LOCK = threading.RLock()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(str(DB_PATH), timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextlib.contextmanager
def database_connection():
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with database_connection() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_key TEXT NOT NULL,
                action TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                client_ip TEXT
            );
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stored_name TEXT UNIQUE NOT NULL,
                original_name TEXT,
                mime_type TEXT,
                byte_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS number_counters (
                counter_key TEXT PRIMARY KEY,
                value INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in database.execute("PRAGMA table_info(audit_log)")}
        if "actor" not in columns:
            database.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT")

        count = database.execute("SELECT COUNT(*) FROM app_state").fetchone()[0]
        if count == 0 and SEED_PATH.exists():
            seeds = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            timestamp = utc_now()
            for key, value in seeds.items():
                if key in ALLOWED_KEYS:
                    database.execute(
                        "INSERT INTO app_state(key, value_json, revision, updated_at) VALUES (?, ?, 1, ?)",
                        (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), timestamp),
                    )
        database.commit()

    migrate_workflow()
    auth.configure(DB_PATH, get_states)
    auth.initialize()


def get_states() -> tuple[dict, dict, int]:
    with database_connection() as database:
        rows = database.execute("SELECT key, value_json, revision, updated_at FROM app_state").fetchall()
    states = {}
    meta = {}
    highest = 0
    for row in rows:
        states[row["key"]] = json.loads(row["value_json"])
        meta[row["key"]] = {"revision": row["revision"], "updatedAt": row["updated_at"]}
        highest = max(highest, int(row["revision"]))
    return states, meta, highest


def normalize_people(people, states) -> list:
    """人员写入前按班组补全工段，避免漏填导致工段长管辖范围失效。"""
    return organization.normalize_people(people, states)


def migrate_workflow():
    """Idempotent upgrade: keep decisions/history, move only unfinished old queues."""
    states, _, _ = get_states()
    timestamp = utc_now()
    changes = {}
    if 'heatstar_departments' not in states:
        changes['heatstar_departments'] = organization.departments(states)
    items = states.get('heatstar_proposals') or []
    migrated = []
    for item in items:
        if item.get('status') in ('pending_section', 'pending_factory'):
            item = {**item, 'status': 'pending_review', 'statusText': '评审小组审核',
                    'workflowVersion': 2, 'workflowMigratedAt': timestamp}
        migrated.append(item)
    if migrated != items:
        changes['heatstar_proposals'] = migrated
    with database_connection() as database:
        for key, value in changes.items():
            revision = int(database.execute('SELECT COALESCE(MAX(revision), 0) FROM app_state WHERE key=?', (key,)).fetchone()[0]) + 1
            database.execute('INSERT INTO app_state(key,value_json,revision,updated_at) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision,updated_at=excluded.updated_at',
                             (key, json.dumps(value, ensure_ascii=False), revision, timestamp))
            database.execute("INSERT INTO audit_log(state_key,action,revision,created_at,actor) VALUES(?,'workflow-upgrade',?,?,?)", (key, revision, timestamp, 'system'))
        database.commit()


def set_state(key: str, value, client_ip: str = "", actor: str = "") -> dict:
    if key not in ALLOWED_KEYS:
        raise ValueError("不允许写入该数据项")
    if key == "heatstar_people":
        current_states, _, _ = get_states()
        value = normalize_people(value, current_states)
    if key == 'heatstar_departments':
        states, _, _ = get_states()
        organization.validate_departments(value, states)
    if key == 'heatstar_teams':
        if not isinstance(value, list):
            raise ValueError('班组名单格式无效')
        states, _, _ = get_states()
        known = {d['id'] for d in organization.departments(states)}
        seen, team_ids = set(), set()
        for team in value:
            if not isinstance(team, dict) or not team.get('id') or team['id'] in team_ids:
                raise ValueError('班组标识无效或重复')
            identity = (organization.department_id(team), team.get('name'))
            if identity[0] not in known or not team.get('name') or identity in seen:
                raise ValueError('班组所属部门无效或部门内班组名称重复')
            seen.add(identity)
            team_ids.add(team['id'])
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 12 * 1024 * 1024:
        raise ValueError("数据项超过 12MB 限制")
    timestamp = utc_now()
    with WRITE_LOCK, database_connection() as database:
        row = database.execute("SELECT revision FROM app_state WHERE key = ?", (key,)).fetchone()
        revision = int(row["revision"]) + 1 if row else 1
        database.execute(
            """
            INSERT INTO app_state(key, value_json, revision, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                revision=excluded.revision, updated_at=excluded.updated_at
            """,
            (key, encoded, revision, timestamp),
        )
        database.execute(
            "INSERT INTO audit_log(state_key, action, revision, created_at, client_ip, actor) VALUES (?, 'replace', ?, ?, ?, ?)",
            (key, revision, timestamp, client_ip, actor),
        )
        database.commit()
    if key == "heatstar_people":
        # 人员名单一变，登录账号跟着开通/停用。
        auth.sync_accounts()
    return {"key": key, "revision": revision, "updatedAt": timestamp}


def reserve_proposal_number(kind: str = "six", user=None) -> str:
    """由服务端统一发号。精益和六源都走这里，避免两人同时提交撞出同一个编号。"""
    now = dt.datetime.now()
    states, _, _ = get_states()
    settings = states.get("heatstar_settings") or {}
    digits = min(6, max(3, int(settings.get("sequenceDigits") or 4)))
    department = next((d for d in organization.departments(states) if d['id'] == organization.department_id(user)), None)
    prefix = proposal_rules.number_prefix(kind, states, now, department)
    existing_max = proposal_rules.highest_used(prefix, states)
    counter_key = f"{kind}:{prefix}"
    with WRITE_LOCK, database_connection() as database:
        row = database.execute("SELECT value FROM number_counters WHERE counter_key = ?", (counter_key,)).fetchone()
        next_value = max(existing_max, int(row["value"]) if row else 0) + 1
        database.execute(
            """INSERT INTO number_counters(counter_key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(counter_key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (counter_key, next_value, utc_now()),
        )
        database.commit()
    return f"{prefix}{next_value:0{digits}d}"


def filter_proposals(items: list, query: dict) -> tuple[list, str]:
    """按月份、班组、工段、状态、类型筛选要导出的提案。"""
    def first(name: str) -> str:
        return str(query.get(name, [""])[0] or "").strip()

    month, team, section, status, kind = (
        first("month"), first("team"), first("section"), first("status"), first("type")
    )
    did = first('departmentId')
    picked = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if did and organization.department_id(item) != did:
            continue
        when = str(item.get("reviewedAt") or item.get("submittedAt") or "")
        if month and not when.startswith(month):
            continue
        if team and item.get("team") != team:
            continue
        if section and item.get("section") != section:
            continue
        if status and item.get("status") != status:
            continue
        if kind and item.get("type") != kind:
            continue
        picked.append(item)
    picked.sort(key=lambda x: str(x.get("submittedAt") or ""), reverse=True)
    label = "、".join(x for x in (month, section, team, export_docx.STATUS_TEXT.get(status, "")) if x)
    return picked, label


def local_now_text() -> str:
    """服务器本地时间，格式与前端展示保持一致。"""
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def save_proposal_atomic(user: dict, incoming: dict, client_ip: str = "") -> dict:
    """读—合并—写全程持锁，保证并发提交/审核不会互相覆盖。"""
    with WRITE_LOCK:
        states, _, _ = get_states()
        existing = states.get("heatstar_proposals") or []
        old = next((x for x in existing if proposal_rules.proposal_id(x) == proposal_rules.proposal_id(incoming)), None)
        incoming = organization.submission(incoming, old, user, states)
        merged, record = company_workflow.merge_one(existing, incoming, user, states, local_now_text())
        set_state("heatstar_proposals", merged, client_ip, user.get("name") or user.get("workNo"))
    return record


def parse_multipart(content_type: str, body: bytes) -> list[dict]:
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    parts = []
    if not message.is_multipart():
        return parts
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition") or ""
        parts.append(
            {
                "name": name,
                "filename": part.get_filename() or "",
                "content_type": part.get_content_type(),
                "data": part.get_payload(decode=True) or b"",
            }
        )
    return parts


def safe_filename(filename: str, fallback_ext: str = "") -> tuple[str, str]:
    original = Path(filename or "upload").name
    suffix = Path(original).suffix.lower() or fallback_ext
    suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else fallback_ext
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original).stem).strip("-")[:36] or "file"
    token = hashlib.sha256((original + utc_now()).encode("utf-8")).hexdigest()[:16]
    return original, f"{stem}-{token}{suffix}"


def store_upload(part: dict, kind: str) -> dict:
    data = part["data"]
    mime_type = (part.get("content_type") or "application/octet-stream").lower()
    limits = {"photo": 20 * 1024 * 1024, "attachment": 20 * 1024 * 1024}
    limit = limits.get(kind, limits["attachment"])
    if not data:
        raise ValueError("上传文件为空")
    if len(data) > limit:
        label = "20MB"
        raise ValueError(f"文件超过 {label} 限制")
    if kind == "photo" and not mime_type.startswith("image/"):
        raise ValueError("照片仅支持图片格式")
    guessed_ext = mimetypes.guess_extension(mime_type) or ""
    original, stored_name = safe_filename(part.get("filename", ""), guessed_ext)
    target = (UPLOAD_DIR / stored_name).resolve()
    if target.parent != UPLOAD_DIR:
        raise ValueError("非法文件名")
    target.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    with database_connection() as database:
        database.execute(
            """INSERT INTO uploads(stored_name, original_name, mime_type, byte_size, sha256, kind, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (stored_name, original, mime_type, len(data), digest, kind, utc_now()),
        )
        database.commit()
    return {
        "name": original,
        "storedName": stored_name,
        "size": len(data),
        "mimeType": mime_type,
        "sha256": digest,
        "url": f"/uploads/{urllib.parse.quote(stored_name)}",
    }


def column_number(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    value = 0
    for character in (letters.group(0).upper() if letters else "A"):
        value = value * 26 + ord(character) - 64
    return value - 1


def read_xlsx(data: bytes) -> list[dict]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        if "xl/worksheets/sheet1.xml" not in names:
            raise ValueError("Excel 中未找到第一个工作表")
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{namespace}si"):
                shared.append("".join(node.text or "" for node in item.iter(f"{namespace}t")))
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        matrix = []
        for row in root.iter(f"{namespace}row"):
            values = []
            for cell in row.findall(f"{namespace}c"):
                index = column_number(cell.attrib.get("r", "A1"))
                while len(values) <= index:
                    values.append("")
                cell_type = cell.attrib.get("t", "")
                if cell_type == "inlineStr":
                    inline = cell.find(f"{namespace}is")
                    value = "" if inline is None else "".join(node.text or "" for node in inline.iter(f"{namespace}t"))
                else:
                    value_node = cell.find(f"{namespace}v")
                    value = value_node.text if value_node is not None and value_node.text is not None else ""
                    if cell_type == "s" and value:
                        value = shared[int(value)]
                values[index] = str(value).strip()
            matrix.append(values)
    matrix = [row for row in matrix if any(str(value).strip() for value in row)]
    if not matrix:
        return []
    header_aliases = {
        "姓名": "name", "工号": "workNo", "班组": "team", "岗位": "role",
        "审核层级": "level", "管辖范围": "scope", "累计积分": "points", "在岗状态": "status",
        "部门": "department", "所属部门": "department",
    }
    headers = [header_aliases.get(str(value).strip(), "") for value in matrix[0]]
    required = {"name", "workNo", "team", "role"}
    if not required.issubset(set(headers)):
        raise ValueError("表头不完整，请使用系统下载的模板")
    rows = []
    for source in matrix[1:502]:
        item = {}
        for index, key in enumerate(headers):
            if key:
                value = source[index] if index < len(source) else ""
                if key == "workNo" and re.fullmatch(r"\d+\.0", value):
                    value = value[:-2]
                item[key] = value
        if not any(str(value).strip() for value in item.values()):
            continue
        try:
            item["points"] = float(item.get("points") or 0)
            if item["points"].is_integer():
                item["points"] = int(item["points"])
        except (TypeError, ValueError):
            item["points"] = 0
        item["level"] = item.get("level") or "无"
        item["scope"] = item.get("scope") or "本人"
        item["status"] = item.get("status") or "在岗"
        rows.append(item)
    if len(matrix) - 1 > 500:
        raise ValueError("单次导入不得超过 500 条")
    return rows


def xlsx_template() -> bytes:
    rows = [
        ["姓名", "工号", "班组", "岗位", "审核层级", "管辖范围", "累计积分", "在岗状态", "部门"],
        ["张建国", "60821", "热三组", "员工", "无", "本人", "0", "在岗", "热处理厂"],
    ]
    row_xml = []
    for row_index, values in enumerate(rows, 1):
        cells = []
        for column_index, value in enumerate(values):
            number = column_index + 1
            letters = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                letters = chr(65 + remainder) + letters
            cells.append(f'<c r="{letters}{row_index}" t="inlineStr"><is><t>{value}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + "".join(row_xml) + '</sheetData></worksheet>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="人员导入" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


class HeatStarHandler(BaseHTTPRequestHandler):
    server_version = "HeatStarAPI/2.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write(f"{self.address_string()} [{self.log_date_time_string()}] {fmt % args}\n")
        sys.stdout.flush()

    @property
    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        return (forwarded.split(",")[0].strip() if forwarded else self.client_address[0])[:64]

    def cors_headers(self) -> dict:
        origin = self.headers.get("Origin", "")
        headers = {"Vary": "Origin"}
        local_origin = re.fullmatch(r"http://(?:localhost|127\.0\.0\.1):\d{2,5}", origin)
        if origin in ALLOWED_ORIGINS or local_origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, OPTIONS"
            headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            headers["Access-Control-Max-Age"] = "600"
        return headers

    def bearer_token(self) -> str:
        header = self.headers.get("Authorization", "")
        if header[:7].lower() == "bearer ":
            return header[7:].strip()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (query.get("token", [""])[0] or "").strip()

    def current_user(self) -> dict | None:
        return auth.resolve_token(self.bearer_token())

    def require_user(self) -> dict:
        user = self.current_user()
        if not user:
            raise auth.AuthError("登录已过期，请重新登录", 401)
        return user

    def require_admin(self) -> dict:
        user = self.require_user()
        if not user.get("isAdmin"):
            raise auth.AuthError("该操作需要管理员权限", 403)
        return user

    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in self.cors_headers().items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, status: int, data: bytes, content_type: str, filename: str = "", cache: str = "private, max-age=3600") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        if filename:
            encoded = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        for key, value in self.cors_headers().items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def read_body(self, maximum: int = 24 * 1024 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("无效的请求长度")
        if length <= 0:
            return b""
        if length > maximum:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def read_json(self) -> dict:
        body = self.read_body(12 * 1024 * 1024)
        try:
            return json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("JSON 格式错误")

    def route(self) -> tuple[str, urllib.parse.ParseResult]:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        elif path == "/api":
            path = "/"
        return path, parsed

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        for key, value in self.cors_headers().items():
            self.send_header(key, value)
        self.end_headers()

    def export_proposals(self, path, parsed, body=None):
        user = self.require_user()
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        states, _, _ = get_states()
        visible, access_scope = export_access.visible_proposals(user, states)
        picked, scope = filter_proposals(visible, query)
        if body is not None:
            if not isinstance(body, dict):
                raise ValueError('无效的导出请求')
            picked = export_access.select_proposals(picked, body.get('ids'))
        elif 'id' in query:
            picked = export_access.select_proposals(picked, query['id'])
        if path.endswith('/preview'):
            self.send_json(200, {
                'ok': True, 'count': len(picked), 'scope': scope, 'accessScope': access_scope,
                'sixCount': export_pptx.count_six(picked),
                'wordCount': len(export_access.select_format(picked, 'docx')),
                'items': [{
                    **{key: x.get(key) for key in
                       ('id', 'sourceNo', 'title', 'type', 'team', 'firstAuthor', 'status', 'stars', 'submittedAt')},
                    'departmentId': organization.department_id(x),
                    'department': organization.department_name(x, states),
                    'exportFormat': export_access.proposal_export_format(x),
                } for x in picked],
            })
            return
        if path.endswith('.xlsx'):
            if not picked:
                raise ValueError('该范围内没有可导出的提案')
            self.send_bytes(200, export_ledger.build(picked, states),
                            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            '热创星提案台账.xlsx', cache='no-store')
            return
        extension = 'pptx' if path.endswith('.pptx') else 'docx'
        matching = export_access.select_format(picked, extension)
        explicitly_selected = body is not None or 'id' in query
        if explicitly_selected and len(matching) != len(picked):
            if extension == 'pptx':
                raise ValueError('PPT 固定用于六源改善，其他提案请使用 Word 导出')
            raise ValueError('六源改善固定使用 PPT 模板，请勿导出为 Word')
        picked = matching
        if not picked:
            label = '六源改善' if extension == 'pptx' else '精益提案、工具开发或 OPL'
            raise ValueError(f'该范围内没有可导出的{label}')
        if path.endswith('.pptx'):
            module = export_pptx
            mime = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        else:
            module = export_docx
            mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        picked = [{**x, 'department': organization.department_name(x, states)} for x in picked]
        self.send_bytes(200, module.build(picked, UPLOAD_DIR), mime,
                        module.filename_for(scope or access_scope), cache='no-store')

    def do_GET(self) -> None:
        try:
            path, parsed = self.route()
            if path in ("/", "/health"):
                states, _, highest = get_states()
                self.send_json(200, {"ok": True, "service": "heatstar-api", "version": APP_VERSION, "stateCount": len(states), "revision": highest, "time": utc_now()})
                return
            if path == "/auth/session":
                user = self.current_user()
                if not user:
                    self.send_json(401, {"ok": False, "auth": True, "message": "登录已过期，请重新登录"})
                else:
                    self.send_json(200, {"ok": True, "user": user})
                return
            if path == "/auth/accounts":
                self.require_admin()
                self.send_json(200, {"ok": True, "accounts": auth.list_accounts(), "defaultPassword": auth.DEFAULT_PASSWORD})
                return
            if path == "/bootstrap":
                user = self.require_user()
                states, meta, highest = get_states()
                self.send_json(200, {"ok": True, "version": APP_VERSION, "revision": highest, "states": states, "meta": meta, "user": user, "serverTime": utc_now()})
                return
            if path.startswith("/state/"):
                self.require_user()
                key = urllib.parse.unquote(path[len("/state/"):])
                states, meta, _ = get_states()
                if key not in states:
                    self.send_json(404, {"ok": False, "message": "数据项不存在"})
                else:
                    self.send_json(200, {"ok": True, "key": key, "value": states[key], **meta[key]})
                return
            if path in ('/export/proposals.docx', '/export/proposals.pptx', '/export/proposals.xlsx', '/export/proposals/preview'):
                self.export_proposals(path, parsed)
                return
            if path == "/templates/personnel.xlsx":
                self.require_admin()
                # 需要鉴权的资源不做缓存，避免换账号后仍能从浏览器缓存取到。
                self.send_bytes(200, xlsx_template(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "热创星人员导入模板.xlsx", cache="no-store")
                return
            if path.startswith("/uploads/"):
                name = Path(urllib.parse.unquote(path[len("/uploads/"):])).name
                target = (UPLOAD_DIR / name).resolve()
                if target.parent != UPLOAD_DIR or not target.is_file():
                    self.send_json(404, {"ok": False, "message": "文件不存在"})
                    return
                mime_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                self.send_bytes(200, target.read_bytes(), mime_type)
                return
            self.send_json(404, {"ok": False, "message": "接口不存在"})
        except Exception as error:
            self.handle_error(error)

    def do_PUT(self) -> None:
        try:
            path, _ = self.route()
            if path.startswith("/state/"):
                user = self.require_user()
                key = urllib.parse.unquote(path[len("/state/"):])
                if not auth.can_write_key(user, key):
                    raise auth.AuthError("该数据只有管理员可以修改", 403)
                body = self.read_json()
                if "value" not in body:
                    raise ValueError("缺少 value 字段")
                value = body["value"]
                if key == "heatstar_proposals":
                    # 整表提交按编号逐条合并，旧客户端推上来的过期快照不会抹掉别人的提案
                    with WRITE_LOCK:
                        states, _, _ = get_states()
                        existing = states.get("heatstar_proposals") or []
                        value = proposal_rules.merge_table(existing, value if isinstance(value, list) else [], user)
                        meta = set_state(key, value, self.client_ip, user.get("name") or user.get("workNo"))
                else:
                    meta = set_state(key, value, self.client_ip, user.get("name") or user.get("workNo"))
                self.send_json(200, {"ok": True, **meta})
                return
            self.send_json(404, {"ok": False, "message": "接口不存在"})
        except Exception as error:
            self.handle_error(error)

    def do_POST(self) -> None:
        try:
            path, parsed = self.route()
            if path in ('/export/proposals.docx', '/export/proposals.pptx', '/export/proposals.xlsx'):
                self.require_user()
                self.export_proposals(path, parsed, self.read_json())
                return
            if path == "/auth/login":
                body = self.read_json()
                result = auth.login(
                    body.get("workNo"),
                    body.get("password"),
                    self.client_ip,
                    self.headers.get("User-Agent", ""),
                )
                self.send_json(200, {"ok": True, **result})
                return
            if path == "/auth/logout":
                self.read_body(1024)
                auth.logout(self.bearer_token())
                self.send_json(200, {"ok": True})
                return
            if path == "/auth/password":
                user = self.require_user()
                body = self.read_json()
                result = auth.change_password(
                    user["workNo"],
                    body.get("oldPassword"),
                    body.get("newPassword"),
                    keep_token=self.bearer_token(),
                )
                self.send_json(200, {"ok": True, **result, "user": auth.describe_user(user["workNo"])})
                return
            if path == "/auth/reset":
                operator = self.require_admin()
                body = self.read_json()
                target = str(body.get("workNo") or "").strip()
                if not target:
                    raise ValueError("缺少工号")
                if target == operator["workNo"]:
                    raise ValueError("不能重置自己的密码，请使用修改密码")
                result = auth.reset_password(target)
                self.send_json(200, {"ok": True, **result})
                return
            if path == "/auth/sync":
                self.require_admin()
                self.read_body(1024)
                self.send_json(200, {"ok": True, **auth.sync_accounts()})
                return
            if path == "/upload":
                self.require_user()
                parts = parse_multipart(self.headers.get("Content-Type", ""), self.read_body())
                file_part = next((part for part in parts if part["name"] == "file"), None)
                if not file_part:
                    raise ValueError("未收到上传文件")
                kind = urllib.parse.parse_qs(parsed.query).get("kind", ["attachment"])[0]
                if kind not in {"photo", "attachment"}:
                    kind = "attachment"
                self.send_json(201, {"ok": True, "file": store_upload(file_part, kind)})
                return
            if path == "/import/personnel/preview":
                self.require_admin()
                parts = parse_multipart(self.headers.get("Content-Type", ""), self.read_body(8 * 1024 * 1024))
                file_part = next((part for part in parts if part["name"] == "file"), None)
                if not file_part:
                    raise ValueError("未收到 Excel 文件")
                filename = file_part.get("filename", "")
                if not filename.lower().endswith(".xlsx"):
                    raise ValueError("请上传 .xlsx 格式文件")
                rows = read_xlsx(file_part["data"])
                self.send_json(200, {"ok": True, "fileName": Path(filename).name, "count": len(rows), "rows": rows})
                return
            if path == "/proposals/delete":
                user = self.require_admin()
                body = self.read_json()
                ids = body.get("ids")
                if isinstance(body.get("id"), str):
                    ids = [body["id"]]
                if not isinstance(ids, list):
                    raise ValueError("缺少要删除的提案编号")
                with WRITE_LOCK:
                    states, _, _ = get_states()
                    kept, removed = proposal_rules.remove(states.get("heatstar_proposals") or [], ids, user)
                    set_state("heatstar_proposals", kept, self.client_ip, user.get("name") or user.get("workNo"))
                self.send_json(200, {"ok": True, "removed": removed, "remaining": len(kept)})
                return
            if path == "/proposals/save":
                user = self.require_user()
                body = self.read_json()
                item = body.get("proposal")
                if not isinstance(item, dict):
                    raise ValueError("缺少提案内容")
                record = save_proposal_atomic(user, item, self.client_ip)
                self.send_json(200, {"ok": True, "proposal": record})
                return
            if path in ("/numbers/six-source", "/numbers/proposal"):
                self.require_user()
                self.read_body(1024)
                kind = urllib.parse.parse_qs(parsed.query).get("type", ["six"])[0]
                if kind not in {"six", "lean", "tool", "opl"}:
                    raise ValueError("不支持的提交门类")
                if path == "/numbers/six-source":
                    kind = "six"
                self.send_json(201, {"ok": True, "number": reserve_proposal_number(kind, self.require_user())})
                return
            self.send_json(404, {"ok": False, "message": "接口不存在"})
        except Exception as error:
            self.handle_error(error)

    def handle_error(self, error: Exception) -> None:
        if isinstance(error, proposal_rules.ProposalError):
            self.send_json(error.status, {"ok": False, "message": str(error)})
            return
        if isinstance(error, auth.AuthError):
            self.send_json(error.status, {"ok": False, "auth": True, "message": str(error)})
            return
        if isinstance(error, (ValueError, zipfile.BadZipFile)):
            self.send_json(400, {"ok": False, "message": str(error) or "请求参数错误"})
            return
        traceback.print_exc()
        self.send_json(500, {"ok": False, "message": "服务器内部错误"})


def main() -> None:
    parser = argparse.ArgumentParser(description="HeatStar shared API")
    parser.add_argument("--host", default=os.environ.get("HEATSTAR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HEATSTAR_PORT", "8765")))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    initialize_database()
    if args.check:
        states, _, revision = get_states()
        print(json.dumps({"ok": True, "states": len(states), "revision": revision, "db": str(DB_PATH)}, ensure_ascii=False))
        return
    server = ThreadingHTTPServer((args.host, args.port), HeatStarHandler)
    print(f"HeatStar API {APP_VERSION} listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
