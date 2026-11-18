"""Учётные записи, серверные сессии и управление сотрудниками."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from . import storage as st

router = APIRouter()
COOKIE = "atlas_session"
ITERATIONS = 600000
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoginInput(Input):
    username: str
    password: str


class PasswordInput(Input):
    current_password: str
    new_password: str


class StaffInput(Input):
    username: str
    display_name: str


class ActiveInput(Input):
    active: bool


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return "pbkdf2_sha256$" + str(ITERATIONS) + "$" + salt + "$" + value.hex()


def password_matches(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or len(password) > 128:
            return False
        value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError):
        return False


def initialize():
    with st.db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            username TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','staff')),
            password_hash TEXT NOT NULL, must_change_password INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1);
        CREATE UNIQUE INDEX IF NOT EXISTS one_admin ON users(role) WHERE role='admin';
        CREATE TABLE IF NOT EXISTS sessions(
            token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
            created_at REAL NOT NULL, last_seen REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS sessions_user ON sessions(username);
        CREATE TABLE IF NOT EXISTS login_failures(key TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS login_failures_key ON login_failures(key,created_at);
        """)
        if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            password = os.getenv("ADMIN_PASSWORD", "")
            if not password:
                raise RuntimeError("Задайте ADMIN_PASSWORD для первоначального создания администратора")
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",
                        ("admin", st.now(), "Администратор", "admin", password_hash(password), 1, 1))


def csrf(token):
    return hmac.new(token.encode(), b"atlas-csrf", hashlib.sha256).hexdigest()


def public_user(row):
    return {key: row[key] for key in ("username", "display_name", "role", "created_at") } | {
        "must_change_password": bool(row["must_change_password"]), "active": bool(row["active"])}


def issue(response, con, username):
    token = secrets.token_urlsafe(32)
    now = time.time()
    cfg = st.settings()
    con.execute("DELETE FROM sessions WHERE created_at<=? OR last_seen<=?",
                (now-cfg["SESSION_MAX_HOURS"]*3600, now-cfg["SESSION_IDLE_MINUTES"]*60))
    con.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest(token), username, now, now))
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/",
                        secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true")


def current(request: Request):
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 128:
        raise HTTPException(401, "Войдите в систему")
    now, cfg = time.time(), st.settings()
    with st.db() as con:
        row = con.execute("SELECT users.*,sessions.created_at AS session_created, sessions.last_seen "
                          "FROM sessions JOIN users USING(username) WHERE token_hash=?", (digest(token),)).fetchone()
        if not row or not row["active"]:
            raise HTTPException(401, "Сессия завершена. Войдите снова")
        if now-row["last_seen"] >= cfg["SESSION_IDLE_MINUTES"]*60 or now-row["session_created"] >= cfg["SESSION_MAX_HOURS"]*3600:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))
            expired = True
        else:
            expired = False
            if request.method in MUTATING and not hmac.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), csrf(token)):
                raise HTTPException(403, "Проверка сессии не пройдена. Обновите страницу")
            con.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, digest(token)))
    if expired:
        raise HTTPException(401, "Сессия истекла. Войдите снова")
    principal = dict(row)
    request.state.principal = principal
    request.state.actor = principal["username"]
    return principal


def user(request: Request, principal=Depends(current)):
    if principal["must_change_password"]:
        raise HTTPException(403, "Сначала измените временный пароль")
    return principal["username"]


def admin(request: Request, actor=Depends(user)):
    if request.state.principal["role"] != "admin":
        raise HTTPException(403, "Действие доступно администратору")
    return actor


@router.post("/auth/login")
def login(payload: LoginInput, request: Request, response: Response):
    if request.headers.get("X-Atlas-Request") != "1":
        raise HTTPException(403, "Обновите страницу входа")
    if len(payload.username) > 80 or len(payload.password) > 128:
        raise HTTPException(422, "Слишком длинный логин или пароль")
    username = payload.username.strip().lower()
    ip_key = digest("ip:" + (request.client.host if request.client else "unknown"))
    name_key = digest("user:" + username)
    now = time.time()
    with st.db() as con:
        con.execute("DELETE FROM login_failures WHERE created_at<?", (now-900,))
        limited = any(con.execute("SELECT COUNT(*) FROM login_failures WHERE key=?", (key,)).fetchone()[0] >= maximum
                      for key, maximum in ((ip_key, 30), (name_key, 10)))
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if limited:
        raise HTTPException(429, "Слишком много попыток. Повторите через 15 минут")
    # Одинаковая дорогая проверка и сообщение для неизвестного/заблокированного логина.
    dummy = "pbkdf2_sha256$" + str(ITERATIONS) + "$" + "00"*16 + "$" + "00"*32
    valid = password_matches(payload.password, row["password_hash"] if row else dummy)
    if not row or not row["active"] or not valid:
        with st.db() as con:
            con.executemany("INSERT INTO login_failures VALUES(?,?)", [(ip_key, now), (name_key, now)])
        raise HTTPException(401, "Неверный логин или пароль")
    with st.db() as con:
        # Повторная проверка не позволяет сбросу пароля, выполненному параллельно,
        # выдать сессию по уже устаревшему хешу.
        latest = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not latest["active"] or latest["password_hash"] != row["password_hash"]:
            raise HTTPException(401, "Неверный логин или пароль")
        con.execute("DELETE FROM login_failures WHERE key=?", (name_key,))
        issue(response, con, username)
    request.state.actor = username
    return public_user(latest)


