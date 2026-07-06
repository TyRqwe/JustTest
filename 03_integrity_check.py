#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка целостности PostgreSQL через amcheck.

Поддерживает btree-индексы через bt_index_check / bt_index_parent_check,
heap-таблицы через verify_heapam, и счётчик checksum_failures
из pg_stat_database (полезен только при включённых data_checksums).
"""

import psycopg2
import psycopg2.extras
from psycopg2 import sql
import sys
import time
import json
import os
from datetime import datetime

# --------------------------- Конфигурация ---------------------------
PG_HOST = os.environ.get("PG_HOST", "/var/run/postgresql")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_USER = os.environ.get("PG_USER", "postgres")
PG_DATABASE = os.environ.get("PG_DATABASE", "postgres")

# CHECK_HEAP по умолчанию выключен: на больших БД verify_heapam идёт часами
# и нагружает диск как VACUUM FULL. Включается явно: CHECK_HEAP=1.
CHECK_HEAP = os.environ.get("CHECK_HEAP", "0") in ("1", "true", "True", "yes")

# 'simple' — bt_index_check (берёт AccessShareLock, безопасен онлайн).
# 'parent' — bt_index_parent_check (берёт AccessExclusiveLock, прод не рекомендуется).
CHECK_INDEX_LEVEL = os.environ.get("CHECK_INDEX_LEVEL", "simple")

EXCLUDE_DATABASES = tuple(
    x.strip() for x in os.environ.get(
        "EXCLUDE_DATABASES", "template0,template1,postgres"
    ).split(",") if x.strip()
)

REPORT_DIR = os.environ.get("REPORT_DIR", "/var/log/postgresql")
LOG_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
STAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
REPORT_FILE = os.path.join(REPORT_DIR, f"integrity_report_{STAMP}.txt")
# Zabbix файл оставляем с фиксированным именем для удобного поллинга.
ZABBIX_OUTPUT_FILE = os.path.join(REPORT_DIR, "zabbix_metrics.json")


def connect_to_db(dbname):
    return psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname=dbname)


def ensure_amcheck_extension(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS amcheck;")
    conn.commit()
    print("√ Расширение amcheck проверено/установлено")


def show_data_checksums_state():
    """
    data_checksums — кластеро-уровневая настройка, ставится при initdb.
    Без неё счётчик checksum_failures всегда будет 0, и эта 'хорошая'
    цифра ничего не доказывает. Предупреждаем явно.
    """
    try:
        conn = connect_to_db(PG_DATABASE)
        with conn.cursor() as cur:
            cur.execute("SHOW data_checksums")
            state = cur.fetchone()[0]
        conn.close()
        print(f"ℹ️  data_checksums = {state}")
        if state != "on":
            print("⚠️  Page checksums выключены — checksum_failures всегда будет 0.")
            print("    Чтобы включить: initdb --data-checksums (только при создании кластера)")
            print("    или pg_checksums --enable (на остановленном кластере, PG 12+).")
        return state
    except Exception as e:
        print(f"⚠️  Не удалось проверить data_checksums: {e}")
        return None


def get_user_databases():
    conn = connect_to_db(PG_DATABASE)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datname NOT IN %s "
            "ORDER BY datname;",
            (EXCLUDE_DATABASES,),
        )
        dbs = [row[0] for row in cur.fetchall()]
    conn.close()
    return dbs


def get_tables_and_indices(conn):
    """
    Возвращает пары (schema, table) и тройки (schema, table, index, am_name).
    am_name нужен, чтобы корректно фильтровать btree от GIN/GiST/hash/BRIN.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT n.nspname AS schema_name, c.relname AS table_name
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('information_schema', 'pg_catalog')
              AND n.nspname NOT LIKE 'pg_temp_%'
              AND n.nspname NOT LIKE 'pg_toast%'
            ORDER BY n.nspname, c.relname;
        """)
        tables = [(row["schema_name"], row["table_name"]) for row in cur.fetchall()]

        cur.execute("""
            SELECT n.nspname  AS schema_name,
                   t.relname  AS table_name,
                   i.relname  AS index_name,
                   am.amname  AS am_name
            FROM pg_index x
            JOIN pg_class i      ON i.oid = x.indexrelid
            JOIN pg_class t      ON t.oid = x.indrelid
            JOIN pg_namespace n  ON n.oid = t.relnamespace
            JOIN pg_am am        ON am.oid = i.relam
            WHERE n.nspname NOT IN ('information_schema', 'pg_catalog')
              AND n.nspname NOT LIKE 'pg_temp_%'
              AND n.nspname NOT LIKE 'pg_toast%'
            ORDER BY n.nspname, t.relname, i.relname;
        """)
        indices = [
            (row["schema_name"], row["table_name"], row["index_name"], row["am_name"])
            for row in cur.fetchall()
        ]
    return tables, indices


def check_heap(conn, schema, table):
    try:
        with conn.cursor() as cur:
            # sql.Identifier правильно экранирует имена со спецсимволами;
            # verify_heapam ожидает regclass — передаём как литерал-строку
            # через psycopg2-параметризацию.
            qualified = f"{schema}.{table}"
            cur.execute(
                sql.SQL("SELECT verify_heapam({})").format(sql.Literal(qualified))
            )
            cur.fetchone()
        return True, None
    except psycopg2.Error as e:
        return False, str(e)


def check_index(conn, schema, table, index, level="simple"):
    try:
        with conn.cursor() as cur:
            qualified = f"{schema}.{index}"
            func = "bt_index_check" if level == "simple" else "bt_index_parent_check"
            cur.execute(
                sql.SQL("SELECT {func}({arg})").format(
                    func=sql.Identifier(func),
                    arg=sql.Literal(qualified),
                )
            )
            cur.fetchone()
        return True, None
    except psycopg2.Error as e:
        return False, str(e)


def get_checksum_failures():
    """Суммарное число сбоев checksum по всем БД (PG 12+)."""
    try:
        conn = connect_to_db(PG_DATABASE)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(checksum_failures), 0)
                FROM pg_stat_database
                WHERE datname NOT IN %s;
            """, (EXCLUDE_DATABASES,))
            total = cur.fetchone()[0]
        conn.close()
        return int(total or 0)
    except psycopg2.Error as e:
        return f"error: {e}"


