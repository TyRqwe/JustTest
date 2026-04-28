#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TARGET_TRANSACTIONS=${TARGET_TRANSACTIONS:-100000}  # Агрегации тяжелее, цель 100 тыс.
TIMEOUT_SEC=300

# Число потоков = число ядер CPU
if command -v nproc &>/dev/null; then
    CPUS=$(nproc)
else
    CPUS=$(grep -c ^processor /proc/cpuinfo)
fi
THREADS=${THREADS:-$CPUS}
[ $THREADS -lt 1 ] && THREADS=4

echo "=== Многопоточный тест ($THREADS потоков, цель: $TARGET_TRANSACTIONS агрегаций, таймаут ${TIMEOUT_SEC} сек) ==="

MAX_ID=$(sudo -u postgres psql -d "$DB_NAME" -t -c "SELECT MAX(id) FROM $TABLE_NAME;" | xargs)
if [ -z "$MAX_ID" ] || [ "$MAX_ID" -eq 0 ]; then
    echo "Ошибка: таблица $TABLE_NAME пуста. Запустите sudo ./01_setup_db.sh"
    exit 1
fi

# Создаём скрипт с тяжёлой агрегацией (группировка по col1, вычисления)
TXN_FILE="/tmp/pgbench_multi_$$.sql"
cat > "$TXN_FILE" <<EOF
SELECT col1, SUM(col2) AS sum_col2, AVG(col3) AS avg_col3, COUNT(*) AS cnt
FROM $TABLE_NAME
WHERE id <= $MAX_ID
GROUP BY col1;
EOF
chmod 644 "$TXN_FILE"

echo "Запуск pgbench с агрегационными запросами (прогресс каждые 10 сек)..."
sudo -u postgres pgbench -d "$DB_NAME" \
    -f "$TXN_FILE" \
    -c $THREADS -j $THREADS \
    -t $TARGET_TRANSACTIONS -T $TIMEOUT_SEC \
    -P 10 -n 2>&1 | tee /tmp/pgbench_multi.out

TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' /tmp/pgbench_multi.out)
ACTUAL_TIME=$(grep -oP 'duration: \K[0-9]+' /tmp/pgbench_multi.out)
TPS=$(grep -oP 'tps = \K[0-9.]+' /tmp/pgbench_multi.out | head -1)

if [ -z "$ACTUAL_TIME" ]; then
    ACTUAL_TIME=$TIMEOUT_SEC
fi

rm -f "$TXN_FILE" /tmp/pgbench_multi.out

# Вычисляем приблизительную производительность на один поток (через bc)
if [ -n "$TPS" ] && [ "$THREADS" -gt 0 ]; then
    TPS_PER_THREAD=$(echo "scale=2; $TPS / $THREADS" | bc)
else
    TPS_PER_THREAD="N/A"
fi

echo ""
echo "================== РЕЗУЛЬТАТ МНОГОПОТОЧНОГО ТЕСТА =================="
echo "Количество потоков:       $THREADS"
echo "Целевое число операций:   $TARGET_TRANSACTIONS"
if [ "$ACTUAL_TIME" -lt "$TIMEOUT_SEC" ]; then
    echo "✅ Тест завершён досрочно за ${ACTUAL_TIME} сек"
else
    echo "⚠️ Тест прерван по таймауту (${TIMEOUT_SEC} сек), целевое не достигнуто"
fi
echo "Выполнено операций:       $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Суммарный TPS:            $TPS"
    echo "TPS на один поток:        ≈ $TPS_PER_THREAD"
fi
echo "====================================================================="
