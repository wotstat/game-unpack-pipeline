# Контекст проекта для агентов

## Назначение репозитория

`game-unpack-pipeline` — публичный оркестратор ручной production-сборки и публикации снимков
клиентов World of Tanks и «Мира танков». Репозиторий владеет GitHub Actions entrypoint, жизненным
циклом временной VM и трёх repository-level JIT runner, вызовом reusable builder/publisher,
cleanup/reconciliation и Telegram-отчётами.

Скачивание и распаковка клиента, преобразования и формат `GameSnapshot` здесь не реализуются.
Автоматическое обнаружение новых версий и публичная история статусов также отсутствуют.

## Реализованный поток

```text
manual workflow_dispatch
  → provision одной VM, direct public IP и egress-only security group в Selectel
  → три JIT runner в game-unpack-pipeline: builder, wot-src и wot-gui-assets
  → game-snapshot-builder@v0.4.0 на builder runner
  → sealed snapshot на локальном диске VM
  → параллельные pinned reusable workflows data-репозиториев
  → выбранные production data-ветки target
  → cleanup с always()
  → итоговый Telegram-отчёт
  → workflow_run reconciler в ru-7 и ru-9
  → recovery alert только при deleted_count > 0
```

Основной workflow всегда строит полный snapshot до `snapshot`. Dispatch предоставляет только
target, client type, languages, Selectel location и два независимых переключателя
`publish_wot_src`/`publish_wot_gui_assets`, включённых по умолчанию.

## Границы компонентов

- Этот репозиторий: `.github/workflows`, Selectel lifecycle, JIT runner bootstrap, вызов reusable
  workflows, cleanup/reconciliation.
- [`wotstat/game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder), локально
  обычно `../game-unpacker`: resolve WGUS/LSTUS, download/verify, client/VFS/readable pipeline,
  transforms, stubs, seal и verify `GameSnapshot`. Оркестратор закрепляет reusable workflow на
  `v0.4.0`.
- [`wotstat/wot-src`](https://github.com/wotstat/wot-src), локально обычно `../wot-src`: reusable
  publication workflow, независимая проверка snapshot и проекция исходников, XML/PO, AS3, stubs и
  Gameface.
- [`wotstat/wot-gui-assets`](https://github.com/wotstat/wot-gui-assets), локально обычно
  `../wot-assets`: reusable publication workflow, независимая проверка snapshot и проекция
  `res/gui` без `.py`.

Не переносить сюда протокол WGUS/LSTUS, builder или правила publisher без отдельного
архитектурного решения.

## Текущие контракты

- Единственная точка ручного запуска — `.github/workflows/ephemeral-snapshot.yml`.
- Targets: `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru`,
  `mt-public-test`; client types: `sd`/`hd`; languages: список или `ALL`; location: `ru-7a`/`ru-9a`.
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
  абсолютный путь; builder открывает publisher только traversal к sealed snapshot.
- Каждый runner имеет уникальные name/label на основе `run_id`/`run_attempt`, отдельного
  Unix-пользователя, HOME, runner directory и одноразовую JIT-конфигурацию. Только builder имеет
  `sudo`.
- Flavor всегда берётся из `SELECTEL_FLAVOR_ID`; location задаёт zone, region и Public Network
  endpoint.
- Cleanup выполняется после ошибок обоих publisher. Reconciler идемпотентен, ищет ресурсы по
  точным ownership-маркерам и проверяет обе region.
- Основной workflow отправляет Telegram-отчёт после cleanup при любом результате. Reconciler
  отправляет recovery alert только при машинно подтверждённом `deleted_count > 0`.
- Не добавлять отменяющий global concurrency. Publisher сериализуют обновления одной data-ветки с
  `cancel-in-progress: false`.

## Секреты и реальные операции

- Код, workflows, несекретная конфигурация и публикуемые данные остаются публичными.
- `GH_APP_PRIVATE_KEY` и `SELECTEL_OS_PASSWORD` хранятся только в Environment `selectel`.
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

- Перед правками проверять фактические workflows/lifecycle scripts и pinned interfaces соседних
  репозиториев.
- При обновлении builder tag сверять inputs, outputs, stage names и требования к runner. Не
  ссылаться на floating `main`.
- При изменении publisher contract обновлять оба call path симметрично и закреплять код на полных
  commit SHA.
- Документация описывает реализованное состояние; будущие идеи явно помечать нереализованными.
- После изменений запускать `./scripts/check.sh`. При правках соседних контрактов дополнительно
  запускать их собственные pytest/Ruff/mypy.
