#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Corruption injection test для PostgreSQL.

Зачем:
  03_integrity_check.py корректно отвечает 'всё ок' на свежей БД —
  потому что там нечего находить. Чтобы убедиться, что чекер реально
  ловит проблемы, этот скрипт намеренно портит relfilenode индекса,
  запускает чекер, потом восстанавливает из бэкапа.

Что делает:
  1. Спрашивает у postgres pg_relation_filepath(test_data_pkey).
  2. Останавливает postgres.
  3. Копирует relfilenode в /var/tmp/corruption_backup_<ts>/.
  4. Пишет 128 случайных байт во вторую страницу файла (page 1, offset 8192).
  5. Поднимает postgres.
  6. Запускает 03_integrity_check.py — ожидаем ненулевой exit code.
  7. Останавливает postgres, восстанавливает relfilenode из бэкапа.
  8. Поднимает postgres, запускает 03_integrity_check.py — ожидаем exit 0.

ТРЕБУЕТ явного подтверждения через переменную окружения:
  I_UNDERSTAND_THIS_DESTROYS_DATA=yes

Без неё скрипт ничего не делает и выходит с инструкцией.
"""
import os
import sys
import shutil
import subprocess
import time
from datetime import datetime

import psycopg2

DB_NAME = os.environ.get("CORRUPT_DB", "testdb")
TABLE_NAME = os.environ.get("CORRUPT_TABLE", "test_data")
# По умолчанию портим PK-индекс таблицы (его создаёт SERIAL PRIMARY KEY).
# Имя дефолтного PK-индекса в PG: <table>_pkey.
TARGET_INDEX = os.environ.get("CORRUPT_INDEX", f"{TABLE_NAME}_pkey")

PAGE_SIZE = 8192
INJECT_OFFSET = PAGE_SIZE + 128   # середина второй страницы
INJECT_BYTES = 128

SAFETY_VAR = "I_UNDERSTAND_THIS_DESTROYS_DATA"
INTEGRITY_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "03_integrity_check.py"
)


def run(argv, check=True):
    """Запуск без shell — аргументы списком, никакой интерполяции в шелл."""
    print("> " + " ".join(argv))
    return subprocess.run(argv, check=check)


def wait_pg_ready(timeout=30):
    res = subprocess.run(
        ["pg_isready", "-t", str(timeout), "-h", "/var/run/postgresql"]
    )
    if res.returncode != 0:
        raise RuntimeError(f"postgres не отвечает за {timeout} сек")


def get_connection():
    return psycopg2.connect(
        dbname=DB_NAME, user="postgres", host="/var/run/postgresql"
    )


def resolve_relfilepath(index_name):
    """Возвращает абсолютный путь к relfilenode индекса в data_directory."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            data_dir = cur.fetchone()[0]
            # pg_relation_filepath даёт относительный путь от data_directory.
            cur.execute("SELECT pg_relation_filepath(%s::regclass)", (index_name,))
            rel = cur.fetchone()[0]
    finally:
        conn.close()
    return os.path.join(data_dir, rel)


def ensure_safety():
    if os.environ.get(SAFETY_VAR) != "yes":
        print(
            f"❌ Скрипт ничего не сделал.\n"
            f"   Это деструктивный тест: он намеренно портит файл БД.\n"
            f"   Подтвердите осознанность запуска:\n\n"
            f"   sudo {SAFETY_VAR}=yes python3 04_corruption_test.py\n\n"
            f"   ЗАПУСКАТЬ ТОЛЬКО НА ТЕСТОВОЙ ВМ С ОДНОРАЗОВОЙ БД!"
        )
        sys.exit(1)
    if os.geteuid() != 0:
        print(f"❌ Запустите с sudo: sudo {SAFETY_VAR}=yes python3 04_corruption_test.py")
        sys.exit(1)
    if DB_NAME in ("postgres", "template0", "template1"):
        print(f"❌ Отказываюсь портить системную БД: {DB_NAME}")
        sys.exit(1)


def stop_postgres():
    run(["systemctl", "stop", "postgresql"])
    # systemctl stop возвращается синхронно, но процесс ещё может писать в файлы.
    # Маленькая пауза + явная проверка статуса — безопаснее, чем гонка.
    time.sleep(1)
    # is-active возвращает 3 для остановленного, не проверяем return code.
    subprocess.run(["systemctl", "is-active", "postgresql"], check=False)


def start_postgres():
    run(["systemctl", "start", "postgresql"])
    wait_pg_ready()


