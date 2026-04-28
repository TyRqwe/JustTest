#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TIMEOUT_SEC=300

if command -v nproc &> /dev/null; then
    CPUS=$(nproc)
else
    CPUS=$(grep -c ^processor /proc/cpuinfo)
fi
THREADS=${THREADS:-$CPUS}
if [ $THREADS -lt 1 ]; then THREADS=4; fi

echo "=== Многопоточный тест ($THREADS потоков, 5 минут) ==="
echo "Оцениваем масштабируемость системы..."

MAX_ID=$(sudo -u postgres psql -d "$DB_NAME" -t -c "SELECT MAX(id) FROM $TABLE_NAME;" | xargs)
if [ -z "$MAX_ID" ] || [ "$MAX_ID" -eq 0 ]; then
    echo "Ошибка: таблица $TABLE_NAME пуста или не существует."
    echo "Запустите сначала: sudo ./01_setup_db.sh"
    exit 1
fi

TXN_FILE="/tmp/pgbench_multi_$$.sql"
cat > "$TXN_FILE" <<EOF
\set id random(1, $MAX_ID)
UPDATE $TABLE_NAME
SET col4 = col1 + col2 + col3,
    col5 = col1::TEXT || col2::TEXT || col3::TEXT
WHERE id = :id;
EOF
chmod 644 "$TXN_FILE"

echo "Запуск pgbench (прогресс каждые 10 сек, $THREADS потоков)..."
sudo -u postgres pgbench -d "$DB_NAME" \
    -f "$TXN_FILE" \
    -c $THREADS -j $THREADS \
    -T $TIMEOUT_SEC \
    -P 10 \
    -n 2>&1 | tee /tmp/pgbench_multi.out

TPS=$(grep -oP 'tps = \K[0-9.]+' /tmp/pgbench_multi.out | head -1)
TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' /tmp/pgbench_multi.out)
ACTUAL_TIME=$(grep -oP 'duration: \K[0-9]+' /tmp/pgbench_multi.out)

if [ -z "$ACTUAL_TIME" ]; then
    ACTUAL_TIME=$TIMEOUT_SEC
fi

rm -f "$TXN_FILE" /tmp/pgbench_multi.out

echo ""
echo "================== РЕЗУЛЬТАТ МНОГОПОТОЧНОГО ТЕСТА =================="
echo "Количество потоков:       $THREADS"
echo "Время выполнения:         ${ACTUAL_TIME} сек (лимит ${TIMEOUT_SEC} сек)"
echo "Всего операций (UPDATE):  $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Суммарная производительность: $TPS транзакций в секунду"
    echo "На один поток (при линейном росте): ≈ $((TPS / THREADS)) т/с"
else
    echo "Не удалось вычислить TPS"
fi
echo "====================================================================="
