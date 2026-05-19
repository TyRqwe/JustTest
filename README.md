бенч для студентов, чтобы провести сравнение производительности разных систем<br>
git clone https://github.com/TyRqwe/JustTest/<br>
sudo python3 01_setup.py<br>
sudo python3 02_bench.py<br>
<br>
Опционально: <br>
sudo python3 03_integrity_check.py<br>
Параметры для проверок:<br>
CHECK_HEAP = True/False (проверка heap-таблиц, может быть долгой)<br>
CHECK_INDEX_LEVEL = 'simple' (только bt_index_check) или 'parent' (более глубокая)<br>
EXCLUDE_DATABASES – список БД, которые не проверять (например, postgres, template0, template1)<br>
