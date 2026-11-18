"""Сессионный вход для проверочных скриптов, без обхода серверной авторизации."""
import getpass
import os
import secrets
from fastapi.testclient import TestClient

from .auth import COOKIE


def login_client(client):
    """Вход в рабочий экземпляр; пароль запрашивается, если не передан окружением."""
    username = os.getenv("ATLAS_LOGIN_USER", "admin")
    password = os.getenv("ATLAS_LOGIN_PASSWORD") or getpass.getpass("Пароль Atlas: ")
    result = client.post("/auth/login", headers={"X-Atlas-Request": "1"},
                         json={"username": username, "password": password})
    result.raise_for_status()
    session = client.get("/session").json()
    if session["must_change_password"]:
        raise RuntimeError("Сначала смените временный пароль через веб-панель")
    client.headers["X-CSRF-Token"] = session["csrf_token"]


class VerificationClient(TestClient):
    """Создаёт персональные тестовые сессии только в изолированной базе скрипта."""
    def __enter__(self):
        super().__enter__()
        self.sessions = {}
        password = os.environ["ADMIN_PASSWORD"]
        self._signin("admin", password)
        result = super().request("POST", "/admin/users", headers=self.sessions["admin"],
                                 json={"username": "verification_staff", "display_name": "Тестовый сотрудник"})
        result.raise_for_status()
        self._signin("verification_staff", result.json()["temporary_password"])
        self.sessions["staff"] = self.sessions["verification_staff"]
        self.cookies.clear()
        return self

    def _signin(self, username, password):
        result = super().request("POST", "/auth/login", headers={"X-Atlas-Request": "1"},
                                 json={"username": username, "password": password})
        result.raise_for_status()
        s = super().request("GET", "/session").json()
        if s["must_change_password"]:
            changed = super().request("POST", "/auth/change-password", headers={"X-CSRF-Token":s["csrf_token"]},
                                      json={"current_password":password, "new_password":secrets.token_urlsafe(24)})
            changed.raise_for_status()
            s = super().request("GET", "/session").json()
        self.sessions[username] = {"Cookie":COOKIE+"="+self.cookies.get(COOKIE), "X-CSRF-Token":s["csrf_token"]}

    def request(self, *args, auth=None, headers=None, **kwargs):
        headers = dict(headers or {})
        if auth:
            headers.update(self.sessions[auth[0]])
        return super().request(*args, headers=headers, **kwargs)
