#!/usr/bin/env python3
"""
Бенч PostgreSQL для сравнения конфигураций ВМ.

Тест time-bounded: фиксированное окно TEST_DURATION секунд, измеряем
сколько транзакций успели, средний TPS и распределение латентности.
"""
import sys
import time
import multiprocessing as mp
import psutil
import psycopg2
import os
import json
import platform
import statistics
import subprocess
from datetime import datetime
from threading import Thread, Event

DB_NAME = "testdb"
TABLE_NAME = "test_data"
TEST_DURATION = int(os.environ.get("TEST_DURATION", "120"))
PROBE_DURATION = int(os.environ.get("PROBE_DURATION", "10"))
NUM_THREADS = int(os.environ.get("NUM_THREADS", str(os.cpu_count() or 2)))
OUTPUT_DIR = os.environ.get("BENCH_OUTPUT_DIR", "./reports")


def get_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user="postgres",
        host="/var/run/postgresql",
    )


# ---------- Снапшот окружения ----------
def collect_environment():
    """Собирает версию PG, ключевые GUC и параметры ВМ. Уходит в отчёт."""
    env = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "host": {
            "cpu_count": os.cpu_count(),
            "ram_total_mb": int(psutil.virtual_memory().total / 1024 / 1024),
            "kernel": platform.release(),
            "platform": platform.platform(),
        },
        "postgres": {},
    }
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            env["postgres"]["version"] = cur.fetchone()[0]
            # Эти GUC напрямую влияют на TPS — без них сравнение конфигов невоспроизводимо.
            guc_names = [
                "shared_buffers", "effective_cache_size", "work_mem",
                "maintenance_work_mem", "synchronous_commit",
                "wal_compression", "max_connections", "data_checksums",
                "max_wal_size", "checkpoint_timeout",
            ]
            for name in guc_names:
                cur.execute(f"SHOW {name}")
                env["postgres"][name] = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        env["postgres"]["error"] = str(e)
    return env


# ---------- Управление автоваккумом и кэшем ----------
def disable_autovacuum_on_table():
    conn = get_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {TABLE_NAME} SET (autovacuum_enabled = false)")
    conn.close()


def enable_autovacuum_on_table():
    conn = get_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {TABLE_NAME} RESET (autovacuum_enabled)")
    conn.close()


def vacuum_analyze():
    conn = get_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"VACUUM (ANALYZE) {TABLE_NAME}")
    conn.close()


