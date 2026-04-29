#!/usr/bin/env python3
import sys
import time
import multiprocessing as mp
import psutil
import psycopg2
import os
from threading import Thread, Event

DB_NAME = "testdb"
TABLE_NAME = "test_data"
TEST_DURATION = 120
PROBE_DURATION = 10          # длительность прогревочного теста (сек)
NUM_THREADS = os.cpu_count() or 2

def get_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user="postgres",
        host="/var/run/postgresql"
    )

# ---------- Вспомогательные функции для подбора ----------
def probe_update_tps(duration):
    """Запускает краткий UPDATE-тест и возвращает TPS."""
    conn = get_connection()
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(f"SELECT MAX(id) FROM {TABLE_NAME}")
    max_id = cur.fetchone()[0]
    start = time.time()
    count = 0
    while time.time() - start < duration:
        cur.execute(f"""
            UPDATE {TABLE_NAME}
            SET col4 = col1 + col2 + col3,
                col5 = col1::TEXT || col2::TEXT || col3::TEXT
            WHERE id = floor(random() * {max_id}) + 1
        """)
        conn.commit()
        count += 1
    elapsed = time.time() - start
    cur.close()
    conn.close()
    return count / elapsed

def probe_aggregation_tps(duration, threads=1):
    """Запускает краткий агрегационный тест (в один поток) и возвращает TPS."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(id) FROM {TABLE_NAME}")
        max_id = cur.fetchone()[0]
    conn.close()

    def worker(result_queue):
        conn = get_connection()
        conn.autocommit = True
        cur = conn.cursor()
        count = 0
        start = time.time()
        while time.time() - start < duration:
            cur.execute(f"""
                SELECT col1, SUM(col2), AVG(col3), COUNT(*)
                FROM {TABLE_NAME}
                WHERE id <= {max_id}
                GROUP BY col1
            """)
            cur.fetchall()
            count += 1
        elapsed = time.time() - start
        cur.close()
        conn.close()
        result_queue.put(count)

    result_queue = mp.Queue()
    p = mp.Process(target=worker, args=(result_queue,))
    p.start()
    p.join()
    count = result_queue.get()
    return count / duration

def auto_choose_target():
    """Определяет целевое число операций (округлённое до сотен, от 100 до 1000)."""
    print("🔍 Автоматический подбор целевого числа операций...")
    update_tps = probe_update_tps(PROBE_DURATION)
    agg_tps = probe_aggregation_tps(PROBE_DURATION, threads=1)
    # Берём минимальное из двух, чтобы оба теста проходили
    min_tps = min(update_tps, agg_tps)
    # Цель: сделать ~80% от максимально возможного за 120 сек (оставляем запас)
    raw_target = int(min_tps * 80)   # tps * 80 = операций за 80 сек
    # Округляем до сотен, ограничиваем диапазон
    target = max(100, min(1000, (raw_target + 50) // 100 * 100))
    print(f"📊 Пробный UPDATE: {update_tps:.1f} TPS, агрегация: {agg_tps:.1f} TPS")
    print(f"🎯 Рекомендуемое целевое число операций: {target} (каждый тест будет выполнен максимум за ~{target/min_tps:.0f} секунд)")
    return target

# ---------- Сбор метрик (без изменений) ----------
class MetricsCollector:
    # ... (код класса MetricsCollector остаётся прежним) ...
    def __init__(self, disk_device=None):
        self.disk_device = disk_device or self._get_postgres_disk()
        self.stop_event = Event()
        self.cpu_per_core_samples = []
        self.mem_samples = []
        self.disk_samples = []

    def _get_postgres_disk(self):
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("SHOW data_directory;")
                path = cur.fetchone()[0]
            conn.close()
            for part in psutil.disk_partitions():
                if path.startswith(part.mountpoint):
                    dev = part.device
                    return dev.replace('/dev/', '')
        except:
            pass
        return None

    def _collect(self):
        while not self.stop_event.is_set():
            per_cpu = psutil.cpu_percent(interval=1, percpu=True)
            self.cpu_per_core_samples.append(per_cpu)
            mem = psutil.virtual_memory()
            self.mem_samples.append(mem.percent)
            if self.disk_device:
                try:
                    disk_io = psutil.disk_io_counters(perdisk=True).get(self.disk_device)
                    if disk_io:
                        self.disk_samples.append((disk_io.read_bytes, disk_io.write_bytes))
                except:
                    pass
            time.sleep(1)

    def start(self):
        self.thread = Thread(target=self._collect, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def get_stats(self):
        num_cores = len(self.cpu_per_core_samples[0]) if self.cpu_per_core_samples else 0
        core_avgs = [0]*num_cores
        core_maxs = [0]*num_cores
        for sample in self.cpu_per_core_samples:
            for i, val in enumerate(sample):
                core_avgs[i] += val
                if val > core_maxs[i]:
                    core_maxs[i] = val
        if self.cpu_per_core_samples:
            core_avgs = [v/len(self.cpu_per_core_samples) for v in core_avgs]
        mem_avg = sum(self.mem_samples)/len(self.mem_samples) if self.mem_samples else 0
        mem_max = max(self.mem_samples) if self.mem_samples else 0
        disk_read_rates = []
        disk_write_rates = []
        for i in range(1, len(self.disk_samples)):
            read_rate = (self.disk_samples[i][0] - self.disk_samples[i-1][0]) / 1.0
            write_rate = (self.disk_samples[i][1] - self.disk_samples[i-1][1]) / 1.0
            if read_rate >= 0:
                disk_read_rates.append(read_rate / 1024)
            if write_rate >= 0:
                disk_write_rates.append(write_rate / 1024)
        disk_read_avg = sum(disk_read_rates)/len(disk_read_rates) if disk_read_rates else 0
        disk_read_max = max(disk_read_rates) if disk_read_rates else 0
        disk_write_avg = sum(disk_write_rates)/len(disk_write_rates) if disk_write_rates else 0
        disk_write_max = max(disk_write_rates) if disk_write_rates else 0
        return {
            'core_avgs': core_avgs,
            'core_maxs': core_maxs,
            'mem_avg': mem_avg,
            'mem_max': mem_max,
            'disk_read_avg_kb': disk_read_avg,
            'disk_read_max_kb': disk_read_max,
            'disk_write_avg_kb': disk_write_avg,
            'disk_write_max_kb': disk_write_max,
        }

# ---------- Однопоточный тест (фиксированное количество операций) ----------
def single_thread_worker(target, result_queue):
    try:
        conn = get_connection()
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(f"SELECT MAX(id) FROM {TABLE_NAME}")
        max_id = cur.fetchone()[0]
        count = 0
        start = time.time()
        while count < target:
            cur.execute(f"""
                UPDATE {TABLE_NAME}
                SET col4 = col1 + col2 + col3,
                    col5 = col1::TEXT || col2::TEXT || col3::TEXT
                WHERE id = floor(random() * {max_id}) + 1
            """)
            conn.commit()
            count += 1
        elapsed = time.time() - start
        cur.close()
        conn.close()
        result_queue.put(('ok', count, elapsed))
    except Exception as e:
        result_queue.put(('error', str(e)))

def run_single_thread(target, timeout=TEST_DURATION):
    print(f"\n=== Однопоточный тест (UPDATE, цель: {target} операций, таймаут {timeout} сек) ===")
    result_queue = mp.Queue()
    p = mp.Process(target=single_thread_worker, args=(target, result_queue))
    p.start()
    p.join(timeout=timeout)
    if p.is_alive():
        p.terminate()
        p.join()
        print(f"⚠️ Тест прерван по таймауту (>{timeout} сек), цель не достигнута")
        return {'status': 'timeout', 'transactions': 0, 'elapsed': timeout, 'tps': 0}
    else:
        result = result_queue.get()
        if result[0] == 'ok':
            _, transactions, elapsed = result
            print(f"✅ Завершён за {elapsed:.2f} сек")
            return {'status': 'ok', 'transactions': transactions, 'elapsed': elapsed, 'tps': transactions/elapsed}
        else:
            print(f"❌ Ошибка: {result[1]}")
            return {'status': 'error', 'transactions': 0, 'elapsed': 0, 'tps': 0}

# ---------- Многопоточный тест (фиксированное количество операций) ----------
def multi_worker(worker_id, target_per_worker, result_queue, max_id):
    try:
        conn = get_connection()
        conn.autocommit = True
        cur = conn.cursor()
        count = 0
        start = time.time()
        while count < target_per_worker:
            cur.execute(f"""
                SELECT col1, SUM(col2), AVG(col3), COUNT(*)
                FROM {TABLE_NAME}
                WHERE id <= {max_id}
                GROUP BY col1
            """)
            cur.fetchall()
            count += 1
        elapsed = time.time() - start
        cur.close()
        conn.close()
        result_queue.put(('ok', worker_id, count, elapsed))
    except Exception as e:
        result_queue.put(('error', worker_id, str(e)))

def run_multi_thread(target, threads=NUM_THREADS, timeout=TEST_DURATION):
    print(f"\n=== Многопоточный тест (агрегация, цель: {target} запросов, {threads} потоков, таймаут {timeout} сек) ===")
    target_per_worker = (target + threads - 1) // threads
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(id) FROM {TABLE_NAME}")
        max_id = cur.fetchone()[0]
    conn.close()
    result_queue = mp.Queue()
    processes = []
    for i in range(threads):
        p = mp.Process(target=multi_worker, args=(i, target_per_worker, result_queue, max_id))
        processes.append(p)
        p.start()
    for p in processes:
        p.join(timeout=timeout)
        if p.is_alive():
            p.terminate()
            p.join()
    total_transactions = 0
    max_elapsed = 0
    errors = []
    while not result_queue.empty():
        item = result_queue.get_nowait()
        if item[0] == 'ok':
            _, _, cnt, el = item
            total_transactions += cnt
            if el > max_elapsed:
                max_elapsed = el
        else:
            errors.append(f"Worker {item[1]}: {item[2]}")
    if errors:
        print("❌ Ошибки в потоках:")
        for err in errors:
            print(f"  {err}")
    if total_transactions >= target:
        status = 'ok'
        elapsed = max_elapsed
        print(f"✅ Завершён за {elapsed:.2f} сек")
    else:
        status = 'timeout'
        elapsed = timeout
        print(f"⚠️ Прерван по таймауту, выполнено {total_transactions} из {target}")
    tps = total_transactions / elapsed if elapsed > 0 else 0
    return {
        'status': status,
        'transactions': total_transactions,
        'elapsed': elapsed,
        'tps': tps,
        'threads': threads
    }

# ---------- Основная программа ----------
def main():
    if os.geteuid() != 0:
        print("❌ Запустите с sudo: sudo python3 bench.py")
        sys.exit(1)

    try:
        conn = get_connection()
        conn.close()
        print("✅ Подключение к БД успешно")
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        sys.exit(1)

    # Определяем целевое число операций (если не задано окружением)
    target = os.environ.get('TARGET_TRANSACTIONS')
    if target is None:
        target = auto_choose_target()
        print(f"Используется цель: {target} операций. (Чтобы переопределить, установите TARGET_TRANSACTIONS)")
    else:
        target = int(target)
        print(f"Цель задана вручную: {target} операций")

    # Однопоточный тест
    collector = MetricsCollector()
    collector.start()
    single_res = run_single_thread(target)
    collector.stop()
    single_metrics = collector.get_stats()

    # Многопоточный тест
    collector2 = MetricsCollector()
    collector2.start()
    multi_res = run_multi_thread(target)
    collector2.stop()
    multi_metrics = collector2.get_stats()

    # Отчёт
    print("\n" + "="*80)
    print("ИТОГОВЫЙ ОТЧЁТ".center(80))
    print("="*80)

    print("\n--- Однопоточный тест (UPDATE) ---")
    print(f"Цель: {target} операций")
    if single_res['status'] == 'ok':
        print(f"Результат: завершён за {single_res['elapsed']:.2f} сек, выполнено {single_res['transactions']} операций")
    elif single_res['status'] == 'timeout':
        print(f"Результат: таймаут (>{single_res['elapsed']} сек), выполнено {single_res['transactions']} операций")
    else:
        print(f"Результат: ошибка")
    print(f"TPS: {single_res['tps']:.2f}")
    print(f"RAM: средняя {single_metrics['mem_avg']:.1f}%, макс {single_metrics['mem_max']:.1f}%")
    print(f"Диск чтение (KB/s): средний {single_metrics['disk_read_avg_kb']:.0f}, макс {single_metrics['disk_read_max_kb']:.0f}")
    print(f"Диск запись (KB/s): средний {single_metrics['disk_write_avg_kb']:.0f}, макс {single_metrics['disk_write_max_kb']:.0f}")
    print("Загрузка CPU по ядрам:")
    for i, (avg, maxv) in enumerate(zip(single_metrics['core_avgs'], single_metrics['core_maxs'])):
        print(f"  Ядро {i}: средняя {avg:.1f}%, макс {maxv:.1f}%")

    print("\n--- Многопоточный тест (агрегация) ---")
    print(f"Потоков: {multi_res['threads']}, цель: {target} запросов")
    if multi_res['status'] == 'ok':
        print(f"Результат: завершён за {multi_res['elapsed']:.2f} сек, выполнено {multi_res['transactions']} операций")
    elif multi_res['status'] == 'timeout':
        print(f"Результат: таймаут (>{multi_res['elapsed']} сек), выполнено {multi_res['transactions']} операций")
    else:
        print(f"Результат: ошибка")
    print(f"Суммарный TPS: {multi_res['tps']:.2f}")
    if multi_res['threads'] > 0:
        print(f"TPS на поток: {multi_res['tps']/multi_res['threads']:.2f}")
    print(f"RAM: средняя {multi_metrics['mem_avg']:.1f}%, макс {multi_metrics['mem_max']:.1f}%")
    print(f"Диск чтение (KB/s): средний {multi_metrics['disk_read_avg_kb']:.0f}, макс {multi_metrics['disk_read_max_kb']:.0f}")
    print(f"Диск запись (KB/s): средний {multi_metrics['disk_write_avg_kb']:.0f}, макс {multi_metrics['disk_write_max_kb']:.0f}")
    print("Загрузка CPU по ядрам:")
    for i, (avg, maxv) in enumerate(zip(multi_metrics['core_avgs'], multi_metrics['core_maxs'])):
        print(f"  Ядро {i}: средняя {avg:.1f}%, макс {maxv:.1f}%")

    print("\n" + "="*80)

if __name__ == "__main__":
    mp.set_start_method('fork', force=True)
    main()
