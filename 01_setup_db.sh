#!/bin/bash
set -e

echo "=== Установка PostgreSQL и инструментов ==="
sudo apt update
# Исправлено: убран pgbench из списка, т.к. он входит в postgresql-contrib
sudo apt install -y postgresql postgresql-contrib bc

sudo systemctl enable postgresql
sudo systemctl start postgresql

DB_NAME="testdb"
TABLE_NAME="test_data"
ROWS_COUNT=500000   # Можно менять, но данные будут одинаковы

# Удаляем старую БД, если есть (для идемпотентности)
sudo -u postgres psql -c "DROP DATABASE IF EXISTS $DB_NAME;"
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME;"

echo "=== Создание таблицы $TABLE_NAME с $ROWS_COUNT строк (детерминированно) ==="
sudo -u postgres psql -d "$DB_NAME" <<SQL
DROP TABLE IF EXISTS $TABLE_NAME;
CREATE TABLE $TABLE_NAME (
    id SERIAL PRIMARY KEY,
    col1 INT NOT NULL,
    col2 INT NOT NULL,
    col3 INT NOT NULL,
    col4 INT NOT NULL,
    col5 TEXT NOT NULL
);

INSERT INTO $TABLE_NAME (col1, col2, col3, col4, col5)
SELECT
    col1,
    col2,
    col3,
    col1 + col2 + col3 AS col4,
    (col1::TEXT || col2::TEXT || col3::TEXT) AS col5
FROM (
    SELECT
        1111 + (i % 2223) AS col1,
        4444 + (i % 2223) AS col2,
        7777 + (i % 2223) AS col3
    FROM generate_series(1, $ROWS_COUNT) AS i
) AS t;

-- Проверка количества строк
SELECT COUNT(*) AS rows_inserted FROM $TABLE_NAME;

-- Индекс не нужен, т.к. id первичный ключ, но на всякий случай создаём
CREATE INDEX IF NOT EXISTS idx_test_data_id ON $TABLE_NAME(id);
SQL

echo "=== Подготовка завершена ==="
echo "База: $DB_NAME, таблица: $TABLE_NAME, строк: $ROWS_COUNT"
echo "Данные строго детерминированы (одинаковы при каждом запуске)."