# ---------- Сбор метрик ----------
class MetricsCollector:
    def __init__(self, disk_device=None):
        self.disk_device = disk_device or self._get_postgres_disk()
        self.stop_event = Event()
        self.cpu_per_core_samples = []
        self.mem_samples = []
        self.pg_rss_samples = []
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
                    return part.device.replace("/dev/", "")
        except Exception:
            pass
        return None

    def _postgres_rss_mb(self):
        # Суммарный RSS всех процессов с именем 'postgres' — это то,
        # что съел собственно сервер, в отличие от virtual_memory().percent,
        # где смешано всё на ВМ.
        total = 0
        for proc in psutil.process_iter(["name", "memory_info"]):
            try:
                if proc.info["name"] == "postgres":
                    total += proc.info["memory_info"].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total / 1024 / 1024

    def _collect(self):
        while not self.stop_event.is_set():
            # cpu_percent(interval=1) сам блокируется на 1 сек — отдельный
            # sleep(1) здесь не нужен, иначе сэмпл-рейт уезжает в 0.5 Гц.
            per_cpu = psutil.cpu_percent(interval=1, percpu=True)
            self.cpu_per_core_samples.append(per_cpu)
            self.mem_samples.append(psutil.virtual_memory().percent)
            self.pg_rss_samples.append(self._postgres_rss_mb())
            if self.disk_device:
                try:
                    disk_io = psutil.disk_io_counters(perdisk=True).get(self.disk_device)
                    if disk_io:
                        self.disk_samples.append((disk_io.read_bytes, disk_io.write_bytes))
                except Exception:
                    pass

    def start(self):
        self.thread = Thread(target=self._collect, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def get_stats(self):
        num_cores = len(self.cpu_per_core_samples[0]) if self.cpu_per_core_samples else 0
        core_avgs = [0.0] * num_cores
        core_maxs = [0.0] * num_cores
        for sample in self.cpu_per_core_samples:
            for i, val in enumerate(sample):
                core_avgs[i] += val
                if val > core_maxs[i]:
                    core_maxs[i] = val
        if self.cpu_per_core_samples:
            core_avgs = [v / len(self.cpu_per_core_samples) for v in core_avgs]

        def safe_avg(xs):
            return sum(xs) / len(xs) if xs else 0
        def safe_max(xs):
            return max(xs) if xs else 0

        disk_read_rates = []
        disk_write_rates = []
        for i in range(1, len(self.disk_samples)):
            r = (self.disk_samples[i][0] - self.disk_samples[i - 1][0])
            w = (self.disk_samples[i][1] - self.disk_samples[i - 1][1])
            if r >= 0:
                disk_read_rates.append(r / 1024)
            if w >= 0:
                disk_write_rates.append(w / 1024)

        return {
            "core_avgs": core_avgs,
            "core_maxs": core_maxs,
            "mem_avg": safe_avg(self.mem_samples),
            "mem_max": safe_max(self.mem_samples),
            "pg_rss_avg_mb": safe_avg(self.pg_rss_samples),
            "pg_rss_max_mb": safe_max(self.pg_rss_samples),
            "disk_read_avg_kb": safe_avg(disk_read_rates),
            "disk_read_max_kb": safe_max(disk_read_rates),
            "disk_write_avg_kb": safe_avg(disk_write_rates),
            "disk_write_max_kb": safe_max(disk_write_rates),
        }


# ---------- Утилиты для latency ----------
def latency_summary(latencies_ms):
    if not latencies_ms:
        return {"count": 0}
    s = sorted(latencies_ms)
    out = {
        "count": len(s),
        "avg_ms": sum(s) / len(s),
        "min_ms": s[0],
        "max_ms": s[-1],
    }
    # statistics.quantiles(n=100) даёт 99 значений (точки между сегментами);
    # 49-я ≈ p50, 94-я ≈ p95, 98-я ≈ p99.
    if len(s) >= 100:
        q = statistics.quantiles(s, n=100)
        out["p50_ms"] = q[49]
        out["p95_ms"] = q[94]
        out["p99_ms"] = q[98]
    else:
        out["p50_ms"] = s[len(s) // 2]
        out["p95_ms"] = s[int(len(s) * 0.95)] if s else 0
        out["p99_ms"] = s[int(len(s) * 0.99)] if s else 0
    return out


# ---------- Однопоточный UPDATE-тест ----------
def single_thread_worker(duration, result_queue):
    try:
        conn = get_connection()
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(f"SELECT MAX(id) FROM {TABLE_NAME}")
        max_id = cur.fetchone()[0]
        latencies_ms = []
        start = time.time()
        while time.time() - start < duration:
            t0 = time.perf_counter()
            cur.execute(f"""
                UPDATE {TABLE_NAME}
                SET col4 = col1 + col2 + col3,
                    col5 = col1::TEXT || col2::TEXT || col3::TEXT
                WHERE id = floor(random() * {max_id}) + 1
            """)
            conn.commit()
            latencies_ms.append((time.perf_counter() - t0) * 1000)
        elapsed = time.time() - start
        cur.close()
        conn.close()
        result_queue.put(("ok", latencies_ms, elapsed))
    except Exception as e:
        result_queue.put(("error", str(e)))


def run_single_thread(duration=TEST_DURATION):
    print(f"\n=== Однопоточный тест (UPDATE, окно {duration} сек) ===")
    result_queue = mp.Queue()
    p = mp.Process(target=single_thread_worker, args=(duration, result_queue))
    p.start()
    p.join(timeout=duration + 30)
    if p.is_alive():
        p.terminate()
        p.join()
        print(f"⚠️ Тест завис, прерван")
        return {"status": "hang", "transactions": 0, "elapsed": duration, "tps": 0, "latency": {}}
    result = result_queue.get()
    if result[0] == "ok":
        _, latencies_ms, elapsed = result
        print(f"✅ Завершён за {elapsed:.2f} сек, {len(latencies_ms)} транзакций")
        return {
            "status": "ok",
            "transactions": len(latencies_ms),
            "elapsed": elapsed,
            "tps": len(latencies_ms) / elapsed if elapsed > 0 else 0,
            "latency": latency_summary(latencies_ms),
        }
    print(f"❌ Ошибка: {result[1]}")
    return {"status": "error", "error": result[1]}


# ---------- Многопоточный SELECT-тест ----------
def multi_worker(worker_id, duration, result_queue, max_id):
    try:
        conn = get_connection()
        conn.autocommit = True
        cur = conn.cursor()
        latencies_ms = []
        start = time.time()
        while time.time() - start < duration:
            t0 = time.perf_counter()
            cur.execute(f"""
                SELECT col1, SUM(col2), AVG(col3), COUNT(*)
                FROM {TABLE_NAME}
                WHERE id <= {max_id}
                GROUP BY col1
            """)
            cur.fetchall()
            latencies_ms.append((time.perf_counter() - t0) * 1000)
        elapsed = time.time() - start
        cur.close()
        conn.close()
        result_queue.put(("ok", worker_id, latencies_ms, elapsed))
    except Exception as e:
        result_queue.put(("error", worker_id, str(e)))


def run_multi_thread(threads=NUM_THREADS, duration=TEST_DURATION):
    print(f"\n=== Многопоточный тест (агрегация, {threads} потоков, окно {duration} сек) ===")
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(id) FROM {TABLE_NAME}")
        max_id = cur.fetchone()[0]
    conn.close()
    result_queue = mp.Queue()
    processes = []
    for i in range(threads):
        p = mp.Process(target=multi_worker, args=(i, duration, result_queue, max_id))
        processes.append(p)
        p.start()
    for p in processes:
        p.join(timeout=duration + 30)
        if p.is_alive():
            p.terminate()
            p.join()

    all_latencies = []
    total_transactions = 0
    max_elapsed = 0.0
    errors = []
    while not result_queue.empty():
        item = result_queue.get_nowait()
        if item[0] == "ok":
            _, _, latencies_ms, el = item
            all_latencies.extend(latencies_ms)
            total_transactions += len(latencies_ms)
            if el > max_elapsed:
                max_elapsed = el
        else:
            errors.append(f"Worker {item[1]}: {item[2]}")
    if errors:
        print("❌ Ошибки в потоках:")
        for err in errors:
            print(f"  {err}")
    print(f"✅ Завершён за {max_elapsed:.2f} сек, {total_transactions} запросов")
    return {
        "status": "ok" if not errors else "partial",
        "transactions": total_transactions,
        "elapsed": max_elapsed,
        "tps": total_transactions / max_elapsed if max_elapsed > 0 else 0,
        "threads": threads,
        "latency": latency_summary(all_latencies),
        "errors": errors,
    }


# ---------- Форматирование отчётов ----------
def format_text_report(env, single_res, single_metrics, multi_res, multi_metrics):
    lines = []
    lines.append("=" * 80)
    lines.append("ИТОГОВЫЙ ОТЧЁТ".center(80))
    lines.append("=" * 80)

    lines.append("\n--- Окружение ---")
    lines.append(f"Время: {env['timestamp']}")
    lines.append(f"Платформа: {env['host']['platform']}, ядро {env['host']['kernel']}")
    lines.append(f"vCPU: {env['host']['cpu_count']}, RAM: {env['host']['ram_total_mb']} MB")
    pg = env["postgres"]
    if "error" in pg:
        lines.append(f"PostgreSQL: ошибка сбора — {pg['error']}")
    else:
        lines.append(f"PostgreSQL {pg.get('version')}")
        for k in ("shared_buffers", "effective_cache_size", "work_mem",
                  "synchronous_commit", "wal_compression", "data_checksums"):
            lines.append(f"  {k} = {pg.get(k)}")

    def section(title, res, metrics):
        lines.append(f"\n--- {title} ---")
        if res.get("status") == "ok" or res.get("status") == "partial":
            lines.append(f"Транзакций: {res['transactions']}, время: {res['elapsed']:.2f} сек")
            lines.append(f"TPS: {res['tps']:.2f}")
            lat = res.get("latency", {})
            if lat.get("count"):
                lines.append(
                    f"Latency (ms): avg {lat['avg_ms']:.2f}, "
                    f"p50 {lat['p50_ms']:.2f}, "
                    f"p95 {lat['p95_ms']:.2f}, "
                    f"p99 {lat['p99_ms']:.2f}, "
                    f"max {lat['max_ms']:.2f}"
                )
        else:
            lines.append(f"Статус: {res.get('status')} — {res.get('error', '')}")
        lines.append(f"RAM ВМ: средняя {metrics['mem_avg']:.1f}%, макс {metrics['mem_max']:.1f}%")
        lines.append(
            f"Postgres RSS: средний {metrics['pg_rss_avg_mb']:.0f} MB, "
            f"макс {metrics['pg_rss_max_mb']:.0f} MB"
        )
        lines.append(
            f"Диск чтение (KB/s): средний {metrics['disk_read_avg_kb']:.0f}, "
            f"макс {metrics['disk_read_max_kb']:.0f}"
        )
        lines.append(
            f"Диск запись (KB/s): средний {metrics['disk_write_avg_kb']:.0f}, "
            f"макс {metrics['disk_write_max_kb']:.0f}"
        )
        lines.append("Загрузка CPU по ядрам:")
        for i, (avg, mx) in enumerate(zip(metrics["core_avgs"], metrics["core_maxs"])):
            lines.append(f"  Ядро {i}: средняя {avg:.1f}%, макс {mx:.1f}%")

    section("Однопоточный тест (UPDATE)", single_res, single_metrics)
    section(f"Многопоточный тест (агрегация, {multi_res.get('threads')} потоков)",
            multi_res, multi_metrics)
    lines.append("\n" + "=" * 80)
    return "\n".join(lines)


def write_reports(env, single_res, single_metrics, multi_res, multi_metrics):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    text = format_text_report(env, single_res, single_metrics,
                              multi_res, multi_metrics)
    text_path = os.path.join(OUTPUT_DIR, f"bench_report_{stamp}.txt")
    with open(text_path, "w") as f:
        f.write(text)

    payload = {
        "environment": env,
        "single_thread": {"result": single_res, "metrics": single_metrics},
        "multi_thread": {"result": multi_res, "metrics": multi_metrics},
    }
    json_path = os.path.join(OUTPUT_DIR, f"bench_report_{stamp}.json")
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"\n📄 Отчёт сохранён: {text_path}")
    print(f"📄 JSON: {json_path}")
    print("\n" + text)


# ---------- Основная программа ----------
def main():
    if os.geteuid() != 0:
        print("❌ Запустите с sudo: sudo python3 02_bench.py")
        sys.exit(1)

    try:
        get_connection().close()
        print("✅ Подключение к БД успешно")
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        sys.exit(1)

    print(f"⚙️  TEST_DURATION={TEST_DURATION}s, NUM_THREADS={NUM_THREADS}")
    env = collect_environment()

    # Отключаем autovacuum на целевой таблице на время бенча: иначе он
    # просыпается прямо во время теста и шумит CPU/диск/латентность.
    disable_autovacuum_on_table()
    try:
        # Опционально короткий прогрев — заполнить кэш страниц.
        # Сам результат прогрева не используется, нужен только сайд-эффект.
        if PROBE_DURATION > 0:
            print(f"🔥 Прогрев {PROBE_DURATION} сек...")
            single_thread_worker(PROBE_DURATION, mp.Queue())

        # Предсказуемое состояние перед каждой фазой.
        vacuum_analyze()

        collector = MetricsCollector()
        collector.start()
        single_res = run_single_thread()
        collector.stop()
        single_metrics = collector.get_stats()

        vacuum_analyze()

        collector2 = MetricsCollector()
        collector2.start()
        multi_res = run_multi_thread()
        collector2.stop()
        multi_metrics = collector2.get_stats()
    finally:
        # Если упадём посреди теста — обязательно вернуть autovacuum,
        # иначе таблица навсегда останется без авто-обслуживания.
        enable_autovacuum_on_table()

    write_reports(env, single_res, single_metrics, multi_res, multi_metrics)


if __name__ == "__main__":
    # fork нужен для psycopg2-соединений в child-процессах.
    # На macOS этот код не предназначен для запуска (psycopg2 + fork = UB).
    mp.set_start_method("fork", force=True)
    main()
