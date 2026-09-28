#!/usr/bin/env python3
"""热创星账号与登录。

仅使用标准库：账号来源于人员名单（工号即账号），PBKDF2-SHA256 口令散列，
不透明会话令牌持久化在同一个 SQLite 库中，登录失败限速，管理员可重置口令。
本模块不 import app.py，由 app.py 在启动时调用 configure() 注入依赖。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import threading
import organization


DEFAULT_PASSWORD = os.environ.get("HEATSTAR_DEFAULT_PASSWORD", "").strip()
SUPER_ADMIN_WORK_NO = "admin"
SUPER_ADMIN_NAME = "系统管理员"
SUPER_ADMIN_DEFAULT_PASSWORD = os.environ.get("HEATSTAR_SUPER_ADMIN_PASSWORD", "").strip()
SESSION_TTL_DAYS = 30
MAX_SESSIONS_PER_ACCOUNT = 5
MAX_FAILED_ATTEMPTS = 8
LOCK_MINUTES = 10
PBKDF2_ROUNDS = 120_000
MIN_PASSWORD_LENGTH = 6

ADMIN_ROLES = {"厂长", "管理员", "系统管理员"}
REVIEW_LEVEL_BY_ROLE = {
    "厂长": "factory",
    "管理员": "factory",
    "系统管理员": "factory",
    "工段长": "",
    "班组长": "team",
    "副班组长": "team",
    "评审员": "reviewer",
}
REVIEW_LEVEL_BY_LEVEL = {"全厂": "", "工段": "", "部门": "", "评审员": "reviewer", "班组": "team", "无": ""}
BLOCKED_STATUS = {"已移除", "离职", "离岗", "停用", "禁用"}

# 只有管理员可以改写的配置类数据项。
ADMIN_ONLY_KEYS = {
    "heatstar_people",
    "heatstar_teams",
    "heatstar_departments",
    "heatstar_permissions",
    "heatstar_settings",
    "heatstar_categories",
    "heatstar_notices",
}

_CONFIG: dict = {"db_path": None, "states_reader": None}
_LOCK = threading.RLock()


class AuthError(Exception):
    """登录/口令相关的可展示错误。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def configure(db_path, states_reader) -> None:
    _CONFIG["db_path"] = str(db_path)
    _CONFIG["states_reader"] = states_reader


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(value: dt.datetime) -> str:
    return value.isoformat()