def main():
    print(f"=== Запуск проверки целостности PostgreSQL в {LOG_TIME} ===")
    if CHECK_INDEX_LEVEL == "parent":
        print("⚠️  CHECK_INDEX_LEVEL=parent — bt_index_parent_check возьмёт "
              "ACCESS EXCLUSIVE LOCK на каждый индекс. На проде не запускать.")

    data_checksums_state = show_data_checksums_state()

    all_errors = []
    all_warnings = []
    total_tables_checked = 0
    total_indices_checked = 0
    total_indices_skipped = 0

    db_list = get_user_databases()
    print(f"Найдены базы данных: {', '.join(db_list) if db_list else '(нет)'}")

    # Аккумулирующие счётчики, а не len(tables) последней итерации,
    # как было в первой версии скрипта.
    checksum_failures = get_checksum_failures()
    if isinstance(checksum_failures, int):
        if checksum_failures > 0:
            all_errors.append(
                f"pg_stat_database.checksum_failures = {checksum_failures} (>0)"
            )
        elif data_checksums_state != "on":
            all_warnings.append(
                "checksum_failures = 0, но data_checksums выключены — метрика бессмысленна"
            )
    else:
        all_errors.append(f"Не удалось прочитать checksum_failures: {checksum_failures}")

    for dbname in db_list:
        print(f"\n--- Проверка базы: {dbname} ---")
        try:
            conn = connect_to_db(dbname)
            ensure_amcheck_extension(conn)
            tables, indices = get_tables_and_indices(conn)
            print(f"  Таблиц: {len(tables)}, Индексов: {len(indices)}")

            if CHECK_HEAP:
                for schema, tbl in tables:
                    ok, err = check_heap(conn, schema, tbl)
                    total_tables_checked += 1
                    if not ok:
                        msg = f"[{dbname}] Heap {schema}.{tbl}: {err}"
                        all_errors.append(msg)
                        print(f"  ❌ {msg}")
                    else:
                        print(f"  ✓ Heap {schema}.{tbl}: OK")

            for schema, tbl, idx_name, am_name in indices:
                if am_name != "btree":
                    # amcheck в стоковом PG умеет только btree (для GIN — только с PG 18).
                    # Не-btree пропускаем явно и пишем в отчёт, чтобы это было видно.
                    total_indices_skipped += 1
                    print(f"  · {idx_name} ({am_name}): skipped (unsupported AM)")
                    continue
                ok, err = check_index(conn, schema, tbl, idx_name, CHECK_INDEX_LEVEL)
                total_indices_checked += 1
                if not ok:
                    msg = f"[{dbname}] Индекс {schema}.{idx_name} (таблица {tbl}): {err}"
                    all_errors.append(msg)
                    print(f"  ❌ {msg}")
                else:
                    print(f"  ✓ Индекс {schema}.{idx_name}: OK")
            conn.close()
        except Exception as e:
            all_errors.append(f"Не удалось обработать БД {dbname}: {e}")
            print(f"  ⚠️ Ошибка при подключении/обработке: {e}")

    # ---------- Отчёт ----------
    report_lines = []
    report_lines.append("=" * 80)
    report_lines.append(f"ОТЧЁТ О ЦЕЛОСТНОСТИ POSTGRESQL от {LOG_TIME}")
    report_lines.append("=" * 80)
    report_lines.append(f"data_checksums: {data_checksums_state}")
    report_lines.append(f"checksum_failures (summary): {checksum_failures}")

    if not all_errors and not all_warnings:
        report_lines.append("\n✅ СТАТУС: ВСЁ В ПОРЯДКЕ. Повреждений не обнаружено.\n")
    else:
        if all_warnings:
            report_lines.append("\n⚠️ ПРЕДУПРЕЖДЕНИЯ:")
            for w in all_warnings:
                report_lines.append(f"  - {w}")
        if all_errors:
            report_lines.append("\n❌ ОШИБКИ ЦЕЛОСТНОСТИ:")
            for err in all_errors:
                report_lines.append(f"  - {err}")
        report_lines.append("\nСтатус: ТРЕБУЕТ ВНИМАНИЯ\n")

    report_lines.append("Подробности проверки:")
    report_lines.append(f"  Проверено баз данных: {len(db_list)}")
    report_lines.append(
        "  Проверено таблиц (heap): "
        + (str(total_tables_checked) if CHECK_HEAP else "отключено (CHECK_HEAP=0)")
    )
    report_lines.append(f"  Проверено btree-индексов: {total_indices_checked}")
    report_lines.append(f"  Пропущено индексов (не-btree): {total_indices_skipped}")
    report_lines.append("=" * 80)

    report_content = "\n".join(report_lines)
    print("\n" + report_content)

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_FILE, "w") as f:
        f.write(report_content)
    print(f"📄 Отчёт: {REPORT_FILE}")

    zabbix_metrics = {
        "timestamp": int(time.time()),
        "error_count": len(all_errors),
        "warning_count": len(all_warnings),
        "checksum_failures": (
            checksum_failures if isinstance(checksum_failures, int) else -1
        ),
        "data_checksums": data_checksums_state,
        "status": 0 if not all_errors else 1,
    }
    with open(ZABBIX_OUTPUT_FILE, "w") as f:
        json.dump(zabbix_metrics, f, indent=2)
    print(f"📄 Zabbix-метрики: {ZABBIX_OUTPUT_FILE}")
    print("\n--- Zabbix-метрики (JSON) ---")
    print(json.dumps(zabbix_metrics, indent=2))

    sys.exit(0 if not all_errors else 1)


if __name__ == "__main__":
    main()
