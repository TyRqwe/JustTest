# JustTest — бенч и проверка целостности PostgreSQL

Набор скриптов включает:

1. **`01_setup.py`** — поставить PostgreSQL и поднять тестовую БД `testdb`.
2. **`02_bench.py`** — time-bounded бенч (TPS + p50/p95/p99 latency) с снапшотом окружения и JSON-отчётом.
3. **`03_integrity_check.py`** — проверка целостности через `amcheck` (`bt_index_check`, `verify_heapam`) + `checksum_failures`.
4. **`04_corruption_test.py`** — намеренно портит файл индекса, чтобы убедиться, что `03_*` реально ловит повреждения.

Работает на Ubuntu/Debian. Заточен на чистую тестовую ВМ.

## Быстрый старт

```bash
git clone https://github.com/TyRqwe/JustTest/
cd JustTest

sudo python3 01_setup.py
sudo python3 02_bench.py
```

Опционально:

```bash
sudo python3 03_integrity_check.py
sudo I_UNDERSTAND_THIS_DESTROYS_DATA=yes python3 04_corruption_test.py
```

## Безопасность

`01_setup.py` **не** переписывает `pg_hba.conf` в `trust`. Все обращения к postgres идут как `sudo -u postgres psql ...` через unix-сокет — это дефолтная `peer`-аутентификация и она безопасна даже на ВМ с публичным IP.

`04_corruption_test.py` деструктивный: он намеренно портит relfilenode индекса. Запускать **только** на одноразовой ВМ. Скрипт сам ничего не делает, пока не выставлено `I_UNDERSTAND_THIS_DESTROYS_DATA=yes`.

## Переменные окружения

### `02_bench.py`

| Переменная | Дефолт | Что делает |
|---|---|---|
| `TEST_DURATION` | `120` | Длительность каждой фазы теста, сек |
| `PROBE_DURATION` | `10` | Длительность прогрева перед тестами |
| `NUM_THREADS` | `cpu_count()` | Количество воркеров в многопоточной фазе |
| `BENCH_OUTPUT_DIR` | `./reports` | Куда складывать отчёты |

Отчёт пишется в два файла:
- `bench_report_<timestamp>.txt` — человекочитаемый,
- `bench_report_<timestamp>.json` — для агрегации в Excel/Grafana.

В отчёт идёт снапшот окружения: версия PG, ключевые GUC (`shared_buffers`, `effective_cache_size`, `synchronous_commit`, `wal_compression`, `data_checksums`, ...), число vCPU и RAM ВМ. Без этого сравнение разных конфигураций ВМ не воспроизводимо.

### `03_integrity_check.py`

| Переменная | Дефолт | Что делает |
|---|---|---|
| `CHECK_HEAP` | `0` | `1`/`yes` включает `verify_heapam` (часы IO на больших БД) |
| `CHECK_INDEX_LEVEL` | `simple` | `simple` = `bt_index_check`, `parent` = `bt_index_parent_check` (AccessExclusiveLock!) |
| `EXCLUDE_DATABASES` | `template0,template1,postgres` | Через запятую |
| `REPORT_DIR` | `/var/log/postgresql` | Куда писать отчёт |

Отчёт пишется в `integrity_report_<timestamp>.txt` (история сохраняется, файл не перезаписывается). Метрики для Zabbix — в `zabbix_metrics.json` с фиксированным именем для удобного поллинга.

**Важно про `checksum_failures`:** этот счётчик имеет смысл только при включённых page checksums. Скрипт явно показывает `data_checksums` в начале запуска. Если `off` — включить можно только пересозданием кластера через `initdb --data-checksums` или `pg_checksums --enable` на остановленной БД.

### `04_corruption_test.py`

| Переменная | Дефолт | Что делает |
|---|---|---|
| `I_UNDERSTAND_THIS_DESTROYS_DATA` | — | Обязательно `yes`, иначе скрипт ничего не делает |
| `CORRUPT_DB` | `testdb` | Какую БД портить |
| `CORRUPT_TABLE` | `test_data` | На какой таблице |
| `CORRUPT_INDEX` | `test_data_pkey` | Какой индекс портим |

Бэкап relfilenode складывается в `/var/tmp/corruption_backup_<ts>/` и **не удаляется автоматически** — это последняя страховка на случай, если автоматическое восстановление сломается.
