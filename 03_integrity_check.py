#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import psycopg2
import psycopg2.extras
import sys
import time
import json
import os
from datetime import datetime

# --------------------------- Конфигурация ---------------------------
PG_HOST = "/var/run/postgresql"   # или 'localhost'
PG_PORT = 5432
PG_USER = "postgres"
PG_DATABASE = "postgres"          # системная БД для начального подключения
CHECK_HEAP = True                 # Проверять heap-таблицы (долго, но полезно)
CHECK_INDEX_LEVEL = 'parent'      # 'simple' - bt_index_check, 'parent' - bt_index_parent_check
EXCLUDE_DATABASES = ['template0', 'template1', 'postgres']
REPORT_FILE = "/var/log/postgresql/integrity_report.txt"
ZABBIX_OUTPUT_FILE = "/var/log/postgresql/zabbix_metrics.json"
LOG_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# --------------------------- Вспомогательные функции ---------------------------
def connect_to_db(dbname):
    """Создаёт соединение с указанной базой данных."""
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        dbname=dbname
    )

def ensure_amcheck_extension(conn):
    """Убеждается, что расширение amcheck установлено в текущей БД."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS amcheck;")
    conn.commit()
    print("√ Расширение amcheck проверено/установлено")

def get_user_databases():
    """Возвращает список названий всех пользовательских БД, исключая системные."""
    conn = connect_to_db(PG_DATABASE)
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT datname FROM pg_database
            WHERE datistemplate = false AND datname NOT IN %s
            ORDER BY datname;
        """, (tuple(EXCLUDE_DATABASES),))
        dbs = [row[0] for row in cur.fetchall()]
    conn.close()
    return dbs

def get_tables_and_indices(conn, schema='public'):
    """Возвращает список таблиц и индексов для переданной базы данных."""
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Таблицы пользовательских схем (кроме системных)
        cur.execute("""
            SELECT n.nspname AS schema_name, c.relname AS table_name
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('information_schema', 'pg_catalog')
            ORDER BY n.nspname, c.relname;
        """)
        tables = [(row['schema_name'], row['table_name']) for row in cur.fetchall()]
        # Индексы для этих таблиц
        indices = []
        for schema, table in tables:
            cur.execute("""
                SELECT indexrelid::regclass::text AS index_name
                FROM pg_index
                WHERE indrelid = (%s || '.' || %s)::regclass;
            """, (schema, table))
            for row in cur:
                indices.append((schema, table, row['index_name']))
    return tables, indices

def check_heap(conn, schema, table):
    """Запускает verify_heapam для таблицы. Возвращает (ok, error_message)."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT verify_heapam('{schema}.{table}');")
            cur.fetchone()
        return True, None
    except psycopg2.Error as e:
        return False, str(e)

def check_index(conn, index_name, level='parent'):
    """
    Проверяет индекс с помощью amcheck.
    level = 'simple' -> bt_index_check
           'parent'  -> bt_index_parent_check
    """
    try:
        with conn.cursor() as cur:
            if level == 'simple':
                cur.execute(f"SELECT bt_index_check('{index_name}');")
            else:
                cur.execute(f"SELECT bt_index_parent_check('{index_name}');")
            cur.fetchone()
        return True, None
    except psycopg2.Error as e:
        return False, str(e)

def check_system_statistics(conn):
    """Проверяет pg_stat_database на признаки повреждений."""
    warnings = []
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT datname, conflicts, deadlocks, blk_read_time, blk_write_time
            FROM pg_stat_database
            WHERE datname NOT IN %s;
        """, (tuple(EXCLUDE_DATABASES),))
        for row in cur:
            if row['conflicts'] > 0:
                warnings.append(f"База {row['datname']}: conflicts = {row['conflicts']}")
            if row['deadlocks'] > 0:
                warnings.append(f"База {row['datname']}: deadlocks = {row['deadlocks']}")
    return warnings

def main():
    print(f"=== Запуск проверки целостности PostgreSQL в {LOG_TIME} ===")
    all_errors = []
    all_warnings = []
    db_list = get_user_databases()
    print(f"Найдены базы данных: {', '.join(db_list)}")

    # Общая проверка системной статистики
    try:
        conn_sys = connect_to_db(PG_DATABASE)
        sys_warnings = check_system_statistics(conn_sys)
        all_warnings.extend(sys_warnings)
        conn_sys.close()
    except Exception as e:
        all_errors.append(f"Ошибка при сборе системной статистики: {e}")

    # Проверка каждой базы
    for dbname in db_list:
        print(f"\n--- Проверка базы: {dbname} ---")
        try:
            conn = connect_to_db(dbname)
            # Убедимся, что amcheck установлен
            ensure_amcheck_extension(conn)
            tables, indices = get_tables_and_indices(conn)
            print(f"  Таблиц: {len(tables)}, Индексов: {len(indices)}")
            # Проверка таблиц (heap)
            if CHECK_HEAP:
                for schema, tbl in tables:
                    ok, err = check_heap(conn, schema, tbl)
                    if not ok:
                        msg = f"Heap-проверка таблицы {schema}.{tbl} не пройдена: {err}"
                        all_errors.append(msg)
                        print(f"  ❌ {msg}")
                    else:
                        print(f"  ✓ Heap-проверка таблицы {schema}.{tbl}: OK")
            # Проверка индексов
            for schema, tbl, idx_name in indices:
                ok, err = check_index(conn, idx_name, CHECK_INDEX_LEVEL)
                if not ok:
                    msg = f"Индекс {idx_name} (таблица {schema}.{tbl}) повреждён: {err}"
                    all_errors.append(msg)
                    print(f"  ❌ {msg}")
                else:
                    print(f"  ✓ Индекс {idx_name}: OK")
            conn.close()
        except Exception as e:
            all_errors.append(f"Не удалось обработать БД {dbname}: {e}")
            print(f"  ⚠️ Ошибка при подключении/обработке: {e}")

    # Формирование человеко-читаемого отчёта
    report_lines = []
    report_lines.append("=" * 80)
    report_lines.append(f"ОТЧЁТ О ЦЕЛОСТНОСТИ POSTGRESQL от {LOG_TIME}")
    report_lines.append("=" * 80)
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
    report_lines.append("Подробности проверки:\n")
    report_lines.append(f"Проверено баз данных: {len(db_list)}")
    report_lines.append(f"Проверено таблиц (heap): {len(tables) if CHECK_HEAP else 'отключено'}")
    report_lines.append(f"Проверено индексов: {len(indices)}")
    report_lines.append("=" * 80)

    report_content = "\n".join(report_lines)
    print(report_content)
    # Запись в файл
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w") as f:
        f.write(report_content)

    # Формирование отчёта для Zabbix (JSON)
    # Ключевые метрики: количество ошибок, количество предупреждений
    zabbix_metrics = {
        "timestamp": int(time.time()),
        "error_count": len(all_errors),
        "warning_count": len(all_warnings),
        "status": 0 if (len(all_errors) == 0 and len(all_warnings) == 0) else 1
    }
    with open(ZABBIX_OUTPUT_FILE, "w") as f:
        json.dump(zabbix_metrics, f, indent=2)

    # Для использования с Zabbix можно также вывести в stdout в формате key value
    print("\n--- Zabbix-метрики (JSON) ---")
    print(json.dumps(zabbix_metrics, indent=2))

    # Возвращаем код ошибки для системы (0 = успех, 1 = есть предупреждения/ошибки)
    sys.exit(0 if len(all_errors) == 0 else 1)

if __name__ == "__main__":
    main()
