"""Первый вход, персональные права и отзыв серверных сессий."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import auth, storage as st
from app.main import app


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = st.DATA
        st.DATA = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ADMIN_PASSWORD": "initial-admin-secret", "SEARCH_MODE": "lexical"})
        self.env.start()
        self.hash_patch = patch("app.auth.ITERATIONS", 1000)
        self.hash_patch.start()
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        st.DATA = self.old_data
        self.hash_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def login(self, username="admin", password="initial-admin-secret"):
        r = self.client.post("/auth/login", headers={"X-Atlas-Request": "1"},
                             json={"username": username, "password": password})
        if r.status_code == 200:
            self.headers = {"X-CSRF-Token": self.client.get("/session").json()["csrf_token"]}
        return r

    def change(self, old="initial-admin-secret", new="new-admin-secret-123"):
        r = self.client.post("/auth/change-password", headers=self.headers,
                             json={"current_password": old, "new_password": new})
        if r.status_code == 200:
            self.headers = {"X-CSRF-Token": self.client.get("/session").json()["csrf_token"]}
        return r

    def ready(self):
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.change().status_code, 200)

    def create(self, name="anna"):
        r = self.client.post("/admin/users", headers=self.headers,
                             json={"username": name, "display_name": "Анна"})
        self.assertEqual(r.status_code, 200)
        return r.json()["temporary_password"]

    def test_first_login_and_role_escalation(self):
        self.login()
        self.assertTrue(self.client.get("/session").json()["must_change_password"])
        self.assertEqual(self.client.get("/kb/documents").status_code, 403)
        self.assertEqual(self.client.post("/admin/users", headers=self.headers,
                         json={"username":"anna","display_name":"Анна"}).status_code,403)
        self.change()
        self.assertEqual(self.client.get("/kb/documents").status_code,200)
        self.assertEqual(self.client.post("/admin/users", headers=self.headers,
                         json={"username":"anna","display_name":"Анна","role":"admin"}).status_code,422)

    def test_staff_first_password_and_rights(self):
        self.ready()
        temporary=self.create()
        self.login("anna",temporary)
        self.assertEqual(self.client.get("/kb/history").status_code,403)
        self.assertEqual(self.change(temporary,"anna-personal-secret").status_code,200)
        self.assertEqual(self.client.get("/kb/documents").status_code,200)
        self.assertEqual(self.client.get("/admin/users").status_code,403)
        self.assertEqual(self.client.get("/audit/runs").status_code,403)
        self.assertEqual(self.client.put("/settings",headers=self.headers,json=st.settings()).status_code,403)
        self.assertEqual(self.login("anna",temporary).status_code,401)

    def test_logout_revokes_copied_cookie(self):
        self.ready()
        token=self.client.cookies.get(auth.COOKIE)
        self.assertEqual(self.client.post("/auth/logout",headers=self.headers,json={}).status_code,200)
        self.assertEqual(self.client.get("/session",headers={"Cookie":auth.COOKIE+"="+token}).status_code,401)
        self.assertEqual(self.client.get("/kb/documents").status_code,401)

    def test_reset_and_block_revoke_sessions(self):
        self.ready()
        admin_headers={**self.headers,"Cookie":auth.COOKIE+"="+self.client.cookies.get(auth.COOKIE)}
        temporary=self.create()
        self.login("anna",temporary)
        self.change(temporary,"anna-personal-secret")
        staff_cookie=auth.COOKIE+"="+self.client.cookies.get(auth.COOKIE)
        r=self.client.post("/admin/users/anna/reset-password",headers=admin_headers,json={})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get("/session",headers={"Cookie":staff_cookie}).status_code,401)
        self.assertEqual(self.login("anna","anna-personal-secret").status_code,401)
        temporary=r.json()["temporary_password"]
        self.login("anna",temporary)
        self.assertEqual(self.client.put("/admin/users/anna",headers=admin_headers,json={"active":False}).status_code,200)
        self.assertEqual(self.client.get("/session").status_code,401)
        self.assertEqual(self.login("anna",temporary).status_code,401)
        self.assertEqual(self.client.put("/admin/users/admin",headers=admin_headers,json={"active":False}).status_code,404)
        self.assertEqual(self.client.put("/admin/users/anna",headers=admin_headers,json={"active":True}).status_code,200)
        self.assertEqual(self.login("anna",temporary).status_code,200)

    def test_password_change_revokes_other_sessions_and_survives_bootstrap(self):
        self.ready()
        old_cookie=auth.COOKIE+"="+self.client.cookies.get(auth.COOKIE)
        self.login(password="new-admin-secret-123")
        self.change("new-admin-secret-123","another-admin-secret-456")
        self.assertEqual(self.client.get("/session",headers={"Cookie":old_cookie}).status_code,401)
        auth.initialize()
        self.assertEqual(self.login().status_code,401)
        self.assertEqual(self.login(password="another-admin-secret-456").status_code,200)
        self.assertEqual(len(st.rows("SELECT * FROM users WHERE role='admin'")),1)

    def test_idle_absolute_expiry_and_settings(self):
        self.ready()
        cfg=st.settings();cfg.update(company_name="Тест",SESSION_IDLE_MINUTES=1,SESSION_MAX_HOURS=1)
        self.assertEqual(self.client.put("/settings",headers=self.headers,json=cfg).status_code,200)
        with st.db() as con:
            con.execute("UPDATE sessions SET last_seen=?",(time.time()-61,))
        self.assertEqual(self.client.get("/session").status_code,401)
        self.assertFalse(st.rows("SELECT * FROM sessions"))
        self.login(password="new-admin-secret-123")
        with st.db() as con:
            con.execute("UPDATE sessions SET created_at=?,last_seen=?",(time.time()-3601,time.time()))
        self.assertEqual(self.client.get("/session").status_code,401)
        self.login(password="new-admin-secret-123")
        cfg.update(SESSION_IDLE_MINUTES=61)
        self.assertEqual(self.client.put("/settings",headers=self.headers,json=cfg).status_code,422)

    def test_csrf_cookie_and_no_basic_bypass(self):
        response=self.login()
        cookie=response.headers['set-cookie'].lower()
        self.assertIn('httponly',cookie);self.assertIn('samesite=strict',cookie)
        self.assertEqual(self.client.post('/auth/logout',json={}).status_code,403)
        self.assertEqual(self.client.post('/auth/logout',headers={**self.headers,'Origin':'https://evil.test'},json={}).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/kb/documents',auth=('admin','initial-admin-secret')).status_code,401)

    def test_secrets_and_hashes_do_not_leak(self):
        self.ready();temporary=self.create()
        self.assertNotIn('password_hash',str(self.client.get('/admin/users').json()))
        stored=st.rows("SELECT * FROM users WHERE username='anna'")[0]
        self.assertNotEqual(stored['password_hash'],temporary)
        self.assertNotEqual(st.rows('SELECT * FROM sessions')[0]['token_hash'],self.client.cookies.get(auth.COOKIE))
        audits=str(st.rows('SELECT input,output FROM audit_runs'))
        for secret in (temporary,'initial-admin-secret','new-admin-secret-123',self.client.cookies.get(auth.COOKIE)):
            self.assertNotIn(secret,audits)

    def test_personal_actor_and_multiple_staff(self):
        self.ready();first=self.create('anna');self.create('boris')
        self.assertEqual(self.client.post('/admin/users',headers=self.headers,json={'username':'ANNA','display_name':'Другая'}).status_code,409)
        self.login('anna',first);self.change(first,'anna-personal-secret')
        self.client.post('/kb/ask',headers=self.headers,json={'question':'Неизвестный вопрос'})
        self.assertEqual(st.rows('SELECT created_by FROM qa_runs')[0]['created_by'],'anna')

    def test_attempt_limit(self):
        for _ in range(10):self.assertEqual(self.login('nobody','wrong').status_code,401)
        self.assertEqual(self.login('nobody','wrong').status_code,429)

    def test_production_hash_parameters(self):
        with patch('app.auth.ITERATIONS',600000):
            encoded=auth.password_hash('production-test-password')
        self.assertTrue(encoded.startswith('pbkdf2_sha256$600000$'))
        self.assertTrue(auth.password_matches('production-test-password',encoded))
        self.assertFalse(auth.password_matches('incorrect',encoded))

    def test_verification_client_uses_real_sessions(self):
        from app.verification import VerificationClient
        with VerificationClient(app) as client:
            self.assertEqual(client.get('/kb/documents',auth=('admin','unused')).status_code,200)
            self.assertEqual(client.get('/session',auth=('staff','unused')).json()['username'],'verification_staff')
            self.assertEqual(client.get('/admin/users',auth=('staff','unused')).status_code,403)

    def test_new_password_minimum_eight_characters(self):
        self.ready()
        rejected=self.change('new-admin-secret-123','short77')
        self.assertEqual(rejected.status_code,422)
        self.assertIn('от 8 до 128',rejected.json()['detail'])
        self.assertEqual(self.change('new-admin-secret-123','eight888').status_code,200)
        self.assertEqual(self.login(password='eight888').status_code,200)
