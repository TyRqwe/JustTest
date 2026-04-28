#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TARGET_TRANSACTIONS=${TARGET_TRANSACTIONS:-500000}
TIMEOUT_SEC=300

echo "=== Однопоточный тест (1 поток, UPDATE, цель: $TARGET_TRANSACTIONS транзакций) ==="
echo "Скрипт выполняется до ${TIMEOUT_SEC} секунд. Пожалуйста, ожидайте..."

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

OUTPUT_FILE="/tmp/pgbench_single.out"
start_time=$(date +%s)
timeout $TIMEOUT_SEC sudo -u postgres pgbench -d "$DB_NAME" -f "$TXN_FILE" -c 1 -j 1 -t $TARGET_TRANSACTIONS -n > "$OUTPUT_FILE" 2>&1
EXIT_CODE=$?
end_time=$(date +%s)
elapsed=$((end_time - start_time))

if [ $EXIT_CODE -eq 124 ]; then
    actual_time=$TIMEOUT_SEC
else
    actual_time=$elapsed
fi

if [ -f "$OUTPUT_FILE" ]; then
    TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' "$OUTPUT_FILE" | head -1)
    TPS=$(grep -oP 'tps = \K[0-9.]+' "$OUTPUT_FILE" | head -1)
fi

[ -z "$TRANSACTIONS" ] && TRANSACTIONS=0
[ -z "$TPS" ] && TPS=""

rm -f "$TXN_FILE" "$OUTPUT_FILE"

echo ""
echo "==================== РЕЗУЛЬТАТ ТЕСТА ===================="
echo "Тип теста:               однопоточный (UPDATE)"
echo "Количество потоков:      1"
if [ $EXIT_CODE -eq 124 ]; then
    echo "Статус:                  прерван по таймауту (${TIMEOUT_SEC} сек)"
    echo "Целевое число операций:  $TARGET_TRANSACTIONS (не достигнуто)"
else
    echo "Статус:                  завершён"
    echo "Целевое число операций:  $TARGET_TRANSACTIONS"
fi
echo "Время выполнения:        ${actual_time} сек"
echo "Выполнено операций:       $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "TPS (средний):           $TPS"
fi
echo "========================================================"
