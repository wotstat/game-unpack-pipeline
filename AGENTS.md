# Контекст проекта для агентов

## Назначение репозитория

`game-unpack-pipeline` — публичный pipeline ручного production-скачивания, распаковки и публикации
снимков клиентов World of Tanks и «Мира танков». Репозиторий владеет основной полезной нагрузкой
downloader, GitHub Actions entrypoint, жизненным циклом временной VM и трёх repository-level JIT
runner, вызовом reusable publisher, cleanup/reconciliation и Telegram-отчётами.

Автоматическое обнаружение новых версий и публичная история статусов отсутствуют.

## Реализованный поток

```text
manual workflow_dispatch
  → provision одной VM, direct public IP и egress-only security group в Selectel
  → три JIT runner в game-unpack-pipeline: downloader, wot-src и wot-gui-assets
  → встроенные downloader stages на downloader runner
  → sealed snapshot на локальном диске VM
  → параллельные pinned reusable workflows data-репозиториев
  → выбранные production data-ветки target
  → cleanup с always()
  → итоговый Telegram-отчёт
  → workflow_run reconciler в ru-7 и ru-9
  → recovery alert только при deleted_count > 0
```

Основной workflow всегда строит полный snapshot до `snapshot`. Dispatch предоставляет только
target, client type, languages и два независимых переключателя
`publish_wot_src`/`publish_wot_gui_assets`, включённых по умолчанию.

## Границы компонентов

- `src/game_downloader`, `contracts/v1`, `config`: resolve WGUS/LSTUS, download/verify,
  client/VFS/readable pipeline, transforms, stubs, seal и verify `GameSnapshot`.
- `.github/workflows`, `.github/scripts`, `scripts`: production entrypoint, stage execution и
  telemetry, Selectel lifecycle, JIT runner bootstrap, publisher calls, cleanup/reconciliation.
- [`wotstat/wot-src`](https://github.com/wotstat/wot-src), локально обычно `../wot-src`: reusable
  publication workflow, независимая проверка snapshot и проекция исходников, XML/PO, AS3, stubs и
  Gameface.
- [`wotstat/wot-gui-assets`](https://github.com/wotstat/wot-gui-assets), локально обычно
  `../wot-assets`: reusable publication workflow, независимая проверка snapshot и проекция
  `res/gui` без `.py`.

Downloader — внутренняя часть этого pipeline. Не выделять его обратно во внешний reusable
workflow или отдельный репозиторий и не переносить сюда правила publisher без отдельного
архитектурного решения.

## Текущие контракты

- Единственная точка ручного production-запуска — `.github/workflows/ephemeral-snapshot.yml`.
- Download job реализован прямо в основном workflow и последовательно выполняет все стадии от
  `resolve` до `snapshot`; внешнего downloader workflow contract нет.
- Targets: `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru`,
  `mt-public-test`; client types: `sd`/`hd`; languages: список или `ALL`. Production location
  зафиксирована как `ru-7b` и не вынесена в dispatch input.
- Каждый publisher можно независимо отключить. Если включены оба, они получают одинаковые target,
  snapshot identity и descriptor digest.
- Publisher lifecycle принадлежит reusable workflow data-репозитория и вызывается прямым
  `uses: owner/repo/.github/workflows/publish-snapshot.yml@<full-sha>`. Не возвращать
  cross-repository dispatch/polling, локальный универсальный publisher workflow или floating
  `main`.
- Called workflow checkout’ит собственный код через `job.workflow_repository` и
  `job.workflow_sha`, а не default checkout caller-репозитория.
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
- Cleanup выполняется после ошибок обоих publisher. Reconciler идемпотентен, ищет ресурсы по
  точным ownership-маркерам и проверяет обе region.
- Основной workflow отправляет после cleanup компактный HTML Telegram-отчёт с человекочитаемым
  target, downloader `readable_version` в формате `x.x.x.x #xxx`, client type, языками и publisher
  state. Введённый `ALL` сохраняется в заголовке буквально, а последняя строка показывает полную
  длительность run рядом со ссылкой. Reconciler отправляет recovery alert только при машинно
  подтверждённом `deleted_count > 0`.
- Не добавлять отменяющий global concurrency. Publisher сериализуют обновления одной data-ветки с
  `cancel-in-progress: false`.

## Секреты и реальные операции

- Код, workflows, несекретная конфигурация и публикуемые данные остаются публичными.
- `GH_APP_PRIVATE_KEY` хранится как repository-level Actions secret только в
  `game-unpack-pipeline` и явно передаётся обоим reusable publisher workflows.
  `SELECTEL_OS_PASSWORD` хранится только в Environment `selectel`.
  `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` — только в Environment `telegram`.
- JIT-конфигурации и installation tokens считаются секретами даже при коротком TTL.
- Не добавлять credentials, project/account identifiers, runner configs или tokens в код,
  fixtures, документацию, summaries и логи.
- Реальные Selectel и GitHub mutations выполнять только по явной задаче пользователя. Локальные
  unit/lint checks безопасны и не обращаются к облаку.
- Не ослаблять masking, отсутствие ingress, разделение Unix-пользователей, digest check runner
  archive или ownership checks ради упрощения.

## Что не входит в систему

- cron и watcher новых WGUS/LSTUS releases;
- release identity/state/retry на уровне orchestrator;
- публичный status store или GitHub Pages;
- processors для S3 и БД;
- долгоживущие self-hosted runners и постоянная инфраструктура.

Не проектировать эти части без нового запроса.

## Правила изменения

- Перед правками проверять фактические workflows/lifecycle scripts и pinned publisher interfaces
  соседних репозиториев.
- Изменения downloader implementation, stage runner, snapshot contracts и основного download job
  выполнять атомарно в этом репозитории.
- При изменении publisher contract обновлять оба call path симметрично и закреплять код на полных
  commit SHA.
- Документация описывает реализованное состояние; будущие идеи явно помечать нереализованными.
- После изменений запускать `./scripts/check.sh`. При правках соседних контрактов дополнительно
  запускать их собственные pytest/Ruff/mypy.
