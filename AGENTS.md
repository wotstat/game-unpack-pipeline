# Контекст проекта для агентов

## Назначение репозитория

`game-unpack-pipeline` — публичный pipeline ручного production-скачивания, распаковки и публикации
снимков клиентов World of Tanks и «Мира танков». Репозиторий владеет основной полезной нагрузкой
downloader, GitHub Actions entrypoint, жизненным циклом временной VM и четырёх repository-level JIT
runner, вызовом reusable snapshot consumer, cleanup/reconciliation, Telegram-отчётами и публичной
статус-страницей GitHub Pages.

Автоматический и ручной checker новых версий и repository status-файлы реализованы. Короткая
публичная история строится из Git-истории этих файлов; внешний status store отсутствует.

## Реализованный поток

```text
manual workflow_dispatch
  → provision одной VM, direct public IP и egress-only security group в Selectel
  → четыре JIT runner в game-unpack-pipeline: downloader, wot-src, wot-gui-assets и wotstat-assets
  → встроенные downloader stages на downloader runner
  → sealed snapshot на локальном диске VM
  → параллельные reusable workflows из main двух data-репозиториев и wotstat-assets-uploader
  → выбранные production data-ветки target
  → cleanup с always()
  → параллельно запись результата run в status и итоговый Telegram-отчёт
  → после status commit сборка и deployment GitHub Pages
  → workflow_run reconciler в ru-7 и ru-9
  → recovery alert только при deleted_count > 0
```

Отдельный `.github/workflows/check-game-releases.yml` через lightweight WGUS/LSTUS probe проверяет
targets, сравнивает `release_name` с `status/<target>.json` и в dispatch-режиме параллельно запускает
основной workflow для отличающихся targets. Cron `23 */2 * * *` проверяет все семь targets и всегда
работает в dispatch-режиме. Ручной запуск по умолчанию проверяет только `wot-eu` как безопасный
dry-run.

Основной workflow всегда строит полный snapshot до `snapshot`. Dispatch предоставляет только
target, client type, languages и три независимых переключателя `publish_wot_src`,
`publish_wot_gui_assets` и `publish_wotstat_assets`, включённых по умолчанию.

## Границы компонентов

- `src/game_downloader`, `contracts/v1`, `config`: resolve WGUS/LSTUS, download/verify,
  client/VFS/readable pipeline, transforms, stubs, seal и verify `GameSnapshot`.
- `.github/workflows`, `.github/scripts`, `scripts`, `status-page`: production entrypoint, stage
  execution и telemetry, Selectel lifecycle, JIT runner bootstrap, publisher calls,
  cleanup/reconciliation и генерация GitHub Pages.
