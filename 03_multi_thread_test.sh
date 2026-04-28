#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TIMEOUT_SEC=300   # 5 минут

# Количество потоков = количество ядер CPU (или 4, если не определяется)
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
if [ -z "$MAX_ID" ]; then
    echo "Ошибка: таблица $TABLE_NAME пуста или не существует. Запустите сначала 01_setup_db.sh"
    exit 1
fi

TXN_FILE=$(mktemp)
cat > "$TXN_FILE" <<EOF
\set id random(1, $MAX_ID)
UPDATE $TABLE_NAME
SET col4 = col1 + col2 + col3,
    col5 = col1::TEXT || col2::TEXT || col3::TEXT
WHERE id = :id;
EOF

OUTPUT=$(sudo -u postgres pgbench -d "$DB_NAME" \
    -f "$TXN_FILE" \
    -c $THREADS -j $THREADS \
    -T $TIMEOUT_SEC \
    -n 2>&1)

rm -f "$TXN_FILE"

TPS=$(echo "$OUTPUT" | grep -oP 'tps = \K[0-9.]+' | head -1)
TRANSACTIONS=$(echo "$OUTPUT" | grep -oP 'number of transactions actually processed: \K[0-9]+')
ACTUAL_TIME=$(echo "$OUTPUT" | grep -oP 'duration: \K[0-9]+' | head -1)

if [ -z "$ACTUAL_TIME" ]; then
    ACTUAL_TIME=$TIMEOUT_SEC
fi

echo ""
echo "================== РЕЗУЛЬТАТ МНОГОПОТОЧНОГО ТЕСТА =================="
echo "Количество потоков:       $THREADS"
echo "Время выполнения:         ${ACTUAL_TIME} сек (лимит ${TIMEOUT_SEC} сек)"
echo "Всего операций (UPDATE):  $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Суммарная производительность: $TPS транзакций в секунду"
    echo "На один поток (при линейном росте): $TPS ≈ $((TPS / THREADS)) т/с"
else
    echo "Не удалось вычислить TPS"
fi
echo "====================================================================="
