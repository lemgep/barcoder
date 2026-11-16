# Отчёт о тестировании

Дата: 14.11.2026

Проект: **Barcode Document Sorter 1.0.0**

## 1. Среда тестирования

| Параметр | Значение |
|---|---|
| Хост | macOS 26.1, Apple Silicon (arm64) |
| Python | CPython 3.12.8 |
| Docker Engine | 29.1.3 (Colima, linux/arm64 VM) |
| Docker Compose | v5.7.1 (plugin) |
| Тестовый фреймворк | pytest 9.2.0 |
| Основные библиотеки | FastAPI 0.162.5/0.162.7, Starlette 1.10.0, SQLAlchemy 2.1.8, PyMuPDF 1.28.2, zxing-cpp 3.1.1, Pillow 12.3.0 |

Docker daemon на хосте изначально не был запущен. Для реальной сборки и
проверки контейнера был поднят Colima (Linux ARM64 VM). Все проверки ниже
выполнены на реальном Docker, а не только статически.

## 2. Автоматические тесты

Команда:

```bash
pytest -q
```

Результат: **77 passed, 0 failed** (2.92 с).

| Файл | Кол-во тестов | Что проверяется |
|---|---:|---|
| `tests/test_barcode_reader.py` | 14 | Code 128, QR, Data Matrix, PDF417, EAN-13, EAN-8, Code 39, ITF; PDF со штрихкодом на 2-й странице; JPEG, TIFF, поворот; лимит страниц; шифрованный/повреждённый PDF; неизвестный формат; конфигурация форматов |
| `tests/test_processor.py` | 16 | сортировка, UNMATCHED, UNREADABLE, NEEDS_REVIEW, несколько штрихкодов одной/разных фамилий, ведущие нули, защита от перезаписи, повторная обработка, восстановление после перезапуска, symlink-атаки, read-only input, неизменность хешей/размеров/mtime, повтор после сбоя записи в БД |
| `tests/test_scanner.py` | 11 | периодический проход, стабильность файла, идемпотентность, недоступность input, изоляция ошибок, backoff, non-retryable, восстановление «зависшей» обработки, запрет параллельных проходов, pause/resume/ручной запуск, игнорирование служебных файлов |
| `tests/test_csv.py` | 8 | импорт UTF-8/BOM/CP1251, дубликаты, ошибки строк, существующие записи, лимит строк, экспорт с BOM, round-trip |
| `tests/test_web.py` | 14 | редирект без авторизации, вход/выход, неверный пароль, rate limit, CSRF, CRUD, поиск, импорт/экспорт, панель, журналы, HTMX-кнопки, health, 404, security headers, TrustedHost, отключение auth |
| `tests/test_security.py` | 8 | PBKDF2, scrypt/PBKDF2-хеши, пустой пароль, права session secret, права лог-файла, rate limiter |
| `tests/test_docker.py` | 6 | `:ro` для input, отсутствие Docker socket/privileged, read-only rootfs, cap_drop, healthcheck, non-root, `.env.example`, `.dockerignore`, `.gitignore` |

Отдельно проверено, что:

- дерево `INPUT_DIR` (имена, размеры, mtime, SHA-256) **не меняется** после обработки;
- обработка работает при `chmod 0555` на `INPUT_DIR`;
- попытка создать файл в `INPUT_DIR` из приложения/контейнера невозможна;
- повторная обработка не создаёт новых копий;
- после сбоя между копированием и финальной записью в БД повторная попытка
  переиспользует уже опубликованный файл (проверяется SHA-256).

## 3. Статический анализ

```bash
ruff check app tests
# All checks passed!

bandit -q -r app -x app/templates,app/static
# 0 findings (High/Medium/Low)
```

Дополнительно: `python -m compileall -q app` — без ошибок.

Замечания анализатора, исправленные в ходе работы:

- убраны `assert` из рабочего кода (SQLite retry, парсер форматов);
- ограничены права `DATA_DIR` (`0700`) и логов (`0640`);
- изменён безопасный default `WEB_HOST` на `127.0.0.1` (в контейнере задаётся
  `0.0.0.0` явно);
- healthcheck переведён с `urllib` на `http.client` (устранено замечание B310).

## 4. Проверка Docker Compose

```bash
docker compose config
```

Результат: **успешно**, конфигурация валидна. Подтверждено:

