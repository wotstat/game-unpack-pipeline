# Техническое введение в game-unpack-pipeline

Публичный pipeline автоматической проверки, скачивания, распаковки и публикации снимков клиентов
World of Tanks и «Мира танков». Репозиторий содержит всю основную полезную нагрузку: протоколы WGUS/LSTUS,
download/verify, распаковку VFS, readable transforms, sealed `GameSnapshot`, временную Aeza VM
и вызов publisher workflows. Текущее состояние и короткая история завершённых запусков публикуются
как статическая страница GitHub Pages.

## Поток

```text
schedule каждый час на :23 UTC
  → lightweight-проверка всех targets
  → для новых версий workflow_dispatch основного pipeline
workflow_dispatch
  → одна почасовая Aeza MSKs-5 VM; одноразовый SSH bootstrap и egress-only nftables
  → локальный systemd kill switch удаляет VM после четырёх часов
  → четыре изолированных repository-level JIT runner на этой VM
  → встроенный download job собирает полный sealed GameSnapshot
  → выбранные reusable workflows из main: wot-src, wot-gui-assets и wotstat-assets-uploader
    обрабатывают snapshot параллельно
  → cleanup с always() удаляет runner registrations и услугу Aeza
  → результат run записывается в status параллельно с Telegram-отчётом
  → из status и его Git-истории собирается и публикуется GitHub Pages
  → workflow_run reconciler подтверждает удаление услуги Aeza
```

Snapshot не загружается в Actions artifact. Downloader и три consumer читают один абсолютный путь
на локальном диске VM, работая под отдельными Unix-пользователями и в разных runner directories.
Только downloader имеет `sudo`.

Несколько ручных запусков независимы и получают разные имена, labels и облачные ресурсы на основе
`github.run_id` и `github.run_attempt`. Отменяющий `concurrency` на уровне orchestrator не
используется. Обновления одной data-ветки сериализуются reusable workflow соответствующего
publisher с `cancel-in-progress: false`.

## Границы компонентов

