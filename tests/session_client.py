"""Явно выбранная тестовая сессия для прежних проверок API."""
from fastapi.testclient import TestClient
from app import auth

class SessionClient(TestClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tokens = {}

    def auth_headers(self, credentials):
        token, csrf = self.tokens.get(credentials, ("", ""))
        return {"Cookie": auth.COOKIE + "=" + token, "X-CSRF-Token": csrf}

    def request(self, *args, auth=None, headers=None, **kwargs):
        headers = dict(headers or {})
        if auth:
            headers.update(self.auth_headers(auth))
        else:
            headers.setdefault("Cookie", "")
        headers.setdefault("X-Atlas-Request", "1")
        response = super().request(*args, headers=headers, **kwargs)
        self.cookies.clear()
        return response
