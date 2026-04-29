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
TARGET_TRANSACTIONS = 1000
TIMEOUT = 120              # секунд
NUM_THREADS = os.cpu_count() or 2

def get_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user="postgres",
        host="/var/run/postgresql"
    )

# Сбор метрик с отдельными ядрами CPU
class MetricsCollector:
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

# ----- Однопоточный тест (UPDATE) -----
def single_thread_worker(target, result_queue):
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
    result_queue.put((count, elapsed))

def run_single_thread():
    print(f"\n=== Однопоточный тест (UPDATE, цель: {TARGET_TRANSACTIONS} операций, таймаут {TIMEOUT} сек) ===")
    result_queue = mp.Queue()
    p = mp.Process(target=single_thread_worker, args=(TARGET_TRANSACTIONS, result_queue))
    p.start()
    p.join(timeout=TIMEOUT)
    if p.is_alive():
        p.terminate()
        p.join()
        # В этом упрощённом варианте мы не знаем, сколько выполнено.
        # Для точного подсчёта потребовался бы более сложный механизм.
        # Предполагаем, что при таймауте не выполнено ничего (можно доработать).
        print(f"⚠️ Тест прерван по таймауту (>{TIMEOUT} сек), цель не достигнута")
        return {'status': 'timeout', 'transactions': 0, 'elapsed': TIMEOUT, 'tps': 0}
    else:
        transactions, elapsed = result_queue.get()
        print(f"✅ Завершён за {elapsed:.2f} сек")
        return {
            'status': 'ok',
            'transactions': transactions,
            'elapsed': elapsed,
            'tps': transactions / elapsed
        }

# ----- Многопоточный тест (агрегация) -----
def multi_worker(worker_id, target_per_worker, result_queue, max_id):
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
    result_queue.put((count, elapsed))

def run_multi_thread():
    print(f"\n=== Многопоточный тест (агрегация, цель: {TARGET_TRANSACTIONS} запросов, {NUM_THREADS} потоков, таймаут {TIMEOUT} сек) ===")
    target_per_worker = (TARGET_TRANSACTIONS + NUM_THREADS - 1) // NUM_THREADS
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(id) FROM {TABLE_NAME}")
        max_id = cur.fetchone()[0]
    conn.close()
    result_queue = mp.Queue()
    processes = []
    for i in range(NUM_THREADS):
        p = mp.Process(target=multi_worker, args=(i, target_per_worker, result_queue, max_id))
        processes.append(p)
        p.start()
    # Ждём завершения или таймаута
    for p in processes:
        p.join(timeout=TIMEOUT)
        if p.is_alive():
            p.terminate()
            p.join()
    total_transactions = 0
    max_elapsed = 0
    for _ in range(len(processes)):
        try:
            cnt, el = result_queue.get_nowait()
            total_transactions += cnt
            if el > max_elapsed:
                max_elapsed = el
        except:
            pass
    if total_transactions >= TARGET_TRANSACTIONS:
        status = 'ok'
        elapsed = max_elapsed
        print(f"✅ Завершён за {elapsed:.2f} сек")
    else:
        status = 'timeout'
        elapsed = TIMEOUT
        print(f"⚠️ Прерван по таймауту, выполнено {total_transactions} из {TARGET_TRANSACTIONS}")
    tps = total_transactions / elapsed if elapsed > 0 else 0
    return {
        'status': status,
        'transactions': total_transactions,
        'elapsed': elapsed,
        'tps': tps,
        'threads': NUM_THREADS
    }

# ----- Основная программа -----
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

    # Однопоточный тест
    collector = MetricsCollector()
    collector.start()
    single_res = run_single_thread()
    collector.stop()
    single_metrics = collector.get_stats()

    # Многопоточный тест
    collector2 = MetricsCollector()
    collector2.start()
    multi_res = run_multi_thread()
    collector2.stop()
    multi_metrics = collector2.get_stats()

    # Вывод отчёта
    print("\n" + "="*80)
    print("ИТОГОВЫЙ ОТЧЁТ".center(80))
    print("="*80)

    print("\n--- Однопоточный тест (UPDATE) ---")
    print(f"Цель: {TARGET_TRANSACTIONS} операций")
    if single_res['status'] == 'ok':
        print(f"Результат: завершён за {single_res['elapsed']:.2f} сек, выполнено {single_res['transactions']} операций")
    else:
        print(f"Результат: таймаут ({TIMEOUT} сек), выполнено {single_res['transactions']} операций")
    print(f"TPS: {single_res['tps']:.2f}")
    print(f"RAM: средняя {single_metrics['mem_avg']:.1f}%, макс {single_metrics['mem_max']:.1f}%")
    print(f"Диск чтение (KB/s): средний {single_metrics['disk_read_avg_kb']:.0f}, макс {single_metrics['disk_read_max_kb']:.0f}")
    print(f"Диск запись (KB/s): средний {single_metrics['disk_write_avg_kb']:.0f}, макс {single_metrics['disk_write_max_kb']:.0f}")
    print("Загрузка CPU по ядрам:")
    for i, (avg, maxv) in enumerate(zip(single_metrics['core_avgs'], single_metrics['core_maxs'])):
        print(f"  Ядро {i}: средняя {avg:.1f}%, макс {maxv:.1f}%")

    print("\n--- Многопоточный тест (агрегация) ---")
    print(f"Потоков: {multi_res['threads']}, цель: {TARGET_TRANSACTIONS} запросов")
    if multi_res['status'] == 'ok':
        print(f"Результат: завершён за {multi_res['elapsed']:.2f} сек, выполнено {multi_res['transactions']} операций")
    else:
        print(f"Результат: таймаут ({TIMEOUT} сек), выполнено {multi_res['transactions']} операций")
    print(f"Суммарный TPS: {multi_res['tps']:.2f}")
    if multi_res['threads']:
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