| Компонент | Ответственность |
| --- | --- |
| `src/game_downloader` | Resolve WGUS/LSTUS, download/verify, client/VFS/readable pipeline, stubs и sealed `GameSnapshot` |
| `.github/workflows` и `scripts` | Production entrypoint, Aeza lifecycle, четыре JIT runner, cleanup/reconciliation и отчёты |
| [`wot-src`](https://github.com/wotstat/wot-src) | Reusable publication workflow, проверка snapshot и публикация исходников, XML/PO, AS3, stubs и Gameface |
| [`wot-gui-assets`](https://github.com/wotstat/wot-gui-assets) | Reusable publication workflow, проверка snapshot и публикация `res/gui` без `.py` |
| [`wotstat-assets-uploader`](https://github.com/wotstat/wotstat-assets-uploader) | Reusable upload workflow, проверка snapshot и загрузка временных данных в ClickHouse и S3 |

Downloader не является отдельным reusable workflow или внешним продуктом. Его production-интерфейс
совпадает с download job основного workflow; устойчивым seam для publisher остаётся только sealed
`GameSnapshot`. Правила проекции data-репозиториев принадлежат самим publisher.

## Download pipeline

Основная реализация находится в [`src/game_downloader`](../src/game_downloader), а GitHub-specific
stage runner и сбор диагностик — в [`.github/scripts`](../.github/scripts). Download job выполняет
фиксированную последовательность:

```text
resolve → plan-acquisition → download → verify → assemble-client
  → index-vfs → materialize-vfs
  → plan-readable → transform-readable → decompile-actionscript
  → assemble-readable → generate-engine-stubs → finalize-readable
  → snapshot
```

Стадии сохраняют атомарные checkpoints внутри run directory. Переход читает непосредственный
checkpoint, проверяет его digest и завершает процесс полной независимой проверкой snapshot.

Обычная HTTP-загрузка делает до восьми попыток на URL с паузами 1, 2, 4, 8 и далее по 10 секунд.
Каждый файл в однопоточном HTTP-режиме останавливается при отсутствии продвижения вперёд за пять
минут. Средняя скорость продвижения ниже 256 КиБ/с за последние пять минут выдаёт предупреждение
и позволяет текущему потоку продолжить загрузку. Предупреждение выводится один раз за период
замедления; после восстановления скорости новое замедление снова попадает в лог. Проверки
действуют с первого запроса, в том числе после fallback с parallel Range, и учитывают все повторы
и URL файла вместе. Повторное скачивание уже полученного префикса не обновляет таймер отсутствия
прогресса. Для всех targets средняя суммарная скорость ниже 1 МиБ/с за пять минут также выдаёт
только предупреждение и не останавливает загрузку. Оно выводится один раз за период замедления;
после восстановления скорости новое замедление снова попадает в лог. Для последних 5% действует
отсрочка предупреждения на 20 минут с момента входа в этот хвост.
Частично скачанный файл сохраняется для докачки с проверкой Range и ETag/Last-Modified,
итоговый payload проверяется по source hash, если он объявлен.
Сетевые ожидания ограничены 15 секундами на соединение и 60 секундами на чтение; они дополнительно
ограничены временем до таймаута отсутствия прогресса на момент начала запроса. Остановка
обнаруживается при следующем чтении или сетевом таймауте. Окончательная ошибка файла останавливает
остальные HTTP-загрузки и не даёт начинать новые. В GitHub Actions вся стадия `download` ограничена
60 минутами, после ошибки выполняются сбор диагностик и штатный cleanup.

Downloader преобразует Python 2.7 `.pyc`, packed XML в `.xml` и `.def`, GNU `.mo` и ActionScript
из SWC, а также форматирует HTML, CSS и JavaScript через pinned Prettier. Остальные assets
сохраняются без изменений; для каждого преобразования provenance manifest фиксирует исходный digest, вид
representation и точную версию инструмента. `READY` появляется последним.

Формат результата закреплён схемами из [`contracts/v1`](../contracts/v1). Publisher получают только
абсолютный snapshot path, snapshot ID и SHA-256 canonical descriptor и повторно проверяют handoff
перед обработкой.

## Local download

The root-level `download.sh` script runs the same downloader pipeline locally without Aeza,
GitHub Actions, or publishers. The recommended short form is:

```bash
./download.sh wot-eu ./.data --language ALL
```

The equivalent form with a named target is also supported:

```bash
./download.sh --target wot-eu --language ALL ./.data
```

The default directory is `./.data`, the default client type is `sd`, and the default language is
`EN`, so the shortest invocation is:

```bash
./download.sh wot-eu
```

Pass multiple languages as a comma-separated list. Additional examples:

```bash
./download.sh wot-eu ./eu-data --language EN,DE --workers 8
./download.sh mt-ru ./mt-data --language RU --client hd
```

The script requires a Unix-like system, [`uv`](https://docs.astral.sh/uv/), Java, Node.js with npm,
and one of `7zz`, `7z`, or `bsdtar`. It installs the lockfile-pinned Prettier `3.9.6` package into
the ignored local `node_modules` directory. It also downloads the pinned FFDec `26.2.1` release
into the ignored local `.tools` directory, verifies the official archive's SHA-256 checksum, and
reuses both installations on later runs. The initial FFDec installation also requires `curl` and
`unzip`.

The selected directory is a persistent workspace containing the download cache, checkpoints, and
sealed snapshots. Later runs reuse already verified blobs. The completed result is stored under
`.data/snapshots/sha256:<identifier>`. It is a `GameSnapshot` for analysis and publication, not a
launcher-ready game installation.

## Ручной запуск

Точка входа —
[`process-game-release.yml`](../.github/workflows/process-game-release.yml). Workflow всегда строит полный
snapshot до стадии `snapshot` и предоставляет только production inputs:

| Input | Значение |
| --- | --- |
| `target` | `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru`, `mt-public-test` |
| `client_type` | `sd` или `hd` |
| `languages` | Коды через запятую или отдельное значение `ALL`; по умолчанию `ALL` |
| `detected_release_name` | Необязательная подсказка checker; в прямом ручном запуске остаётся пустой |
| `publish_wot_src` | Включить публикацию в `wot-src`; по умолчанию `true` |
| `publish_wot_gui_assets` | Включить публикацию в `wot-gui-assets`; по умолчанию `true` |
| `publish_wotstat_assets` | Включить временную загрузку через `wotstat-assets-uploader`; по умолчанию `true` |

Каждый consumer можно независимо отключить для ручного рерана. Если отключить все три, workflow только
соберёт и проверит snapshot, после чего удалит VM. Используется только Aeza Moscow
Shared MSKs-5: 16 vCPU, 32 ГБ RAM, 480 ГБ NVMe, Ubuntu 24.04, €0,53/час.
Provision проверяет свободный shared/hourly slot, баланс и котировку до заказа.
Прерываемых VM, выбора региона и немедленных автоматических rerun нет.

## Проверка новых версий

[`check-game-releases.yml`](../.github/workflows/check-game-releases.yml) — отдельный checker с
автоматическим расписанием `23 * * * *` и ручным `workflow_dispatch`. Он не скачивает клиент:
для каждого выбранного target lightweight probe запрашивает WGUS/LSTUS metadata и один patches
chain для объявленной default language, затем сравнивает `release_name` с [`status`](../status).

Scheduled run каждый час проверяет все семь targets и автоматически dispatch'ит основной
pipeline для новых версий. Ручной запуск сохраняет семь независимых whitelist-чекбоксов; по
умолчанию включены все семь targets и `dispatch_pipelines`, поэтому найденные новые версии сразу
запускают production pipeline. Все targets используют тот же Aeza MSKs-5.
Для dry-run нужно явно выключить `dispatch_pipelines`.

При разрешённом dispatch checker сначала исключает уже ожидающий или работающий pipeline того же
target. После `failure` или `cancelled` неопубликованная `release_name` повторяется автоматически:
первые три дня — через 12 часов, следующие семь дней — через 24 часа, затем — через семь дней.
Возраст цикла считается между первой и последней неудачными попытками этой версии; выбранный
интервал отсчитывается от завершения последней попытки. Ближайшая часовая проверка после дедлайна
разрешает один dispatch, без накопления пропущенных запусков. До дедлайна checker показывает
`retry_wait` и время следующей попытки в UTC. Прямой ручной запуск основного workflow доступен
в любое время; неудачный ручной запуск с известной версией также сдвигает следующую попытку.
В ручном checker можно включить `force_retry` (по умолчанию выключен), чтобы сразу повторить все
найденные неопубликованные версии, не дожидаясь retry. Для запуска нужен `dispatch_pipelines`;
уже работающие targets и успешно опубликованные версии пропускаются. Cron соблюдает расписание retry.
Отличающиеся targets запускаются параллельно на default branch с `sd`, `ALL` и всеми тремя consumer.
Checker передаёт найденную `release_name` как необязательную подсказку, чтобы ранняя ошибка pipeline
всё равно была привязана к проверенной версии. После завершения матрицы общий Job Summary показывает
таблицу с сохранённой и найденной версиями, результатом сравнения и действием для каждого выбранного
target; сбои отдельных targets остаются видны отдельными строками.

Каждый status-файл хранит последнюю успешно опубликованную `release_name`, соответствующую
`readable_version` в Telegram-формате `x.x.x.x #xxx` и описание `last_run`: результат, обе версии,
время начала и завершения, длительность, Actions Run ID, attempt и URL. Bootstrap-значения равны
`null`. Необязательный объект `retry` содержит только `release_name`, `first_failed_at` и
`last_failed_at` текущего цикла повторов. Новая версия допускается к запуску сразу и при неудаче
начинает новый цикл; успех очищает `retry`. Ошибка без определённой версии сохраняет существующий
цикл. Старые документы без `retry` читаются с датой последней сохранённой неудачной попытки как
началом цикла; при внедрении для текущей неуспешной WoT CN версии первая дата восстановлена из
Git-истории. История запусков по-прежнему хранится только в Git.

`release_name` фиксируется из успешно завершённой стадии `resolve`, а для checker-originated run при
ранней ошибке берётся из необязательной dispatch-подсказки. `readable_version` читается из корневого
`version.xml` готового snapshot и до получения payload недоступна. Поэтому ранняя ошибка
автоматически запущенного pipeline всё равно показывает проверенную release name; ранняя ошибка
прямого ручного запуска без подсказки может остаться без версии.

После cleanup основной workflow с `always()` перезаписывает `last_run` и коммитит один региональный
файл даже при `failure` или `cancelled`. Результат для status определяется успехом provision,
downloader и всех включённых consumer; пропуск включённого consumer считается ошибкой. Успешная
обработка одновременно продвигает верхнеуровневые `release_name` и `readable_version`. Ошибки
cleanup, queue watchdog и уведомлений не меняют результат обработки в status, но остаются видны
в GitHub Actions. Параллельные status-job сериализованы без
отмены; конфигурация ручного run намеренно не входит в status.

## Публичная статус-страница

[`deploy-status-page.yml`](../.github/workflows/deploy-status-page.yml) checkout'ит полную Git-историю,
проверяет текущие документы, извлекает прошлые `last_run` из предыдущих версий тех же файлов и
генерирует статический сайт через [`render_status_page.py`](../scripts/render_status_page.py). На
странице показаны текущая Telegram-version по каждому региону, состояние последнего запуска и
короткая общая история со временем, длительностью и ссылкой на Actions run.

Для каждого target сборка также создаёт Shields endpoint `badges/<target>.json`. Бейдж показывает
последнюю успешно опубликованную `readable_version`, а цвет отражает результат последнего запуска:
зелёный для `success`, красный для `failure`, жёлтый для `cancelled` и серый при отсутствии данных.
Бейджи в основном README ведут на якоря соответствующих регионов статус-страницы.

Checker после завершения передаёт Pages workflow время, результат и URL текущей проверки напрямую,
без status-коммита. При остальных пересборках Pages workflow читает последний завершённый checker
run через GitHub Actions API. На странице время последней проверки ведёт на checker run, время
последнего обновления — на изменивший status pipeline. Общий endpoint `badges/release-check.json`
показывает время проверки в МСК и также не требует Git-коммита.

Чтение списка checker runs и его jobs повторяется при HTTP 502: не более четырёх попыток
на запрос с паузами 5, 15 и 30 секунд. После исчерпания попыток сборка завершается ошибкой;
остальные HTTP-ошибки не повторяются. Переданные checker metadata не требуют этих API-запросов.

Основной pipeline вызывает reusable Pages workflow только после успешного status commit. Это
работает и для runs, созданных checker через repository `GITHUB_TOKEN`, для которых новый
`workflow_run` или `push` workflow автоматически не возник бы. Отдельные `push` и
`workflow_dispatch` triggers позволяют пересобрать страницу после изменения её кода или вручную.
Собранный `_site` передаётся GitHub Pages только как Actions artifact и не коммитится.

## Reusable snapshot consumer workflows

Оркестратор вызывает каждый consumer напрямую из его ветки `main`:

```yaml
uses: wotstat/wot-src/.github/workflows/publish-snapshot.yml@main
uses: wotstat/wot-gui-assets/.github/workflows/publish-snapshot.yml@main
uses: wotstat/wotstat-assets-uploader/.github/workflows/upload-snapshot.yml@main
```

Это обычные reusable workflows внутри caller run, а не cross-repository dispatch. Каждый workflow
checkout’ит собственный репозиторий через `job.workflow_repository` и `job.workflow_sha`, поэтому
исполняемый consumer-код совпадает с commit, в который GitHub разрешил `main` для данного run. Он
получает выделенный JIT runner, локальный
snapshot path, target, snapshot ID и descriptor digest. Data-репозитории выводят branch и правила
проекции из своей конфигурации; uploader получает ClickHouse/S3 settings только из отдельного
Environment `wotstat-assets-uploader` caller-репозитория. Имя Environment зафиксировано внутри
reusable workflow, а caller использует `secrets: inherit`, чтобы called job получил его environment
secrets.

Отсутствующая data-ветка создаётся первой публикацией сразу на version commit. Существующая ветка
обязана содержать `.publication.json`; markerless ref считается чужим состоянием и приводит к hard
failure. Повтор идентичных публикуемых данных возвращает `unchanged` без commit.

Если изменённые Git blobs суммарно превышают 1 ГБ, publisher загружает bounded batches во
временную `publication-staging/...` ветку, кумулятивно собирает полный tree и проверяет его object
ID. Затем GitHub Git Database API создаёт один final version commit, а production-ref обновляется
без force. Staging commits не входят в production-историю и временный ref удаляется также при
ошибке. Подробные transport-инварианты находятся в документации data-репозиториев.

## Cleanup и безопасность

Aeza API key хранится в Environment `aeza` и root-файле `0600` на временной VM:
это общий ключ аккаунта, ограничить его одним сервером нельзя. Bootstrap загружается
через одноразовый SSH-ключ; до запуска runner systemd вооружает четырёхчасовой timer,
а nftables закрывает ingress. SSH-ключ удаляется, SSH отключается.
Каждый runner имеет отдельного Unix-пользователя и HOME, sudo есть только у downloader.
Архив runner сверяется по SHA-256 официального release asset.

Cleanup выполняется с `always()` и подтверждает `deleted`/404 после асинхронного DELETE
услуги. Reconciler ищет точное имя с hash репозитория, Run ID/attempt и проверяет тариф
и почасовой payment term. SSH-ключ удаляется после VM; сбой GitHub не мешает удалению
платной услуги. Watchdog и локальный timer используют один абсолютный дедлайн.
Для token-originated runs сохраняется явный dispatch reconciler. Recovery alert
отправляется только при подтверждённом `deleted_count > 0`.

GitHub App, consumer contracts, secrets uploader, Telegram и Git-история status остаются
прежними. Неуспешные версии повторяются существующим checker по retry state.

## Настройка и проверка

Полная настройка Aeza, GitHub App, Environments, variables и Telegram описана в
[`setup.md`](setup.md). `Process game release` сразу создаёт тарифицируемые ресурсы;
`Check game releases` по умолчанию dispatch'ит production pipeline для найденных новых версий.

```text
.github/scripts/
.github/workflows/check-game-releases.yml
.github/workflows/deploy-status-page.yml
.github/workflows/process-game-release.yml
.github/workflows/reconcile-release-resources.yml
contracts/v1/
scripts/bootstrap-actions-runner.sh
scripts/emergency_self_destruct.py
scripts/release_status.py
scripts/render_status_page.py
scripts/runner_lifecycle.py
status-page/
src/game_downloader/
tests/
docs/setup.md
```

Локальные проверки не обращаются к GitHub или Aeza:

```bash
./scripts/check.sh
```