def backup_file(src, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, os.path.basename(src))
    shutil.copy2(src, dst)
    print(f"💾 Бэкап: {src} -> {dst}")
    return dst


def inject_corruption(path):
    """
    Пишем INJECT_BYTES случайных байт по смещению INJECT_OFFSET.
    Раньше тут было dd через shell — заменили на чистый os.open/os.pwrite,
    чтобы убрать любую интерполяцию пути в шелл-команду.
    """
    file_size = os.path.getsize(path)
    if file_size < INJECT_OFFSET + INJECT_BYTES:
        raise RuntimeError(
            f"Файл {path} слишком мал ({file_size} байт) для инъекции"
            f" на смещении {INJECT_OFFSET}"
        )
    with open("/dev/urandom", "rb") as rnd:
        payload = rnd.read(INJECT_BYTES)
    fd = os.open(path, os.O_WRONLY)
    try:
        # pwrite не двигает offset и не растягивает файл, в отличие от write().
        os.pwrite(fd, payload, INJECT_OFFSET)
        os.fsync(fd)
    finally:
        os.close(fd)
    print(f"💥 Инъекция: {INJECT_BYTES} случайных байт по offset {INJECT_OFFSET}")


def restore_file(backup_path, target_path):
    shutil.copy2(backup_path, target_path)
    # Восстановим uid/gid postgres'а на восстановленный файл.
    run(["chown", "postgres:postgres", target_path])
    print(f"♻️  Восстановлено: {backup_path} -> {target_path}")


def run_integrity_check():
    """Возвращает exit code 03_integrity_check.py."""
    res = subprocess.run(
        [sys.executable, INTEGRITY_SCRIPT],
        capture_output=True, text=True,
        env={**os.environ, "CHECK_HEAP": "0", "CHECK_INDEX_LEVEL": "simple"},
    )
    print(res.stdout)
    if res.stderr:
        print(res.stderr, file=sys.stderr)
    return res.returncode


def main():
    ensure_safety()

    print(f"=== Corruption test: целевой индекс {TARGET_INDEX} в БД {DB_NAME} ===")

    target_path = resolve_relfilepath(TARGET_INDEX)
    if not target_path or not os.path.exists(target_path):
        print(f"❌ Не удалось найти relfilenode для {TARGET_INDEX} (path={target_path})")
        sys.exit(1)
    print(f"📁 relfilenode: {target_path}")

    backup_dir = f"/var/tmp/corruption_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    backup_path = None

    try:
        # ---- Шаг 1: бэкап + инъекция ----
        stop_postgres()
        backup_path = backup_file(target_path, backup_dir)
        inject_corruption(target_path)
        start_postgres()

        # ---- Шаг 2: чекер должен поймать ----
        print("\n=== Прогон 03_integrity_check.py ПОСЛЕ инъекции ===")
        rc_corrupt = run_integrity_check()
        if rc_corrupt == 0:
            print("⚠️  Неожиданно: чекер вернул 0 — повреждение не обнаружено")
            print("    Возможно, инъекция попала в свободную область страницы.")
        else:
            print(f"✅ Ожидаемо: чекер вернул код {rc_corrupt} — повреждение найдено")
    finally:
        # ---- Шаг 3: восстановление (всегда, даже на ошибке) ----
        # Восстановление обязательно: ставим в finally, чтобы любой провал
        # выше не оставил БД в битом состоянии.
        try:
            stop_postgres()
            if backup_path and os.path.exists(backup_path):
                restore_file(backup_path, target_path)
            start_postgres()
        except Exception as e:
            print(f"❌❌❌ ВОССТАНОВЛЕНИЕ ПРОВАЛИЛОСЬ: {e}")
            print(f"    Ручное восстановление: cp {backup_dir}/* {os.path.dirname(target_path)}/")
            raise

    # ---- Шаг 4: чекер должен снова быть чистым ----
    print("\n=== Прогон 03_integrity_check.py ПОСЛЕ восстановления ===")
    rc_clean = run_integrity_check()
    if rc_clean == 0:
        print("✅ Восстановление подтверждено: чекер чист")
    else:
        print(f"❌ После восстановления чекер всё ещё рапортует ошибки (rc={rc_clean})")
        sys.exit(2)

    print(f"\n💾 Бэкап оставлен в {backup_dir} — удалите вручную, когда убедитесь, что всё ок.")


if __name__ == "__main__":
    main()
