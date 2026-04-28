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
# Запускаем pgbench с таймаутом
timeout $TIMEOUT_SEC sudo -u postgres pgbench -d "$DB_NAME" \
    -f "$TXN_FILE" \
    -c 1 -j 1 \
    -t $TARGET_TRANSACTIONS \
    -P 10 -n > /tmp/pgbench_single.out 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    # Таймаут сработал: pgbench не успел завершить все транзакции
    echo "⚠️ Тест прерван по таймауту (${TIMEOUT_SEC} сек)"
    # Нужно получить количество выполненных транзакций из частичного вывода
    TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' /tmp/pgbench_single.out | head -1)
    ACTUAL_TIME=$TIMEOUT_SEC
    TPS=$(grep -oP 'tps = \K[0-9.]+' /tmp/pgbench_single.out | head -1)
    COMPLETED="no"
else
    # Нормальное завершение
    TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' /tmp/pgbench_single.out)
    ACTUAL_TIME=$(grep -oP 'duration: \K[0-9]+' /tmp/pgbench_single.out)
    TPS=$(grep -oP 'tps = \K[0-9.]+' /tmp/pgbench_single.out | head -1)
    COMPLETED="yes"
fi

if [ -z "$TRANSACTIONS" ]; then
    TRANSACTIONS=0
fi

rm -f "$TXN_FILE" /tmp/pgbench_single.out

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
echo "================================================================"#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TARGET_TRANSACTIONS=${TARGET_TRANSACTIONS:-500000}  # Цель: 500 тыс. UPDATE
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
sudo -u postgres pgbench -d "$DB_NAME" \
    -f "$TXN_FILE" \
    -c 1 -j 1 \
    -t $TARGET_TRANSACTIONS -T $TIMEOUT_SEC \
    -P 10 -n 2>&1 | tee /tmp/pgbench_single.out

# Извлечение данных
TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' /tmp/pgbench_single.out)
ACTUAL_TIME=$(grep -oP 'duration: \K[0-9]+' /tmp/pgbench_single.out)
TPS=$(grep -oP 'tps = \K[0-9.]+' /tmp/pgbench_single.out | head -1)

if [ -z "$ACTUAL_TIME" ]; then
    ACTUAL_TIME=$TIMEOUT_SEC
fi

rm -f "$TXN_FILE" /tmp/pgbench_single.out

echo ""
echo "================== РЕЗУЛЬТАТ ОДНОПОТОЧНОГО ТЕСТА =================="
echo "Целевое число операций:   $TARGET_TRANSACTIONS"
if [ "$ACTUAL_TIME" -lt "$TIMEOUT_SEC" ]; then
    echo "✅ Тест завершён досрочно за ${ACTUAL_TIME} сек"
else
    echo "⚠️ Тест прерван по таймауту (${TIMEOUT_SEC} сек), целевое не достигнуто"
fi
echo "Выполнено операций:       $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Средняя производительность: $TPS транз/сек"
fi
echo "================================================================"#!/bin/bash
set -e

DB_NAME="testdb"
TABLE_NAME="test_data"
TIMEOUT_SEC=300

echo "=== Однопоточный тест (1 поток, 5 минут) ==="
echo "Проверяем производительность ядра CPU..."

MAX_ID=$(sudo -u postgres psql -d "$DB_NAME" -t -c "SELECT MAX(id) FROM $TABLE_NAME;" | xargs)
if [ -z "$MAX_ID" ] || [ "$MAX_ID" -eq 0 ]; then
    echo "Ошибка: таблица $TABLE_NAME пуста или не существует."
    echo "Запустите сначала: sudo ./01_setup_db.sh"
    exit 1
fi

# Создаём файл скрипта с доступом postgres
TXN_FILE="/tmp/pgbench_single_$$.sql"
cat > "$TXN_FILE" <<EOF
\set id random(1, $MAX_ID)
UPDATE $TABLE_NAME
SET col4 = col1 + col2 + col3,
    col5 = col1::TEXT || col2::TEXT || col3::TEXT
WHERE id = :id;
EOF
chmod 644 "$TXN_FILE"

echo "Запуск pgbench (прогресс каждые 10 сек, тест до ${TIMEOUT_SEC} сек)..."
sudo -u postgres pgbench -d "$DB_NAME" \
    -f "$TXN_FILE" \
    -c 1 -j 1 \
    -T $TIMEOUT_SEC \
    -P 10 \
    -n 2>&1 | tee /tmp/pgbench_single.out

TPS=$(grep -oP 'tps = \K[0-9.]+' /tmp/pgbench_single.out | head -1)
TRANSACTIONS=$(grep -oP 'number of transactions actually processed: \K[0-9]+' /tmp/pgbench_single.out)
ACTUAL_TIME=$(grep -oP 'duration: \K[0-9]+' /tmp/pgbench_single.out)

if [ -z "$ACTUAL_TIME" ]; then
    ACTUAL_TIME=$TIMEOUT_SEC
fi

rm -f "$TXN_FILE" /tmp/pgbench_single.out

echo ""
echo "================== РЕЗУЛЬТАТ ОДНОПОТОЧНОГО ТЕСТА =================="
echo "Время выполнения:        ${ACTUAL_TIME} сек (лимит ${TIMEOUT_SEC} сек)"
echo "Всего операций (UPDATE): $TRANSACTIONS"
if [ -n "$TPS" ]; then
    echo "Средняя производительность: $TPS транзакций в секунду"
else
    echo "Не удалось вычислить TPS (возможно, тест не завершился корректно)"
fi
echo "==================================================================="