def _parse(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


@contextlib.contextmanager
def _connection():
    if not _CONFIG["db_path"]:
        raise RuntimeError("auth 模块未配置数据库路径")
    connection = sqlite3.connect(_CONFIG["db_path"], timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
    finally:
        connection.close()


def _new_salt() -> str:
    return secrets.token_hex(16)


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()


def _initial_password(is_super: bool = False) -> str:
    configured = SUPER_ADMIN_DEFAULT_PASSWORD if is_super else DEFAULT_PASSWORD
    if configured:
        return configured
    generated = secrets.token_urlsafe(18)
    if is_super:
        print(
            "[heatstar] HEATSTAR_SUPER_ADMIN_PASSWORD is not set; "
            f"generated one-time bootstrap password for admin: {generated}",
            file=sys.stderr,
        )
    return generated


def _people() -> list[dict]:
    reader = _CONFIG["states_reader"]
    if not reader:
        return []
    try:
        states, _, _ = reader()
    except Exception:
        return []
    people = states.get("heatstar_people")
    if not isinstance(people, list):
        return []
    return [item for item in people if isinstance(item, dict)]


def _work_no(person: dict) -> str:
    return str(person.get("workNo") or person.get("id") or "").strip()


def initialize() -> None:
    with _LOCK, _connection() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                work_no TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                must_change INTEGER NOT NULL DEFAULT 1,
                is_super INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '在岗',
                failed_count INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                work_no TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                client_ip TEXT,
                user_agent TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_work_no ON sessions(work_no);
            """
        )
        database.commit()
    ensure_super_admin()
    sync_accounts()
    purge_expired_sessions()


def ensure_super_admin() -> None:
    timestamp = _iso(_now())
    with _LOCK, _connection() as database:
        row = database.execute(
            "SELECT work_no FROM accounts WHERE work_no = ?", (SUPER_ADMIN_WORK_NO,)
        ).fetchone()
        if row:
            return
        salt = _new_salt()
        database.execute(
            """INSERT INTO accounts(work_no, name, password_hash, salt, must_change, is_super,
                                    status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, 1, '在岗', ?, ?)""",
            (
                SUPER_ADMIN_WORK_NO,
                SUPER_ADMIN_NAME,
                _hash(_initial_password(is_super=True), salt),
                salt,
                timestamp,
                timestamp,
            ),
        )
        database.commit()


def sync_accounts() -> dict:
    """把人员名单同步成登录账号：新人自动开通，名单里移除的人自动停用。"""
    people = _people()
    timestamp = _iso(_now())
    created = 0
    updated = 0
    disabled = 0
    with _LOCK, _connection() as database:
        rows = {row["work_no"]: row for row in database.execute("SELECT * FROM accounts").fetchall()}
        seen: set[str] = set()
        for person in people:
            work_no = _work_no(person)
            if not work_no or work_no == SUPER_ADMIN_WORK_NO:
                continue
            seen.add(work_no)
            name = str(person.get("name") or "").strip() or work_no
            status = str(person.get("status") or "在岗").strip() or "在岗"
            row = rows.get(work_no)
            if row is None:
                salt = _new_salt()
                database.execute(
                    """INSERT INTO accounts(work_no, name, password_hash, salt, must_change, is_super,
                                            status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?)""",
                    (work_no, name, _hash(_initial_password(), salt), salt, status, timestamp, timestamp),
                )
                created += 1
            elif row["name"] != name or row["status"] != status:
                database.execute(
                    "UPDATE accounts SET name = ?, status = ?, updated_at = ? WHERE work_no = ?",
                    (name, status, timestamp, work_no),
                )
                updated += 1
        for work_no, row in rows.items():
            if row["is_super"] or work_no in seen or row["status"] == "已移除":
                continue
            database.execute(
                "UPDATE accounts SET status = '已移除', updated_at = ? WHERE work_no = ?",
                (timestamp, work_no),
            )
            database.execute("DELETE FROM sessions WHERE work_no = ?", (work_no,))
            disabled += 1
        database.commit()
    return {"created": created, "updated": updated, "disabled": disabled, "total": len(seen)}


def purge_expired_sessions() -> int:
    now = _iso(_now())
    with _LOCK, _connection() as database:
        cursor = database.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        database.commit()
        return cursor.rowcount or 0


def describe_user(work_no: str) -> dict | None:
    work_no = str(work_no or "").strip()
    if not work_no:
        return None
    with _connection() as database:
        row = database.execute("SELECT * FROM accounts WHERE work_no = ?", (work_no,)).fetchone()
    if not row:
        return None

    if row["is_super"]:
        profile: dict = {
            "id": work_no,
            "name": row["name"],
            "role": "系统管理员",
            "level": "公司",
            "scope": "全公司",
            "team": "",
            "section": "公司级职能",
            "points": 0,
            "status": "在岗",
        }
    else:
        person = next((item for item in _people() if _work_no(item) == work_no), None)
        if not person:
            return None
        profile = dict(person)

    role = str(profile.get("role") or "员工").strip() or "员工"
    level = str(profile.get("level") or "").strip()
    # 后台显式配置的审核层级优先于岗位推断，否则在权限管理里撤销审核权会不生效。
    if level in REVIEW_LEVEL_BY_LEVEL:
        review_level = REVIEW_LEVEL_BY_LEVEL[level]
    else:
        review_level = REVIEW_LEVEL_BY_ROLE.get(role, "")
    assigned_roles = organization.configured_roles(profile)
    is_admin = bool(row["is_super"]) or role in ADMIN_ROLES or bool(profile.get("isAdmin")) or ('permissionRoles' in profile and '管理员' in assigned_roles)
    if level == '公司':
        review_level = 'company' if profile.get('permissionRole') == '审核者' or (not is_admin and profile.get('permissionRole') != '员工') else ''
    can_recommend = level == '精益专员' and profile.get('permissionRole') != '员工'
    if level == '精益专员':
        review_level = ''
    if profile.get('permissionRole') == '员工':
        review_level = ''
    states, _, _ = _CONFIG['states_reader']()
    did = organization.department_id(profile)
    department_scopes = profile.get('departmentScopes') or []
    if (review_level == 'reviewer' or can_recommend) and not department_scopes:
        named_scopes = organization.scopes(profile)
        department_scopes = [d['id'] for d in organization.departments(states) if d['name'] in named_scopes] or [did]
    if row['is_super'] or level == '公司':
        department_scopes = [d['id'] for d in organization.departments(states)]
    if row['is_super']:
        review_level = 'reviewer'
        department_scopes = [d['id'] for d in organization.departments(states)]
    access_level = 'factory' if level in ('全厂', '部门') or role in ADMIN_ROLES else ('section' if level == '工段' else review_level)
    if row['is_super'] or level == '公司':
        access_level = 'company'
    elif can_recommend:
        access_level = 'specialist'
    review_levels = [review_level] if review_level else []
    multi = {}
    if 'levels' in profile and not row['is_super']:
        levels = organization.configured_levels(profile)
        mapping = {'班组':'team', '评审员':'reviewer', '部门':'factory', '公司':'company', '精益专员':'specialist', '工段':'section'}
        grants = {}
        names = {d['id']:d['name'] for d in organization.departments(states)}
        for duty in levels:
            kind = mapping.get(duty)
            selected = (profile.get('levelScopes') or {}).get(duty, [])
            if not kind or not selected:
                continue
            grants[kind] = {'departmentId':did, 'scope':'、'.join(selected), 'team':profile.get('team'), 'section':profile.get('section')}
            if duty in ('评审员', '精益专员', '部门'):
                grants[kind]['departmentScopes'] = selected
                grants[kind]['scope'] = '、'.join(names.get(k, k) for k in selected)
            if duty == '公司':
                grants[kind]['scope'] = '全公司'
        if not any(r in assigned_roles for r in ('审核者','管理员')):
            grants = {}
        review_levels = [kind for kind in ('team','reviewer','company') if kind in grants and
                         (kind != 'company' or '审核者' in assigned_roles or not is_admin)]
        review_level = review_levels[0] if review_levels else ''
        can_recommend = 'specialist' in grants
        access_level = next((kind for kind in ('company','factory','reviewer','specialist','section','team') if kind in grants), '')
        department_scopes = list(dict.fromkeys(d for grant in grants.values() for d in grant.get('departmentScopes', [])))
        level = '、'.join(levels) or '无'
        profile['scope'] = '；'.join(f"{duty}：{grants[mapping[duty]]['scope']}" for duty in levels if mapping.get(duty) in grants) or '本人'
        multi = {'scopeByLevel':grants, 'levels':levels, 'levelScopes':profile.get('levelScopes', {}), 'permissionRoles':assigned_roles}
    return {
        "workNo": work_no,
        "id": str(profile.get("id") or work_no),
        "name": str(profile.get("name") or row["name"]),
        "team": str(profile.get("team") or ""),
        "section": str(profile.get("section") or ""),
        "departmentId": did,
        "department": organization.department_name(profile, states),
        "departmentScopes": department_scopes,
        "accessLevel": access_level or '',
        "role": role,
        "level": level,
        "scope": str(profile.get("scope") or ""),
        "points": profile.get("points") or 0,
        "rank": profile.get("rank") or 0,
        "status": str(profile.get("status") or "在岗"),
        "isAdmin": is_admin,
        "isSuper": bool(row["is_super"]),
        "reviewLevel": review_level or "",
        "reviewLevels": review_levels,
        **multi,
        "canReview": bool(review_level),
        "canRecommend": bool(can_recommend or row['is_super']),
        "mustChangePassword": bool(row["must_change"]),
        "lastLoginAt": row["last_login_at"] or "",
    }


def _create_session(work_no: str, client_ip: str = "", user_agent: str = "") -> str:
    token = secrets.token_urlsafe(36)
    now = _now()
    with _LOCK, _connection() as database:
        database.execute(
            """INSERT INTO sessions(token, work_no, created_at, expires_at, last_seen_at, client_ip, user_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                token,
                work_no,
                _iso(now),
                _iso(now + dt.timedelta(days=SESSION_TTL_DAYS)),
                _iso(now),
                (client_ip or "")[:64],
                (user_agent or "")[:180],
            ),
        )
        # 换设备/换浏览器登录会不断累积会话，只保留最近几个。
        database.execute(
            """DELETE FROM sessions WHERE work_no = ? AND token NOT IN (
                   SELECT token FROM sessions WHERE work_no = ? ORDER BY created_at DESC LIMIT ?
               )""",
            (work_no, work_no, MAX_SESSIONS_PER_ACCOUNT),
        )
        database.commit()
    return token


def login(work_no: str, password: str, client_ip: str = "", user_agent: str = "") -> dict:
    work_no = str(work_no or "").strip()
    password = str(password or "")
    if not work_no or not password:
        raise AuthError("请输入工号和密码")

    with _connection() as database:
        row = database.execute("SELECT * FROM accounts WHERE work_no = ?", (work_no,)).fetchone()
    if row is None:
        # 名单刚导入、账号还没同步过：补同步一次再找。
        sync_accounts()
        with _connection() as database:
            row = database.execute("SELECT * FROM accounts WHERE work_no = ?", (work_no,)).fetchone()
    if row is None:
        raise AuthError("工号或密码不正确", 401)

    locked_until = _parse(row["locked_until"])
    if locked_until and locked_until > _now():
        minutes = max(1, int((locked_until - _now()).total_seconds() // 60) + 1)
        raise AuthError("密码错误次数过多，请 %d 分钟后再试" % minutes, 429)

    if str(row["status"]).strip() in BLOCKED_STATUS:
        raise AuthError("该账号已停用，请联系管理员", 403)

    if not hmac.compare_digest(_hash(password, row["salt"]), row["password_hash"]):
        failed = int(row["failed_count"]) + 1
        lock_value = None
        if failed >= MAX_FAILED_ATTEMPTS:
            lock_value = _iso(_now() + dt.timedelta(minutes=LOCK_MINUTES))
            failed = 0
        with _LOCK, _connection() as database:
            database.execute(
                "UPDATE accounts SET failed_count = ?, locked_until = ?, updated_at = ? WHERE work_no = ?",
                (failed, lock_value, _iso(_now()), work_no),
            )
            database.commit()
        if lock_value:
            raise AuthError("密码错误次数过多，账号已锁定 %d 分钟" % LOCK_MINUTES, 429)
        raise AuthError("工号或密码不正确，还可尝试 %d 次" % (MAX_FAILED_ATTEMPTS - failed), 401)

    user = describe_user(work_no)
    if not user:
        raise AuthError("该工号不在当前人员名单中，请联系管理员", 403)

    timestamp = _iso(_now())
    with _LOCK, _connection() as database:
        database.execute(
            "UPDATE accounts SET failed_count = 0, locked_until = NULL, last_login_at = ?, updated_at = ? WHERE work_no = ?",
            (timestamp, timestamp, work_no),
        )
        database.commit()
    user["lastLoginAt"] = timestamp
    token = _create_session(work_no, client_ip, user_agent)
    return {"token": token, "user": user, "expiresInDays": SESSION_TTL_DAYS}


def resolve_token(token: str) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    now = _now()
    with _connection() as database:
        row = database.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    expires = _parse(row["expires_at"])
    if not expires or expires <= now:
        with _LOCK, _connection() as database:
            database.execute("DELETE FROM sessions WHERE token = ?", (token,))
            database.commit()
        return None

    user = describe_user(row["work_no"])
    if not user or str(user.get("status") or "").strip() in BLOCKED_STATUS:
        with _LOCK, _connection() as database:
            database.execute("DELETE FROM sessions WHERE token = ?", (token,))
            database.commit()
        return None

    last_seen = _parse(row["last_seen_at"])
    if not last_seen or (now - last_seen).total_seconds() > 3600:
        with _LOCK, _connection() as database:
            database.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token = ?",
                (_iso(now), _iso(now + dt.timedelta(days=SESSION_TTL_DAYS)), token),
            )
            database.commit()
    return user


def logout(token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    with _LOCK, _connection() as database:
        database.execute("DELETE FROM sessions WHERE token = ?", (token,))
        database.commit()


def _validate_new_password(password: str) -> str:
    password = str(password or "").strip()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError("新密码至少 %d 位" % MIN_PASSWORD_LENGTH)
    if len(password) > 64:
        raise AuthError("新密码不能超过 64 位")
    return password


def change_password(work_no: str, old_password: str, new_password: str, keep_token: str = "") -> dict:
    with _connection() as database:
        row = database.execute("SELECT * FROM accounts WHERE work_no = ?", (work_no,)).fetchone()
    if not row:
        raise AuthError("账号不存在", 404)
    if not hmac.compare_digest(_hash(old_password or "", row["salt"]), row["password_hash"]):
        raise AuthError("当前密码不正确", 401)
    password = _validate_new_password(new_password)
    if hmac.compare_digest(_hash(password, row["salt"]), row["password_hash"]):
        raise AuthError("新密码不能与当前密码相同")

    salt = _new_salt()
    timestamp = _iso(_now())
    with _LOCK, _connection() as database:
        database.execute(
            "UPDATE accounts SET password_hash = ?, salt = ?, must_change = 0, failed_count = 0, locked_until = NULL, updated_at = ? WHERE work_no = ?",
            (_hash(password, salt), salt, timestamp, work_no),
        )
        # 改密后踢掉其它设备，保留当前这台。
        if keep_token:
            database.execute(
                "DELETE FROM sessions WHERE work_no = ? AND token <> ?", (work_no, keep_token)
            )
        else:
            database.execute("DELETE FROM sessions WHERE work_no = ?", (work_no,))
        database.commit()
    return {"workNo": work_no, "changedAt": timestamp}


def reset_password(work_no: str) -> dict:
    work_no = str(work_no or "").strip()
    with _connection() as database:
        row = database.execute("SELECT * FROM accounts WHERE work_no = ?", (work_no,)).fetchone()
    if not row:
        sync_accounts()
        with _connection() as database:
            row = database.execute("SELECT * FROM accounts WHERE work_no = ?", (work_no,)).fetchone()
    if not row:
        raise AuthError("账号不存在", 404)

    password = _initial_password(is_super=bool(row["is_super"]))
    salt = _new_salt()
    timestamp = _iso(_now())
    with _LOCK, _connection() as database:
        database.execute(
            "UPDATE accounts SET password_hash = ?, salt = ?, must_change = 1, failed_count = 0, locked_until = NULL, updated_at = ? WHERE work_no = ?",
            (_hash(password, salt), salt, timestamp, work_no),
        )
        database.execute("DELETE FROM sessions WHERE work_no = ?", (work_no,))
        database.commit()
    return {"workNo": work_no, "name": row["name"], "password": password, "resetAt": timestamp}


def list_accounts() -> list[dict]:
    with _connection() as database:
        rows = database.execute(
            """SELECT work_no, name, status, must_change, is_super, last_login_at, locked_until
               FROM accounts ORDER BY is_super DESC, work_no"""
        ).fetchall()
        active = {
            item["work_no"]: item["count"]
            for item in database.execute(
                "SELECT work_no, COUNT(*) AS count FROM sessions WHERE expires_at > ? GROUP BY work_no",
                (_iso(_now()),),
            ).fetchall()
        }
    now = _now()
    accounts = []
    for row in rows:
        locked_until = _parse(row["locked_until"])
        accounts.append(
            {
                "workNo": row["work_no"],
                "name": row["name"],
                "status": row["status"],
                "initialPassword": bool(row["must_change"]),
                "isSuper": bool(row["is_super"]),
                "lastLoginAt": row["last_login_at"] or "",
                "locked": bool(locked_until and locked_until > now),
                "onlineSessions": active.get(row["work_no"], 0),
            }
        )
    return accounts


def can_write_key(user: dict | None, key: str) -> bool:
    if not user:
        return False
    if key in ADMIN_ONLY_KEYS:
        return bool(user.get("isAdmin"))
    return True
