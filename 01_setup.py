#!/usr/bin/env python3
import subprocess
import sys
import os

DB_NAME = "testdb"
TABLE_NAME = "test_data"
ROWS = 500_000


def run(argv, check=True, input_data=None):
    """Запуск без shell — аргументы передаём списком, никакой интерполяции."""
    print("> " + " ".join(argv))
    return subprocess.run(argv, check=check, input=input_data, text=True)


def ensure_debian_family():
    # Все вызовы apt ниже подразумевают Debian/Ubuntu. На RHEL-семействе
    # apt просто отсутствует, и мы предпочитаем понятную ошибку молчаливому падению.
    try:
        with open("/etc/os-release") as f:
            data = f.read()
    except OSError:
        print("❌ Не удалось прочитать /etc/os-release")
        sys.exit(1)
    fields = {}
    for line in data.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k] = v.strip().strip('"')
    id_ = fields.get("ID", "")
    id_like = fields.get("ID_LIKE", "")
    if id_ not in {"ubuntu", "debian"} and "debian" not in id_like:
        print(f"❌ Поддерживаются только Ubuntu/Debian. Обнаружено: ID={id_!r}, ID_LIKE={id_like!r}")
        sys.exit(1)


def wait_postgres_ready(timeout=30):
    # pg_isready детерминированно ждёт принятия соединений, в отличие от sleep().
    res = subprocess.run(
        ["pg_isready", "-t", str(timeout), "-h", "/var/run/postgresql"]
    )
    if res.returncode != 0:
        print(f"❌ postgres не отвечает за {timeout} сек")
        sys.exit(1)


def psql_postgres(sql, db=None):
    # SQL передаём через stdin, чтобы не зависеть от systemd PrivateTmp=yes
    # у юнита postgresql.service (файл в /tmp от root для postgres невидим).
    argv = ["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1"]
    if db:
        argv += ["-d", db]
    return run(argv, input_data=sql)


def main():
    if os.geteuid() != 0:
        print("❌ Запустите с sudo: sudo python3 01_setup.py")
        sys.exit(1)

    ensure_debian_family()

    print("=== Установка PostgreSQL и Python-библиотек ===")
    run(["apt", "update"])
    run([
        "apt", "install", "-y",
        "postgresql", "postgresql-contrib",
        "python3-psutil", "python3-psycopg2",
    ])

    print("=== Запуск PostgreSQL и проверка готовности ===")
    # На Debian/Ubuntu postgres стартует автоматически после установки,
    # но на повторном запуске скрипта это уже не первый старт — гарантируем явный.
    run(["systemctl", "enable", "--now", "postgresql"])
    wait_postgres_ready(timeout=30)

    print("=== Создание базы данных и таблицы ===")
    # Аутентификация — peer через unix-сокет под пользователем postgres.
    # pg_hba.conf специально не правим: peer уже работает, а trust на ВМ
    # с публичным IP — плохая идея даже на тестовом стенде.
    psql_postgres(f"DROP DATABASE IF EXISTS {DB_NAME};")
    psql_postgres(f"CREATE DATABASE {DB_NAME};")

    # idx_test_data_id из старой версии скрипта удалён: SERIAL PRIMARY KEY
    # уже создаёт btree на id, второй такой же индекс — мёртвый и портит
    # write-нагрузку (две записи в индекс на каждый UPDATE).
    sql = f"""
    DROP TABLE IF EXISTS {TABLE_NAME};
    CREATE TABLE {TABLE_NAME} (
        id SERIAL PRIMARY KEY,
        col1 INT NOT NULL,
        col2 INT NOT NULL,
        col3 INT NOT NULL,
        col4 INT NOT NULL,
        col5 TEXT NOT NULL
    );
    INSERT INTO {TABLE_NAME} (col1, col2, col3, col4, col5)
    SELECT
        col1, col2, col3,
        col1 + col2 + col3 AS col4,
        (col1::TEXT || col2::TEXT || col3::TEXT) AS col5
    FROM (
        SELECT
            1111 + (i % 2223) AS col1,
            4444 + (i % 2223) AS col2,
            7777 + (i % 2223) AS col3
        FROM generate_series(1, {ROWS}) AS i
    ) AS t;
    SELECT COUNT(*) AS rows_inserted FROM {TABLE_NAME};
    """
    psql_postgres(sql, db=DB_NAME)
    print("✅ Готово. База данных и таблица созданы, данные заполнены.")


if __name__ == "__main__":
    main()
