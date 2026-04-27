#!/bin/bash
set -e

echo "=== Установка PostgreSQL и инструментов для тестирования (Ubuntu 24.04) ==="
sudo apt update
sudo apt install -y postgresql postgresql-contrib sysstat bc

# Включение сбора статистики sysstat (для sar)
sudo systemctl enable sysstat
sudo systemctl start sysstat

# Включение и запуск PostgreSQL
sudo systemctl enable postgresql
sudo systemctl start postgresql

# Создание тестовой БД
DB_NAME="testdb"
if sudo -u postgres psql -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    echo "База данных $DB_NAME уже существует, удаляем и создаём заново..."
    sudo -u postgres psql -c "DROP DATABASE $DB_NAME;"
fi
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME;"

# Инициализация данных через pgbench (масштаб 100 ≈ 1.6 ГБ данных)
SCALE=${PGBENCH_SCALE:-100}
echo "Заполнение таблиц данными (масштаб $SCALE)..."
sudo -u postgres pgbench -i -s "$SCALE" "$DB_NAME"

echo "=== Подготовка завершена. ==="
echo "Тестовая БД: $DB_NAME"
echo "Для нагрузочного тестирования используйте скрипт Ubuntu_run.sh"