- `${INPUT_DIR}:/app/input:ro` — `read_only: true`;
- `OUTPUT_DIR`, `DATA_DIR`, `LOG_DIR` — read-write bind mounts;
- `restart: unless-stopped`, `init: true`, `stop_grace_period: 45s`;
- `read_only: true` для корневой ФС;
- `tmpfs /tmp` с `noexec,nosuid,nodev`;
- `cap_drop: [ALL]`, `no-new-privileges:true`;
- healthcheck `python -m app.healthcheck`;
- нет `privileged: true`, нет `/var/run/docker.sock`.

## 5. Сборка Docker-образа

```bash
docker compose build
```

Результат: **Successfully built**; образ
`barcode-sorter-barcode-sorter:latest` (linux/arm64).

Изначально сборка падала на `zxing-cpp 2.3.0`: для Linux ARM64 не публикуется
wheel, и pip пытался собрать библиотеку из исходников. Зависимость обновлена
до `zxing-cpp>=3.0,<4.0`; в 3.1.1 есть готовые wheel для Linux ARM64 и
x86_64. После обновления сборка проходит, все тесты распознавания проходят.

Дополнительно запускалась сборка под `linux/amd64` — см. раздел 10.

## 6. Запуск контейнера и healthcheck

```bash
docker compose up -d
docker compose ps
```

Статус: `Up ... (healthy)`.

```json
{
  "status": "ok",
  "version": "1.0.0",
  "database": true,
  "input_available": true,
  "input_readonly": true,
  "scanner_running": true,
  "scanner_paused": false
}
```

Веб-интерфейс доступен по HTTP, вход выполнен с паролем из `.env` и отдельно с
`WEB_PASSWORD_HASH` (PBKDF2). После входа открываются панель, база, журналы.

## 7. Сквозной функциональный тест в контейнере

Сценарий выполнен через реальный HTTP-интерфейс (httpx), файлы создавались
локально и копировались в смонтированный `INPUT_DIR`.

1. Добавлены соответствия `CONTAINER-001 → Иванов`, `OTHER-002 → Петров`.
2. В input положены:
   - `scan1.png` (Code 128, известный штрихкод);
   - `unknown.png` (QR, нет в базе);
   - `blank.pdf` (без штрихкода);
   - `review.png` (два штрихкода разных фамилий).
3. Сканер обработал документы автоматически.
4. Проверен второй проход: новый файл, добавленный **после** первого прохода,
   также обработан и сохранён в БД.
5. Импортирован CSV `CSV-777,Сидоров`; добавлен скан; документ отсортирован в
   `Сидоров/`.
6. Экспорт CSV отдал файл с BOM и корректным содержимым.
7. Файл с тем же именем `scan1.png` (другой путь, другое содержимое) создал
   `scan1_1.png`; существующий `scan1.png` не изменён (проверено по SHA-256).

Итоговая структура output:

```
output/
├── Иванов/
│   ├── scan1.png
│   ├── scan1_1.png
│   ├── late.png
│   └── check.png
├── Сидоров/
│   └── csvscan.png
├── UNMATCHED/
│   └── unknown.png
├── UNREADABLE/
│   └── blank.pdf
└── NEEDS_REVIEW/
    └── review.png
```

Статусы в SQLite: `sorted`, `unmatched`, `unreadable`, `needs_review`;
все 8 документов учтены.

## 8. Проверка безопасности исходной папки в реальном контейнере

```bash
docker compose exec -T barcode-sorter id
# uid=1000(app) gid=1000(app)

docker compose exec -T barcode-sorter sh -c \
  'touch /app/input/forbidden.txt'
# touch: cannot touch '/app/input/forbidden.txt': Read-only file system
```

Проверка монтирования внутри контейнера:

```
/app/input  ... ro,relatime
/app/output ... rw,...
/app/data   ... rw,...
/app/logs   ... rw,...
/           ... ro,...
```

Проверка конфигурации контейнера:

```
readonly_rootfs=true
privileged=false
cap_drop=[ALL]
security_opt=[no-new-privileges:true]
user=app
```

Попытка записи в корневую ФС (`/app/forbidden.txt`) также блокируется:
`Read-only file system`. Запись в `/tmp` работает (tmpfs).

Снимок дерева `INPUT_DIR` до и после нескольких проходов сканера совпал
(имена, размеры, mtime, SHA-256). Приложение не создало в исходной папке ни
одного файла/каталога.

## 9. Перезапуск и сохранность данных

