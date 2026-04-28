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

OUTPUT_FILE="/tmp/pgbench_single.out"
start_time=$(date +%s)
echo "Запуск pgbench (прогресс каждые 10 сек)..."
# stdbuf отключает буферизацию для немедленного вывода
timeout $TIMEOUT_SEC sh -c "sudo -u postgres stdbuf -oL -eL pgbench -d '$DB_NAME' -f '$TXN_FILE' -c 1 -j 1 -t $TARGET_TRANSACTIONS -P 10 -n 2>&1 | grep -v '^pgbench: client' | grep -vE '^(SET|UPDATE|INSERT|DELETE|SELECT)|col5 =|WHERE id =' | tee '$OUTPUT_FILE'"
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
echo "================== РЕЗУЛЬТАТ ОДНОПОТОЧНОГО ТЕСТА =================="
echo "Целевое число операций:   $TARGET_TRANSACTIONS"
if [ $EXIT_CODE -eq 124 ]; then
    echo "⚠️ Тест прерван по таймауту (${TIMEOUT_SEC} сек), целевое не достигнуто"
elif [ $EXIT_CODE -eq 0 ]; then
    echo "✅ Тест завершён за ${actual_time} сек (все транзакции выполнены)"
else
    echo "⚠️ Тест завершился с ошибкой (код $EXIT_CODE)"
fi
echo "Выполнено операций:       $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Средняя производительность: $TPS транз/сек"
fi
echo "================================================================"
