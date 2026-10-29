# HTTP через aria2: проверка на Aeza

HTTP payload теперь скачивает один общий процесс aria2. Python выполняет только
metadata-запросы, контроль полезного прогресса, `fsync`, source hash/SHA-256 и CAS commit.
На MSKs-5 используются 16 file workers; хеширование ограничено четырьмя workers.
Прежний Python HTTP/Range transport удалён.

## Проверки корректности

`./scripts/check.sh`: 401 тест прошёл, 13 opt-in live тестов пропущены; Ruff, mypy,
shellcheck и actionlint прошли. 22 теста запускают настоящий aria2 и локальный HTTPS:

- source hash, SHA-256, кеш и отклонение изменённого CAS blob;
- resume после сетевого обрыва и отмены, включая sparse Range-файлы;
- смена ETag, Last-Modified без ETag, отсутствие валидатора/control-файла;
- импорт проверенного sequential prefix прежнего транспорта;
- маленькие порции данных, медленный здоровый поток, read timeout и idle deadline;
- malformed Range, серверы без HEAD/Range и безопасный single-stream fallback;
- ошибки callback и отсутствие живых дочерних процессов после выхода;
- отсутствие URI credentials в argv и диагностике.

Сохраняются 15 секунд на connect, 60 секунд на read, пять минут без полезного прогресса,
ограниченные повторы с backoff, предупреждения о низкой скорости, 60 минут на download
в Actions и четырёхчасовой VM timer. После смены источника загрузка начинается заново;
после обычного обрыва используется `.aria2` bitmap с проверкой валидатора. Старые Python
Range partials однократно перезапускаются; готовые проверенные CAS blobs переиспользуются.

RPC слушает только loopback. Его одноразовый token находится в config `0600` внутри
каталога `0700`, не попадает в argv/env/logs и удаляется при закрытии. Локальный RPC-клиент
не использует proxy environment. Ошибка/отмена останавливает и дожидается HTTP-процесса
aria2 до дальнейшего изменения partial/control-файлов.

Встроенный write cache aria2 отключён, чтобы маленький последовательный префикс точно
отражал полученные байты даже ниже блочной точности RPC. Sparse-файл никогда не считается
готовым по одному размеру: нужны native completion, `fsync` и хеши. Метаданные источника
проверяются до передачи и повторно перед приёмкой; conditional headers связывают запросы
с strong ETag/Last-Modified.

## Скорость на Aeza

В [run 40800722749](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40800722749)
все 22 native acceptance tests повторно прошли на Ubuntu за 65,51 секунды.
Получены 30 корректных измерений; все SHA-256 совпали, fingerprints DNS одинаковы.

Медианы трёх повторов, **МиБ/с**, включая `fsync` и проверку:

| Клиент | 16 файлов по 109–146 МиБ | 4 файла по 568–630 МиБ |
| --- | ---: | ---: |
| curl | 345,6 | 142,1 |
| aria2, одно соединение на файл | 380,6 | 144,1 |
| aria2, Range | 371,2 | 362,5 |
| Прежний buffered Python downloader, 4 workers | 164,7 | 239,3 |
| Новый downloader через aria2, 16 workers | **291,4** | **267,5** |

Новый downloader быстрее прежнего в **1,77 раза** на небольшом наборе и в **1,12 раза**
на крупном. Все три результата нового транспорта: 291,6 / 253,8 / 291,4 МиБ/с и
284,5 / 267,5 / 240,2 МиБ/с соответственно. Отдельный aria2 остаётся быстрее обёртки.
Обёртка дополнительно проверяет метаданные, синхронизирует и принимает отдельные файлы в CAS;
этот прогон не разделяет стоимость этих операций и параметры native разбиения.

Медиана CPU-времени небольшого набора снизилась с 8,14 до 5,06 секунды, крупного —
с 13,56 до 5,31 секунды. Voluntary context switches: 237 380 → 66 108 и
417 899 → 71 859 соответственно. Значения относятся к GNU time для всего worker,
включая дочерний aria2.

## Методика скоростного сравнения

[Benchmark run 40800722749](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40800722749)
использует почасовую Aeza Moscow Shared MSKs-5 и production protections.
Baseline — предыдущий buffered Python transport
`5227fbf076d1e52b2e93e27b2a5d5d168a8edba3` с четырьмя workers; новый transport —
`30e08aa45c1fec7c6ee1a1c007edd6314f536f67` с 16 workers.

Все инструменты получают одинаковые pinned игровые файлы и пустой каталог. Три повтора,
ротация порядка, время включает передачу, `fsync` и проверку целостности. SHA-256 всех
результатов должны совпасть. Один public IP каждого hostname закреплён только на временной
VM; все клиенты используют системный resolver, fingerprints DNS проверяются перед
принятием замера. Адреса VM и игровые payload в отчёт не загружаются.

В отличие от [предыдущего исследования](download-performance.md), сравнение не смешивает
разные DNS endpoints. При повторении нужно учитывать состояние CDN, shared VM и диска;
это замер выбранных подмножеств, а не всего клиента.

Наборы содержат 2 182 397 500 и 2 532 808 412 байт. Артефакт
`download-benchmark-40800722749-1` содержит manifests, digests, все результаты,
версии инструментов и CPU/I/O/network samples. Результаты предыдущего исследования
с другими VM и DNS endpoints нельзя использовать вместо baseline этого прогона.

## Удаление VM

Одна почасовая MSKs-5 была создана для этого теста. Весь workflow занял 10 минут 41 секунду,
аренда — **€0,53**. [Cleanup](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40800722749/job/123211082837)
подтвердил удаление услуги через Aeza API и временных ресурсов. Запрос GitHub API после
cleanup не нашёл runner этого run. [Reconciler 40801792826](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40801792826)
успешен; recovery alert пропущен, поскольку дополнительных ресурсов для удаления не осталось.

Для повторения: `Benchmark game download`, `repeats: 3`, `diagnose: false`,
`baseline_sha: 5227fbf076d1e52b2e93e27b2a5d5d168a8edba3`, `baseline_workers: 4`,
`loopback_control: false`. Повторение создаёт новую платную VM.

Описание возможностей и RPC: [официальное руководство aria2](https://aria2.github.io/manual/en/html/aria2c.html).