- [`wotstat/wot-src`](https://github.com/wotstat/wot-src), локально обычно `../wot-src`: reusable
  publication workflow, независимая проверка snapshot и проекция исходников, XML/PO, AS3, stubs и
  Gameface.
- [`wotstat/wot-gui-assets`](https://github.com/wotstat/wot-gui-assets), локально обычно
  `../wot-assets`: reusable publication workflow, независимая проверка snapshot и проекция
  `res/gui` без `.py`.
- [`wotstat/wotstat-assets-uploader`](https://github.com/wotstat/wotstat-assets-uploader), локально
  обычно `../wotstat-assets-uploader`: reusable upload workflow, проверка sealed handoff и загрузка
  временно маркированных данных в ClickHouse и S3.

Downloader — внутренняя часть этого pipeline. Не выделять его обратно во внешний reusable
workflow или отдельный репозиторий и не переносить сюда правила publisher без отдельного
архитектурного решения.

## Текущие контракты

- Единственная точка ручного production-запуска — `.github/workflows/process-game-release.yml`.
- `.github/workflows/check-game-releases.yml` запускается по cron `23 */2 * * *` и вручную. Cron
  проверяет все семь targets с включённым dispatch; ручной запуск имеет отдельные boolean whitelist
  inputs, `wot-eu: true` по умолчанию и `dispatch_pipelines: false`. Реальный dispatch всегда
  использует default branch, `sd`, `ALL` и все три snapshot consumer.
- Checker не создаёт downloader Run и не скачивает payload: lightweight probe запрашивает metadata,
  затем один patches chain для объявленной default language. Отсутствующий или некорректный status
  блокирует target; сбои targets изолированы через matrix `fail-fast: false`.
- `status/<target>.json` содержит последнюю успешную пару `release_name`/`readable_version` либо
  bootstrap `null`, а также `last_run` с result, обеими версиями, timestamps, duration, Actions Run
  ID/attempt и URL. После cleanup status-job с `always()` коммитит каждый завершённый run; ошибка не
  продвигает успешную пару. Checker сравнивает только `release_name`. Status-job сериализованы общей
  non-cancelling concurrency-группой и выполняются параллельно Telegram.
- `.github/workflows/deploy-status-page.yml` checkout'ит default branch с полной историей, строит
  `_site` из текущих status и предыдущих версий тех же файлов, создаёт Shields endpoint для каждого
  target и публикует GitHub Pages artifact. Бейдж показывает последнюю успешную `readable_version`,
  а цвет — результат последнего run. Основной workflow вызывает Pages прямо после status commit,
  включая token-originated runs;
  `push` и `workflow_dispatch` используются для изменений страницы и ручной пересборки. История не
  дублируется в накопительном JSON или отдельных run-файлах.
- Download job реализован прямо в основном workflow и последовательно выполняет все стадии от
  `resolve` до `snapshot`; внешнего downloader workflow contract нет.
- Targets: `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru`,
  `mt-public-test`; client types: `sd`/`hd`; languages: список или `ALL`. Production location
  зафиксирована как `ru-7b` и не вынесена в dispatch input.
- Каждый consumer можно независимо отключить. Все включённые consumer получают одинаковые target,
  snapshot identity и descriptor digest.
- Publisher lifecycle принадлежит reusable workflow data-репозитория и вызывается прямым
  `uses: owner/repo/.github/workflows/publish-snapshot.yml@main`. Все принадлежащие проекту
  cross-repository reusable workflows используют `@main`; не возвращать full-SHA pins,
  cross-repository dispatch/polling или локальный универсальный publisher workflow.
- Called workflow checkout’ит собственный код через `job.workflow_repository` и
  `job.workflow_sha`, а не default checkout caller-репозитория.
- `wotstat-assets-uploader` получает settings через фиксированный Environment
  `wotstat-assets-uploader` в caller-репозитории. Его reusable workflow не принимает имя
  Environment или named credentials через `workflow_call`; caller использует `secrets: inherit`,
  чтобы environment secrets были доступны called job. Workflow использует `DATA_DIR` только из
  snapshot path и обязан завершаться ошибкой при сбое любого loader.
- Отсутствующая data-ветка создаётся первой публикацией. Существующий ref без
  `.publication.json` — hard failure; bootstrap compatibility не поддерживается.
- Изменённые Git blobs суммарно больше 1 ГБ publisher загружает bounded staging pushes, создавая
  полный cumulative tree. После проверки tree hash один final commit создаётся через GitHub Git
  Database API; production-ref обновляется без force, staging commits в production-историю не
  входят.
- Перед изменением publisher transport полностью прочитать `../wot-src/AGENTS.md`,
  `../wot-src/docs/publication-transport.md`, `../wot-assets/AGENTS.md` и
  `../wot-assets/docs/publication-transport.md`. Не считать staging protocol legacy.
- Snapshot не загружается в Actions artifact. Все runner находятся на одной VM и читают один
  абсолютный путь; downloader открывает publisher только traversal к sealed snapshot.
- Каждый runner имеет уникальные name/label на основе `run_id`/`run_attempt`, отдельного
  Unix-пользователя, HOME, runner directory и одноразовую JIT-конфигурацию. Только downloader имеет
  `sudo`.
- Production flavor зафиксирован как HighFreq с выделенными ядрами
  `HFL2.16-32768-256-AMD`; Standard, обычный HighFreq и выбор flavor не поддерживаются.
- Cleanup выполняется после ошибок всех consumer. Reconciler идемпотентен, ищет ресурсы по
  точным ownership-маркерам и проверяет обе region.
- Для ручного snapshot run reconciler запускается через `workflow_run`. Snapshot run, созданный
  checker через repository `GITHUB_TOKEN`, после cleanup явно dispatch'ит reconciler, потому что
  GitHub подавляет последующий `workflow_run` для token-originated chain. Оба пути передают исходные
  `run_id`/`run_attempt`; reconciliation остаётся идемпотентной.
- Основной workflow отправляет после cleanup компактный HTML Telegram-отчёт с человекочитаемым
  target, downloader `readable_version` в формате `x.x.x.x #xxx`, client type, языками и consumer
  state. Введённый `ALL` сохраняется в заголовке буквально, а последняя строка показывает полную
  длительность run рядом со ссылкой. Reconciler отправляет recovery alert только при машинно
  подтверждённом `deleted_count > 0`.
- Не добавлять отменяющий global concurrency. Publisher сериализуют обновления одной data-ветки с
  `cancel-in-progress: false`; status и Pages deployment также сериализованы без отмены. Uploader не
  сериализует runs: production targets пишут во временные S3 namespaces `wot`/`mt`, а
  `wot-common-test` и `mt-public-test` изолированы в `wot-test`/`mt-test`.

## Секреты и реальные операции

- Код, workflows, несекретная конфигурация и публикуемые данные остаются публичными.
- `GH_APP_PRIVATE_KEY` хранится как repository-level Actions secret только в
  `game-unpack-pipeline` и явно передаётся обоим reusable publisher workflows.
  Вызов uploader использует `secrets: inherit`, поэтому его project-owned reusable workflow также
  входит в trusted boundary caller secrets, хотя `GH_APP_PRIVATE_KEY` не использует.
  `SELECTEL_OS_PASSWORD` хранится только в Environment `selectel`.
  `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` — только в Environment `telegram`.
- `CLICKHOUSE_PASSWORD`, `AWS_ACCESS_KEY_ID` и `AWS_SECRET_ACCESS_KEY` uploader хранятся как
  Environment secrets `wotstat-assets-uploader` в `game-unpack-pipeline`. `CLICKHOUSE_HOST`,
  `CLICKHOUSE_USER`, `AWS_REGION`, `AWS_ENDPOINT_URL` и `AWS_BUCKET` хранятся как variables того же
  Environment.
- JIT-конфигурации и installation tokens считаются секретами даже при коротком TTL.
- Не добавлять credentials, project/account identifiers, runner configs или tokens в код,
  fixtures, документацию, summaries и логи.
- Реальные Selectel и GitHub mutations выполнять только по явной задаче пользователя. Локальные
  unit/lint checks безопасны и не обращаются к облаку.
- Не ослаблять masking, отсутствие ingress, разделение Unix-пользователей, digest check runner
  archive или ownership checks ради упрощения.

## Что не входит в систему

- отдельная telemetry задержек scheduled checker;
- отдельная release identity и retry policy сверх status и подавления дубликата активного run;
- внешний status store или история запусков вне Git-истории региональных status-файлов;
- долгоживущие self-hosted runners и постоянная инфраструктура.

Не проектировать эти части без нового запроса.

## Правила изменения

- Перед правками проверять фактические workflows/lifecycle scripts и текущие publisher interfaces
  соседних репозиториев.
- Изменения downloader implementation, stage runner, snapshot contracts и основного download job
  выполнять атомарно в этом репозитории.
- При изменении publisher contract обновлять оба call path симметрично; project-owned reusable
  workflows продолжают вызываться через `@main`.
- Документация описывает реализованное состояние; будущие идеи явно помечать нереализованными.
- После изменений запускать `./scripts/check.sh`. При правках соседних контрактов дополнительно
  запускать их собственные pytest/Ruff/mypy.