- `docker compose restart` → контейнер healthy, документы и копии на месте,
  повторных копий нет.
- `docker compose down` → контейнер и сеть удалены, каталоги `data/`,
  `output/`, `logs/` сохранены.
- `docker compose up -d` → 8 документов в БД, 8 файлов в output, повторной
  обработки и лишних копий нет.
- SQLite-файл `0600`, лог `0640`, каталог данных `0700`.
- Файл лога пишется в `LOG_DIR` и сохраняется между перезапусками.

## 10. Сборка и запуск под linux/amd64

Для типовой архитектуры Ubuntu Server отдельно выполнено:

```bash
docker buildx build --platform linux/amd64 --load \
  -t barcode-sorter-amd64:test .
```

Результат: **успешно**. Скачаны и установлены x86_64-wheel:

- `zxing_cpp-3.1.1-cp312-abi3-manylinux_2_27_x86_64...whl`
- `pymupdf-1.28.2-cp310-abi3-manylinux_2_28_x86_64...whl`
- `pillow-12.3.0-...manylinux_2_27_x86_64...whl`
- `pydantic_core-2.50.4-...manylinux_2_17_x86_64...whl`

Проверка импортов и запуска x86_64-образа под эмуляцией:

```bash
docker run --rm --platform linux/amd64 --entrypoint python \
  barcode-sorter-amd64:test -c "import app.main, zxingcpp, pymupdf; ..."
# x86_64 ... imports_ok
```

Контейнер с `--read-only`, `--cap-drop ALL`, `no-new-privileges`, read-only
input и отдельными томами запущен под linux/amd64:

```json
{"status":"ok","version":"1.0.0","database":true,
 "input_available":null,"input_readonly":null,
 "scanner_running":false,"scanner_paused":false}
```

Статус контейнера: `Up ... (healthy)`. Это подтверждает, что Dockerfile
работоспособен не только на ARM64, но и на целевой x86_64-платформе. На
реальном Ubuntu-сервере QEMU-эмуляция не используется, поэтому запуск будет
быстрее.

## 11. Важное эксплуатационное замечание по SQLite

В ходе теста на macOS/Colima обнаружено: если открыть файл
`DATA_DIR/barcode_sorter.db` **сторонним SQLite-клиентом с хоста** при
работающем контейнере, виртуальная ФС (virtiofs) может удалить WAL/SHM-файлы,
которые контейнер держит открытыми. После этого новые транзакции приложения
могут теряться. Это ограничение тестовой виртуальной ФС, а не логики
приложения: при повторном чистом прогоне без доступа к БД с хоста все записи
(включая документы, добавленные после первого прохода) сохраняются.

Правила эксплуатации:

- `DATA_DIR` размещать **только на локальном диске** (ext4/xfs), не на SMB/NFS;
- не открывать SQLite с хоста при работающем контейнере;
- для резервного копирования останавливать контейнер или использовать
  `docker compose exec` + SQLite Backup API (см. README, раздел 18).

## 12. Ограничения проверки

- Полноценный функциональный прогон производился на Linux ARM64 (Colima) под
  Docker; сборка **и запуск** linux/amd64 дополнительно проверены под QEMU.
- Не проводилось нагрузочное тестирование на реальном сетевом SMB-ресурсе и
  сканере; логика SMB/NFS не реализуется приложением — используется уже
  смонтированный каталог хоста.
- Не проверялась работа с паролями PDF, отличными от «зашифрован без пароля
  пользователя»: такие документы корректно классифицируются как ошибка чтения.
- Интерфейс рассчитан на один экземпляр приложения; горизонтальное
  масштабирование с SQLite не поддерживается.

## 13. Итог

| Критерий | Результат |
|---|---|
| Docker-образ собирается | ✅ |
| Docker Compose запускает приложение | ✅ |
| Веб-интерфейс открывается | ✅ |
| SQLite создаётся автоматически | ✅ |
| CRUD штрихкодов через UI | ✅ |
| Импорт CSV | ✅ |
| Непрерывное сканирование | ✅ |
| PDF и изображения | ✅ |
| Сортировка в папки фамилий | ✅ |
| UNMATCHED / UNREADABLE / NEEDS_REVIEW | ✅ |
| Повторная обработка без лишних копий | ✅ |
| Состояние после restart/recreate | ✅ |
| INPUT_DIR не изменяется | ✅ |
| Автотесты | ✅ 77/77 |
