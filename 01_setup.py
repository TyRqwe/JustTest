#!/usr/bin/env python3
import subprocess
import sys
import os
import time

DB_NAME = "testdb"
TABLE_NAME = "test_data"
ROWS = 500_000

def run(cmd, check=True):
    print(f"> {cmd}")
    subprocess.run(cmd, shell=True, check=check)

def main():
    if os.geteuid() != 0:
        print("❌ Запустите с sudo: sudo python3 setup_db.py")
        sys.exit(1)

    print("=== Установка PostgreSQL и Python-библиотек ===")
    run("apt update")
    run("apt install -y postgresql postgresql-contrib python3-psutil python3-psycopg2")

    print("=== Настройка pg_hba.conf для локального доступа postgres (trust) ===")
    # Находим файл pg_hba.conf
    result = subprocess.run("find /etc/postgresql -name pg_hba.conf | head -1", shell=True, capture_output=True, text=True)
    pg_hba = result.stdout.strip()
    if not pg_hba:
        print("❌ Не найден pg_hba.conf")
        sys.exit(1)
    # Меняем peer на trust для локального подключения postgres
    run(f"sed -i 's/^local.*postgres.*peer/local   all             postgres                                trust/' {pg_hba}")
    # Также для всех остальных локальных подключений (на всякий случай)
    run(f"sed -i 's/^local.*all.*peer/local   all             all                                     trust/' {pg_hba}")

    print("=== Перезапуск PostgreSQL ===")
    run("systemctl restart postgresql")
    time.sleep(2)  # даём время подняться

    print("=== Создание базы данных и таблицы ===")
    run(f"sudo -u postgres psql -c 'DROP DATABASE IF EXISTS {DB_NAME}'", check=False)
    run(f"sudo -u postgres psql -c 'CREATE DATABASE {DB_NAME}'")

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
    CREATE INDEX idx_{TABLE_NAME}_id ON {TABLE_NAME}(id);
    SELECT COUNT(*) AS rows_inserted FROM {TABLE_NAME};
    """
    with open("/tmp/setup.sql", "w") as f:
        f.write(sql)
    run(f"sudo -u postgres psql -d {DB_NAME} -f /tmp/setup.sql")
    os.remove("/tmp/setup.sql")
    print("✅ Готово. База данных и таблица созданы, данные заполнены, аутентификация настроена.")

if __name__ == "__main__":
    main()
