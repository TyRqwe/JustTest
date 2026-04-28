#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TARGET_TRANSACTIONS=${TARGET_TRANSACTIONS:-500000}
TIMEOUT_SEC=300

echo "=== Однопоточный тест (1 поток, цель: $TARGET_TRANSACTIONS UPDATE, таймаут ${TIMEOUT_SEC} сек) ==="

MAX_ID=$(sudo -u postgres psql -d "$DB_NAME" -t -c "SELECT MAX(id) FROM $TABLE_NAME;" | xargs)
if [ -z "$MAX_ID" ] || [ "$MAX_ID" -eq 0 ]; then
    echo "Ошибка: таблица $TABLE_NAME пуста. Запустите sudo ./01_setup_db.sh"
    exit 1
fi

TXN_FILE="/tmp/pgbench_single_$$.sql"
cat > "$TXN_FILE" <<EOF
\set id random(1, $MAX_ID)
UPDATE $TABLE_NAME
SET col4 = col1 + col2 + col3,
    col5 = col1::TEXT || col2::TEXT || col3::TEXT
WHERE id = :id;
EOF
chmod 644 "$TXN_FILE"

echo "Запуск pgbench (прогресс каждые 10 сек)..."
OUTPUT_FILE="/tmp/pgbench_single.out"
# Запускаем pgbench с таймаутом, вывод дублируем в консоль и файл
timeout $TIMEOUT_SEC bash -c "sudo -u postgres pgbench -d '$DB_NAME' -f '$TXN_FILE' -c 1 -j 1 -t $TARGET_TRANSACTIONS -P 10 -q -n 2>&1 | tee '$OUTPUT_FILE'"
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    # Таймаут: pgbench не успел выполнить все транзакции
    echo "⚠️ Тест прерван по таймауту (${TIMEOUT_SEC} сек)"
    TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' "$OUTPUT_FILE" | head -1)
    ACTUAL_TIME=$TIMEOUT_SEC
    TPS=$(grep -oP 'tps = \K[0-9.]+' "$OUTPUT_FILE" | head -1)
    if [ -z "$TRANSACTIONS" ]; then
        # Если pgbench не успел вывести статистику, пробуем извлечь из прогресса
        TRANSACTIONS=$(grep -oP 'progress: .*? (\d+) tps' "$OUTPUT_FILE" | tail -1 | awk '{print $(NF-1)}')
        [ -z "$TRANSACTIONS" ] && TRANSACTIONS=0
    fi
    COMPLETED="no"
else
    # Нормальное завершение (или ошибка, но pgbench вывел статистику)
    TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' "$OUTPUT_FILE")
    ACTUAL_TIME=$(grep -oP 'duration: \K[0-9]+' "$OUTPUT_FILE")
    TPS=$(grep -oP 'tps = \K[0-9.]+' "$OUTPUT_FILE" | head -1)
    COMPLETED="yes"
fi

[ -z "$TRANSACTIONS" ] && TRANSACTIONS=0
[ -z "$ACTUAL_TIME" ] && ACTUAL_TIME=$TIMEOUT_SEC

rm -f "$TXN_FILE" "$OUTPUT_FILE"

echo ""
echo "================== РЕЗУЛЬТАТ ОДНОПОТОЧНОГО ТЕСТА =================="
echo "Целевое число операций:   $TARGET_TRANSACTIONS"
if [ "$COMPLETED" = "yes" ]; then
    echo "✅ Тест завершён досрочно за ${ACTUAL_TIME} сек"
else
    echo "⚠️ Тест прерван по таймауту (${TIMEOUT_SEC} сек), целевое не достигнуто"
fi
echo "Выполнено операций:       $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Средняя производительность: $TPS транз/сек"
fi
echo "================================================================"
