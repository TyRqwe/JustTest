#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TIMEOUT_SEC=300

CPUS=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo)
THREADS=${THREADS:-$CPUS}
[ $THREADS -lt 1 ] && THREADS=4

echo "=== Многопоточный тест ($THREADS потоков, агрегации, длительность ${TIMEOUT_SEC} сек) ==="
echo "Скрипт выполняется до ${TIMEOUT_SEC} секунд. Пожалуйста, ожидайте..."

MAX_ID=$(sudo -u postgres psql -d "$DB_NAME" -t -c "SELECT MAX(id) FROM $TABLE_NAME;" | xargs)
if [ -z "$MAX_ID" ] || [ "$MAX_ID" -eq 0 ]; then
    echo "Ошибка: таблица $TABLE_NAME пуста. Запустите sudo ./01_setup_db.sh"
    exit 1
fi

TXN_FILE="/tmp/pgbench_multi_$$.sql"
cat > "$TXN_FILE" <<EOF
SELECT col1, SUM(col2) AS sum_col2, AVG(col3) AS avg_col3, COUNT(*) AS cnt
FROM $TABLE_NAME
WHERE id <= $MAX_ID
GROUP BY col1;
EOF
chmod 644 "$TXN_FILE"

OUTPUT_FILE="/tmp/pgbench_multi.out"
start_time=$(date +%s)
# Запуск с фиксированной длительностью -T, без -t
sudo -u postgres pgbench -d "$DB_NAME" -f "$TXN_FILE" -c $THREADS -j $THREADS -T $TIMEOUT_SEC -n > "$OUTPUT_FILE" 2>&1
EXIT_CODE=$?
end_time=$(date +%s)
elapsed=$((end_time - start_time))

if [ -f "$OUTPUT_FILE" ] && [ -s "$OUTPUT_FILE" ]; then
    TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' "$OUTPUT_FILE" | head -1)
    TPS=$(grep -oP 'tps = \K[0-9.]+' "$OUTPUT_FILE" | head -1)
fi

[ -z "$TRANSACTIONS" ] && TRANSACTIONS=0
[ -z "$TPS" ] && TPS=""

rm -f "$TXN_FILE" "$OUTPUT_FILE"

if [ -n "$TPS" ] && [ "$THREADS" -gt 0 ]; then
    TPS_PER_THREAD=$(echo "scale=2; $TPS / $THREADS" | bc 2>/dev/null || echo "N/A")
else
    TPS_PER_THREAD="N/A"
fi

echo ""
echo "================== РЕЗУЛЬТАТ МНОГОПОТОЧНОГО ТЕСТА =================="
echo "Количество потоков:       $THREADS"
echo "Длительность теста:       ${TIMEOUT_SEC} сек"
echo "Выполнено операций:       $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Суммарный TPS:            $TPS"
    [ "$TPS_PER_THREAD" != "N/A" ] && echo "TPS на один поток:        ≈ $TPS_PER_THREAD"
fi
if [ $EXIT_CODE -ne 0 ]; then
    echo "⚠️ Тест завершился с ошибкой (код $EXIT_CODE)"
fi
echo "====================================================================="