@router.get("/session")
def session(request: Request, principal=Depends(current)):
    return {**public_user(principal), "csrf_token": csrf(request.cookies[COOKIE]),
            "session_expires_at": principal["session_created"] + st.settings()["SESSION_MAX_HOURS"]*3600}


@router.post("/auth/logout")
def logout(request: Request, response: Response, principal=Depends(current)):
    with st.db() as con:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies[COOKIE]),))
    response.delete_cookie(COOKIE, path="/")
    return {"status": "ok"}


@router.post("/auth/change-password")
def change_password(payload: PasswordInput, request: Request, response: Response, principal=Depends(current)):
    if not 8 <= len(payload.new_password) <= 128:
        raise HTTPException(422, "Новый пароль: от 8 до 128 символов")
    if payload.current_password == payload.new_password:
        raise HTTPException(422, "Новый пароль должен отличаться от временного или текущего")
    if not password_matches(payload.current_password, principal["password_hash"]):
        raise HTTPException(400, "Текущий пароль неверен")
    encoded = password_hash(payload.new_password)
    with st.db() as con:
        updated = con.execute("UPDATE users SET password_hash=?,must_change_password=0 "
                              "WHERE username=? AND password_hash=? AND active=1",
                              (encoded, principal["username"], principal["password_hash"])).rowcount
        if not updated:
            raise HTTPException(409, "Учётная запись изменилась. Войдите снова")
        con.execute("DELETE FROM sessions WHERE username=?", (principal["username"],))
        issue(response, con, principal["username"])
    return {"status": "ok"}


@router.get("/admin/users")
def users(actor=Depends(admin)):
    return [public_user(row) for row in st.rows("SELECT * FROM users ORDER BY created_at")]


def staff_row(con, username):
    row = con.execute("SELECT * FROM users WHERE username=? AND role='staff'", (username,)).fetchone()
    if not row:
        raise HTTPException(404, "Сотрудник не найден")
    return row


@router.post("/admin/users")
def create_staff(payload: StaffInput, request: Request, actor=Depends(admin)):
    username, name = payload.username.strip().lower(), payload.display_name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,39}", username) or username in {"admin", "staff"}:
        raise HTTPException(422, "Логин: 3–40 латинских букв, цифр, точек, дефисов; начните с буквы. admin/staff зарезервированы")
    if not name or len(name) > 100:
        raise HTTPException(422, "Имя: от 1 до 100 символов")
    temporary = secrets.token_urlsafe(18)
    try:
        with st.db() as con:
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",
                        (username, st.now(), name, "staff", password_hash(temporary), 1, 1))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Такой логин уже существует") from None
    request.state.result = {"username": username, "role": "staff", "must_change_password": True}
    return {**request.state.result, "temporary_password": temporary}


@router.put("/admin/users/{username}")
def set_active(username: str, payload: ActiveInput, actor=Depends(admin)):
    with st.db() as con:
        staff_row(con, username)
        con.execute("UPDATE users SET active=? WHERE username=?", (int(payload.active), username))
        con.execute("DELETE FROM sessions WHERE username=?", (username,))
    return {"status": "ok"}


@router.post("/admin/users/{username}/reset-password")
def reset_password(username: str, request: Request, actor=Depends(admin)):
    temporary = secrets.token_urlsafe(18)
    with st.db() as con:
        staff_row(con, username)
        con.execute("UPDATE users SET password_hash=?,must_change_password=1 WHERE username=?",
                    (password_hash(temporary), username))
        con.execute("DELETE FROM sessions WHERE username=?", (username,))
    request.state.result = {"username": username, "must_change_password": True}
    return {**request.state.result, "temporary_password": temporary}
