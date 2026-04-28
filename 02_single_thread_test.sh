#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TIMEOUT_SEC=300   # 5 минут

# Получаем максимальный id из таблицы
MAX_ID=$(sudo -u postgres psql -d "$DB_NAME" -t -c "SELECT MAX(id) FROM $TABLE_NAME;" | xargs)
if [ -z "$MAX_ID" ]; then
    echo "Ошибка: таблица $TABLE_NAME пуста или не существует. Запустите сначала 01_setup_db.sh"
    exit 1
fi

echo "=== Однопоточный тест (1 поток, 5 минут) ==="
echo "Проверяем производительность ядра CPU..."

# Создаём временный файл транзакции
TXN_FILE=$(mktemp)
cat > "$TXN_FILE" <<EOF
\set id random(1, $MAX_ID)
UPDATE $TABLE_NAME
SET col4 = col1 + col2 + col3,
    col5 = col1::TEXT || col2::TEXT || col3::TEXT
WHERE id = :id;
EOF

# Запуск pgbench с 1 клиентом и 1 потоком, таймаут 300 сек
OUTPUT=$(sudo -u postgres pgbench -d "$DB_NAME" \
    -f "$TXN_FILE" \
    -c 1 -j 1 \
    -T $TIMEOUT_SEC \
    -n 2>&1)

rm -f "$TXN_FILE"

# Извлекаем TPS (без учёта соединений) и количество транзакций
TPS=$(echo "$OUTPUT" | grep -oP 'tps = \K[0-9.]+' | head -1)
TRANSACTIONS=$(echo "$OUTPUT" | grep -oP 'number of transactions actually processed: \K[0-9]+')
ACTUAL_TIME=$(echo "$OUTPUT" | grep -oP 'duration: \K[0-9]+' | head -1)

if [ -z "$ACTUAL_TIME" ]; then
    ACTUAL_TIME=$TIMEOUT_SEC
fi

echo ""
echo "================== РЕЗУЛЬТАТ ОДНОПОТОЧНОГО ТЕСТА =================="
echo "Время выполнения:        ${ACTUAL_TIME} сек (лимит ${TIMEOUT_SEC} сек)"
echo "Всего операций (UPDATE): $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Средняя производительность: $TPS транзакций в секунду"
else
    echo "Не удалось вычислить TPS"
fi
echo "==================================================================="
