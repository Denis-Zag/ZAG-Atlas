"""Восстановление admin на отдельной базе без изменения рабочей установки."""
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, recover_admin as recovery, storage as st
from app.main import app


class AdminRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atlas-recovery-test-")
        self.old_data = st.DATA
        st.DATA = Path(self.temp.name)
        self.env_patch = patch.dict(os.environ, {"ADMIN_PASSWORD": "Initial-pass-123", "SEARCH_MODE": "lexical"})
        self.env_patch.start()
        self.hash_patch = patch("app.auth.ITERATIONS", 1000)
        self.hash_patch.start()
        st.initialize()
        auth.initialize()
        self.path = st.DATA / "atlas.sqlite3"
        with st.db() as con:
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (
                "anna", st.now(), "Анна", "staff", auth.password_hash("Staff-pass-123"), 0, 1))
            now = time.time()
            for name, token in (("admin", "old-admin-token"), ("anna", "staff-token")):
                con.execute("INSERT INTO sessions VALUES(?,?,?,?)", (auth.digest(token), name, now, now))
            for key in ("user:admin", "user:anna", "ip:127.0.0.1"):
                con.execute("INSERT INTO login_failures VALUES(?,?)", (auth.digest(key), now))
            con.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)", (
                "doc-1", st.now(), "Инструкция", "Учебный текст", "admin", None, "text", "[]"))
            con.execute("INSERT INTO snippets VALUES(?,?,?,?)", ("snippet-1", st.now(), "doc-1", "Учебный текст"))
            con.execute("INSERT INTO qa_runs VALUES(?,?,?,?,?,?,?,?,?)", (
                "qa-1", st.now(), "Вопрос", "Ответ", "[]", 1, "Нет данных", "low", "anna"))

    def tearDown(self):
        st.DATA = self.old_data
        self.hash_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    def admin(self):
        return st.rows("SELECT * FROM users WHERE username='admin'")[0]

    def test_password_replaced_and_change_is_required(self):
        recovery.recover_admin("Temporary-$pass-123")
        row = self.admin()
        self.assertTrue(auth.password_matches("Temporary-$pass-123", row["password_hash"]))
        self.assertFalse(auth.password_matches("Initial-pass-123", row["password_hash"]))
        self.assertEqual(row["must_change_password"], 1)
        self.assertNotEqual(row["password_hash"], "Temporary-$pass-123")

    def test_only_admin_sessions_and_login_counter_are_cleared(self):
        result = recovery.recover_admin("Temporary-pass-123")
        self.assertEqual(result["sessions_revoked"], 1)
        self.assertEqual(result["login_failures_cleared"], 1)
        self.assertEqual([r["username"] for r in st.rows("SELECT * FROM sessions")], ["anna"])
        keys = {row["key"] for row in st.rows("SELECT * FROM login_failures")}
        self.assertEqual(keys, {auth.digest("user:anna"), auth.digest("ip:127.0.0.1")})

    def test_documents_history_and_staff_are_preserved(self):
        tables = ("documents", "snippets", "qa_runs")
        before = {table: st.rows("SELECT * FROM " + table) for table in tables}
        staff = st.rows("SELECT * FROM users WHERE username='anna'")
        recovery.recover_admin("Temporary-pass-123")
        self.assertEqual(before, {table: st.rows("SELECT * FROM " + table) for table in tables})
        self.assertEqual(staff, st.rows("SELECT * FROM users WHERE username='anna'"))

    def test_audit_does_not_contain_password_or_hash(self):
        password = "Private-temp-123"
        recovery.recover_admin(password)
        rows = st.rows("SELECT * FROM audit_runs WHERE action=?", (recovery.ACTION,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")
        text = json.dumps(rows[0])
        self.assertNotIn(password, text)
        self.assertNotIn(self.admin()["password_hash"], text)
        self.assertEqual(json.loads(rows[0]["input"])["actor"], "installation_owner")

    def test_invalid_password_does_not_change_data(self):
        before = self.admin()
        for password in ("short", "a" * 129, "1234567\n8", None):
            with self.subTest(kind=type(password).__name__):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.recover_admin(password)
        self.assertEqual(before, self.admin())
        self.assertFalse(st.rows("SELECT * FROM audit_runs"))

    def test_only_admin_access_changes_and_audit_has_no_secret(self):
        password = "Owner-temp-$789"
        with st.db() as con:
            now = time.time()
            con.execute("INSERT INTO sessions VALUES(?,?,?,?)", (auth.digest("second-admin-token"), "admin", now, now))
        st.audit("GET old_record", {}, {"kept": True}, "ok", None, 1)
        tables = ("documents", "snippets", "qa_runs")
        before = {name: st.rows("SELECT * FROM " + name) for name in tables}
        staff = st.rows("SELECT * FROM users WHERE username='anna'")
        staff_sessions = st.rows("SELECT * FROM sessions WHERE username='anna'")
        other_failures = st.rows("SELECT * FROM login_failures WHERE key!=?", (auth.digest("user:admin"),))
        old_audit = st.rows("SELECT * FROM audit_runs")[0]
        result = recovery.recover_admin(password)
        self.assertEqual(result["sessions_revoked"], 2)
        self.assertFalse(st.rows("SELECT * FROM sessions WHERE username='admin'"))
        self.assertFalse(st.rows("SELECT * FROM login_failures WHERE key=?", (auth.digest("user:admin"),)))
        self.assertEqual(staff, st.rows("SELECT * FROM users WHERE username='anna'"))
        self.assertEqual(staff_sessions, st.rows("SELECT * FROM sessions WHERE username='anna'"))
        self.assertEqual(other_failures, st.rows("SELECT * FROM login_failures WHERE key!=?", (auth.digest("user:admin"),)))
        for name in tables:
            self.assertEqual(before[name], st.rows("SELECT * FROM " + name))
        self.assertEqual(old_audit, st.rows("SELECT * FROM audit_runs WHERE id=?", (old_audit["id"],))[0])
        audit = st.rows("SELECT * FROM audit_runs WHERE action=?", (recovery.ACTION,))[0]
        encoded = json.dumps(audit)
        self.assertNotIn(password, encoded)
        self.assertNotIn(self.admin()["password_hash"], encoded)
        self.assertEqual(json.loads(audit["output"]), result)
        self.assertEqual(json.loads(audit["input"])["actor"], "installation_owner")

    def test_missing_database_is_not_created(self):
        path = st.DATA / "absent-folder" / "atlas.sqlite3"
        with self.assertRaises(recovery.RecoveryError):
            recovery.recover_admin("Temporary-pass-123", path=path)
        self.assertFalse(path.parent.exists())

    def test_missing_admin_is_not_created(self):
        with st.db() as con:
            con.execute("DELETE FROM sessions WHERE username='admin'")
            con.execute("DELETE FROM users WHERE role='admin'")
        with self.assertRaises(recovery.RecoveryError):
            recovery.recover_admin("Temporary-pass-123")
        self.assertFalse(st.rows("SELECT * FROM users WHERE role='admin'"))

    def test_audit_failure_rolls_back_password_and_sessions(self):
        before = self.admin()
        sessions = st.rows("SELECT * FROM sessions")
        failures = st.rows("SELECT * FROM login_failures")
        with st.db() as con:
            con.execute("CREATE TRIGGER reject_recovery BEFORE INSERT ON audit_runs "
                        "BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            recovery.recover_admin("Temporary-pass-123")
        self.assertEqual(before, self.admin())
        self.assertEqual(sessions, st.rows("SELECT * FROM sessions"))
        self.assertEqual(failures, st.rows("SELECT * FROM login_failures"))

    def test_disabled_admin_is_reactivated(self):
        with st.db() as con:
            con.execute("UPDATE users SET active=0 WHERE role='admin'")
        recovery.recover_admin("Temporary-pass-123")
        self.assertEqual(self.admin()["active"], 1)

    def test_real_login_requires_password_change_and_old_cookie_fails(self):
        with TestClient(app) as client:
            recovery.recover_admin("Temporary-pass-123")
            old = client.get("/session", headers={"Cookie": auth.COOKIE + "=old-admin-token"})
            self.assertEqual(old.status_code, 401)
            headers = {"X-Atlas-Request": "1"}
            self.assertEqual(client.post("/auth/login", headers=headers, json={
                "username": "admin", "password": "Initial-pass-123"}).status_code, 401)
            self.assertEqual(client.post("/auth/login", headers=headers, json={
                "username": "admin", "password": "Temporary-pass-123"}).status_code, 200)
            session = client.get("/session").json()
            self.assertTrue(session["must_change_password"])
            self.assertEqual(client.get("/kb/documents").status_code, 403)
            result = client.post("/auth/change-password", headers={"X-CSRF-Token": session["csrf_token"]},
                                 json={"current_password": "Temporary-pass-123", "new_password": "Final-pass-456"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(client.get("/kb/documents").status_code, 200)

    def cli(self, passwords=(), args=None, terminal=True):
        output, error = io.StringIO(), io.StringIO()
        with patch("app.recover_admin.sys.stdin.isatty", return_value=terminal), \
             patch("app.recover_admin.sys.stderr", error), \
             patch.object(error, "isatty", return_value=terminal), \
             patch("app.recover_admin.sys.stdout", output), \
             patch("app.recover_admin.getpass.getpass", side_effect=passwords) as reader:
            code = recovery.main([] if args is None else args)
        return code, output.getvalue() + error.getvalue(), reader.call_count

    def test_cli_requires_terminal_before_reading_password(self):
        before = self.admin()
        code, output, calls = self.cli(terminal=False)
        self.assertEqual(code, 2)
        self.assertEqual(calls, 0)
        self.assertEqual(before, self.admin())

    def test_cli_mismatch_is_safe(self):
        before = self.admin()
        code, output, calls = self.cli(("First-temp-123", "Other-temp-456"))
        self.assertEqual(code, 1)
        self.assertIn("не совпадают", output)
        self.assertNotIn("First-temp-123", output)
        self.assertEqual(before, self.admin())

    def test_cli_success_and_parameters_do_not_expose_secret(self):
        password = "Hidden-temp-123"
        code, output, calls = self.cli((password, password))
        self.assertEqual(code, 0)
        self.assertEqual(calls, 2)
        self.assertNotIn(password, output)
        code, output, calls = self.cli(args=["--password", password])
        self.assertEqual(code, 2)
        self.assertEqual(calls, 0)
        self.assertNotIn(password, output)

    def test_getpass_warning_does_not_fall_back_to_visible_input(self):
        def no_hidden_input(_):
            warnings.warn("test warning", recovery.getpass.GetPassWarning)
        before = self.admin()
        with patch("app.recover_admin.sys.stdin.isatty", return_value=True), \
             patch("app.recover_admin.sys.stderr", io.StringIO()) as error, \
             patch.object(error, "isatty", return_value=True), \
             patch("app.recover_admin.sys.stdout", io.StringIO()), \
             patch("app.recover_admin.getpass.getpass", side_effect=no_hidden_input):
            self.assertEqual(recovery.main([]), 2)
        self.assertEqual(before, self.admin())


if __name__ == "__main__":
    unittest.main()
