#!/bin/bash
set -e

# Проверка аргументов
if [ $# -ne 2 ]; then
    echo "Использование: $0 <минуты> <потоки>"
    echo "Пример: $0 10 4   # 10 минут, 4 потока"
    exit 1
fi

MINUTES=$1
THREADS=$2
DURATION_SEC=$((MINUTES * 60))
DB_NAME="testdb"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Проверка прав (требуется sudo для sar)
if [ "$EUID" -ne 0 ]; then
    echo "Пожалуйста, запустите с sudo: sudo $0 $*"
    exit 1
fi

# Определение диска, на котором лежит PGDATA
PGDATA_DIR=$(sudo -u postgres psql -t -c "SHOW data_directory;" | tr -d ' ')
DISK_DEV=$(df -P "$PGDATA_DIR" | tail -1 | awk '{print $1}' | sed 's/[0-9]*$//')
if [ -z "$DISK_DEV" ]; then
    echo "Не удалось определить диск для PGDATA, используем всё устройство."
    DISK_DEV=""
fi

echo "=== Нагрузочное тестирование ==="
echo "Длительность: ${MINUTES} мин (${DURATION_SEC} сек)"
echo "Количество потоков: ${THREADS}"
echo "Диск мониторинга: ${DISK_DEV:-все}"
echo "Временная метка: $TIMESTAMP"

# Файлы для логов sar
SAR_CPU_LOG="/tmp/sar_cpu_${TIMESTAMP}.bin"
SAR_MEM_LOG="/tmp/sar_mem_${TIMESTAMP}.bin"
SAR_DISK_LOG="/tmp/sar_disk_${TIMESTAMP}.bin"

# Запуск сбора метрик в фоне
sar -u -o "$SAR_CPU_LOG" 1 >/dev/null 2>&1 &
PID_SAR_CPU=$!
sar -r -o "$SAR_MEM_LOG" 1 >/dev/null 2>&1 &
PID_SAR_MEM=$!
sar -d -p -o "$SAR_DISK_LOG" 1 >/dev/null 2>&1 &
PID_SAR_DISK=$!

# Функция остановки сбора
stop_metrics() {
    kill $PID_SAR_CPU $PID_SAR_MEM $PID_SAR_DISK 2>/dev/null
    wait $PID_SAR_CPU $PID_SAR_MEM $PID_SAR_DISK 2>/dev/null
    sleep 1
}

# Запуск pgbench
echo "Запуск pgbench..."
PGBENCH_OUT="/tmp/pgbench_${TIMESTAMP}.out"
sudo -u postgres pgbench -c "$THREADS" -j "$THREADS" -T "$DURATION_SEC" -P 60 -r "$DB_NAME" > "$PGBENCH_OUT" 2>&1
PGBENCH_EXIT=$?

# Останавливаем сбор метрик
stop_metrics

if [ $PGBENCH_EXIT -ne 0 ]; then
    echo "Ошибка выполнения pgbench. Код: $PGBENCH_EXIT"
    cat "$PGBENCH_OUT"
    exit $PGBENCH_EXIT
fi

# === Формирование отчёта ===
echo ""
echo "================== ОТЧЁТ О ТЕСТИРОВАНИИ =================="
echo "Длительность: $MINUTES мин, потоков: $THREADS"
echo "-----------------------------------------------------------"

# 1. Результаты pgbench (TPS и задержки)
echo "--- Результаты pgbench ---"
grep -E "tps =|latency average|latency stddev" "$PGBENCH_OUT" | head -5
echo ""

# 2. CPU (средние и максимальные)
echo "--- Загрузка CPU ---"
# Средние значения (среднее за весь период)
CPU_AVG=$(sar -u -f "$SAR_CPU_LOG" | grep Average | tail -1)
if [ -n "$CPU_AVG" ]; then
    echo "Средние: $CPU_AVG"
else
    echo "Нет данных для CPU (средние)"
fi
# Максимальные значения (ищем максимум по %user, %system, %iowait)
CPU_DATA=$(sar -u -f "$SAR_CPU_LOG" | grep -v Average | grep -v Linux | grep -v "^$" | tail -n +4)
if [ -n "$CPU_DATA" ]; then
    MAX_USER=$(echo "$CPU_DATA" | awk '{print $4}' | sort -n | tail -1)
    MAX_SYSTEM=$(echo "$CPU_DATA" | awk '{print $6}' | sort -n | tail -1)
    MAX_IOWAIT=$(echo "$CPU_DATA" | awk '{print $8}' | sort -n | tail -1)
    echo "Максимальные: %user=$MAX_USER, %system=$MAX_SYSTEM, %iowait=$MAX_IOWAIT"
fi
echo ""

# 3. Память (средние и максимальные %memused)
echo "--- Использование ОЗУ ---"
MEM_AVG=$(sar -r -f "$SAR_MEM_LOG" | grep Average | tail -1)
if [ -n "$MEM_AVG" ]; then
    echo "Средние: $MEM_AVG"
else
    echo "Нет данных для памяти (средние)"
fi
MEM_DATA=$(sar -r -f "$SAR_MEM_LOG" | grep -v Average | grep -v Linux | grep -v "^$" | tail -n +4)
if [ -n "$MEM_DATA" ]; then
    MAX_MEMUSED=$(echo "$MEM_DATA" | awk '{print $6}' | sed 's/%//' | sort -n | tail -1)
    echo "Максимальное использование памяти: ${MAX_MEMUSED}%"
fi
echo ""

# 4. Диск (средние и максимальные %util, tps, rkB/s, wkB/s)
echo "--- Нагрузка на диск (${DISK_DEV:-все устройства}) ---"
# Фильтруем записи только для выбранного диска (если определён)
if [ -n "$DISK_DEV" ]; then
    DISK_DATA=$(sar -d -p -f "$SAR_DISK_LOG" | grep -w "$DISK_DEV" | grep -v Average)
    DISK_AVG=$(sar -d -p -f "$SAR_DISK_LOG" | grep Average | grep -w "$DISK_DEV")
else
    DISK_DATA=$(sar -d -p -f "$SAR_DISK_LOG" | grep -v Average | grep -v Linux | grep -v DEV)
    DISK_AVG=$(sar -d -p -f "$SAR_DISK_LOG" | grep Average)
fi
if [ -n "$DISK_AVG" ]; then
    echo "Средние: $DISK_AVG"
fi
if [ -n "$DISK_DATA" ]; then
    # tps (2-ая колонка), rkB/s (3), wkB/s (4), %util (10-ая)
    MAX_TPS=$(echo "$DISK_DATA" | awk '{print $3}' | grep -v '^$' | sort -n | tail -1)
    MAX_RKB=$(echo "$DISK_DATA" | awk '{print $4}' | grep -v '^$' | sort -n | tail -1)
    MAX_WKB=$(echo "$DISK_DATA" | awk '{print $5}' | grep -v '^$' | sort -n | tail -1)
    MAX_UTIL=$(echo "$DISK_DATA" | awk '{print $10}' | grep -v '^$' | sort -n | tail -1)
    echo "Максимальные: tps=$MAX_TPS, rkB/s=$MAX_RKB, wkB/s=$MAX_WKB, %util=$MAX_UTIL"
fi
echo ""

# Очистка временных файлов (оставляем логи при необходимости)
rm -f "$SAR_CPU_LOG" "$SAR_MEM_LOG" "$SAR_DISK_LOG"
echo "Временные файлы удалены. Лог pgbench: $PGBENCH_OUT"
echo "================== КОНЕЦ ОТЧЁТА =================="
