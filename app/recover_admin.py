"""Восстановление единственного admin владельцем установки через терминал."""
import getpass
import json
import sqlite3
import sys
import time
import warnings
from pathlib import Path

from . import auth
from . import storage as st

ACTION = "CLI admin_password_recovery"


class RecoveryError(Exception):
    """Понятная ошибка восстановления без секретов в сообщении."""


def database_path():
    return (st.DATA / "atlas.sqlite3").resolve()


def open_existing(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise RecoveryError("Существующая база Atlas не найдена. Проверьте экземпляр и том данных.")
    # mode=rw исключает создание новой пустой базы при ошибке пути.
    con = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def admin_username(con):
    rows = con.execute("SELECT username FROM users WHERE role='admin'").fetchall()
    if len(rows) != 1:
        raise RecoveryError("В базе должен существовать ровно один администратор. Автоматическое создание отключено.")
    return rows[0]["username"]


def inspect_database(path):
    con = open_existing(path)
    try:
        username = admin_username(con)
        # Проверяем нужную схему до запроса нового пароля, ничего не создаём.
        con.execute("SELECT token_hash FROM sessions LIMIT 0")
        con.execute("SELECT key FROM login_failures LIMIT 0")
        con.execute("SELECT action, input, output, status, error, duration_ms FROM audit_runs LIMIT 0")
        return username
    finally:
        con.close()


def validate_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise RecoveryError("Временный пароль должен содержать от 8 до 128 символов.")
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise RecoveryError("Пароль не должен содержать управляющие символы.")


def recover_admin(password, *, path=None):
    """Атомарно меняет пароль, отзывает сессии и фиксирует безопасный аудит."""
    validate_password(password)
    started = time.perf_counter()
    encoded = auth.password_hash(password)
    con = open_existing(path if path is not None else database_path())
    try:
        con.execute("BEGIN IMMEDIATE")
        username = admin_username(con)
        con.execute(
            "UPDATE users SET password_hash=?, must_change_password=1, active=1 "
            "WHERE username=? AND role='admin'",
            (encoded, username),
        )
        revoked = con.execute("DELETE FROM sessions WHERE username=?", (username,)).rowcount
        cleared = con.execute(
            "DELETE FROM login_failures WHERE key=?", (auth.digest("user:" + username),)
        ).rowcount
        result = {"username": username, "must_change_password": True,
                  "sessions_revoked": revoked, "login_failures_cleared": cleared}
        # Секрет, его хеш и соль не включаются в аудит.
        con.execute(
            "INSERT INTO audit_runs(id,created_at,action,input,output,status,error,duration_ms) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (st.uid(), st.now(), ACTION,
             json.dumps({"actor": "installation_owner", "source": "local_cli", "target_user": username}),
             json.dumps(result), "ok", None,
             round((time.perf_counter() - started) * 1000)),
        )
        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args in (["--help"], ["-h"]):
        print("Восстановление admin: python -m app.recover_admin")
        print("Команда для терминала владельца установки при остановленном сервисе atlas.")
        print("Пароль вводится скрыто дважды; параметры командной строки не принимаются.")
        return 0
    if args:
        print("Параметры не поддерживаются. Запустите python -m app.recover_admin без параметров.", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Нужен интерактивный терминал со скрытым вводом. Не используйте перенаправление или Docker-флаг -T.",
              file=sys.stderr)
        return 2
    try:
        path = database_path()
        username = inspect_database(path)
        print(f"База: {path}")
        print(f"Восстановление пользователя {username}. Все его старые сессии будут завершены.")
        print("Перед процедурой сервис atlas должен быть остановлен по инструкции README.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Новый временный пароль (8–128 символов): ")
            validate_password(password)
            confirmation = getpass.getpass("Повторите временный пароль: ")
        if password != confirmation:
            raise RecoveryError("Пароли не совпадают. Восстановление не выполнено.")
        result = recover_admin(password, path=path)
        del password, confirmation
        print(f"Пароль пользователя {result['username']} восстановлен. Факт записан в аудит.")
        print("Запустите atlas, войдите с временным паролем и обязательно установите новый пароль.")
        print("Ограничения входа по IP сохраняются; при ответе 429 дождитесь окончания окна ограничения.")
        return 0
    except RecoveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("Не удалось обеспечить скрытый ввод. Используйте обычный интерактивный терминал.", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Восстановление отменено.", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as error:
        print(f"Не удалось завершить восстановление ({type(error).__name__}). Проверьте базу, права и доступность аудита.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
