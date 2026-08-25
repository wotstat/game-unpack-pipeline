# game-unpack-pipeline

Публичный pipeline ручного скачивания, распаковки и публикации снимков клиентов World of Tanks и
«Мира танков». Репозиторий содержит всю основную полезную нагрузку: протоколы WGUS/LSTUS,
download/verify, распаковку VFS, readable transforms, sealed `GameSnapshot`, временную Selectel VM
и вызов publisher workflows.

## Поток

```text
workflow_dispatch
  → одна временная VM, direct public IP и egress-only security group в Selectel
  → три изолированных repository-level JIT runner на этой VM
  → встроенный download job собирает полный sealed GameSnapshot
  → выбранные pinned reusable workflows wot-src и wot-gui-assets публикуют snapshot параллельно
  → cleanup с always() удаляет runner registrations и ресурсы Selectel
  → release name записывается в status параллельно с Telegram-отчётом
  → workflow_run reconciler повторно проверяет ru-7 и ru-9
```

Snapshot не загружается в Actions artifact. Downloader и оба publisher читают один абсолютный путь
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
| `.github/workflows` и `scripts` | Production entrypoint, Selectel lifecycle, три JIT runner, cleanup/reconciliation и отчёты |
| [`wot-src`](https://github.com/wotstat/wot-src) | Reusable publication workflow, проверка snapshot и публикация исходников, XML/PO, AS3, stubs и Gameface |
| [`wot-gui-assets`](https://github.com/wotstat/wot-gui-assets) | Reusable publication workflow, проверка snapshot и публикация `res/gui` без `.py` |

Downloader не является отдельным reusable workflow или внешним продуктом. Его production-интерфейс
совпадает с download job основного workflow; устойчивым seam для publisher остаётся только sealed
`GameSnapshot`. Правила проекции data-репозиториев принадлежат самим publisher.

## Download pipeline

Основная реализация находится в [`src/game_downloader`](src/game_downloader), а GitHub-specific
stage runner и сбор диагностик — в [`.github/scripts`](.github/scripts). Download job выполняет
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
Downloader преобразует Python 2.7 `.pyc`, packed XML, GNU `.mo` и ActionScript из SWC, сохраняет
исходные assets и формирует provenance manifests. `READY` появляется последним.

Формат результата закреплён схемами из [`contracts/v1`](contracts/v1). Publisher получают только
абсолютный snapshot path, snapshot ID и SHA-256 canonical descriptor и повторно проверяют snapshot
перед публикацией.

## Ручной запуск

Точка входа —
[`ephemeral-snapshot.yml`](.github/workflows/ephemeral-snapshot.yml). Workflow всегда строит полный
snapshot до стадии `snapshot` и предоставляет только production inputs:

| Input | Значение |
| --- | --- |
| `target` | `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru`, `mt-public-test` |
| `client_type` | `sd` или `hd` |
| `languages` | Коды через запятую или отдельное значение `ALL`; по умолчанию `EN` |
| `publish_wot_src` | Включить публикацию в `wot-src`; по умолчанию `true` |
| `publish_wot_gui_assets` | Включить публикацию в `wot-gui-assets`; по умолчанию `true` |

Publisher можно независимо отключить для ручного рерана. Если отключить оба, workflow только
соберёт и проверит snapshot, после чего удалит VM. Production-конфигурация зафиксирована как
HighFreq с выделенными ядрами `HFL2.16-32768-256-AMD` в `ru-7b`: 16 vCPU, 32 ГБ RAM и
256 ГБ локального диска. Standard, обычный HighFreq, выбор flavor и location не поддерживаются.

## Проверка новых версий

[`check-game-releases.yml`](.github/workflows/check-game-releases.yml) — отдельный ручной checker
без schedule. Он не создаёт downloader Run и не скачивает клиент: для каждого выбранного target
lightweight probe запрашивает WGUS/LSTUS metadata и один patches chain для объявленной default
language, затем сравнивает `release_name` с [`status`](status).

Workflow предоставляет семь независимых whitelist-чекбоксов. По умолчанию включён только
`wot-eu`, а `dispatch_pipelines` выключен: несовпадение лишь выводится в лог как
`action=would-dispatch`. При явном включении dispatch checker сначала исключает уже ожидающий или
работающий pipeline того же target, после чего запускает отличающиеся targets параллельно на
default branch с `sd`, `ALL` и обоими publisher.

Каждый status-файл содержит только `release_name`; `null` означает, что успешная версия ещё не
зафиксирована. Отсутствующий файл, неверный JSON, лишнее поле или значение другого типа блокирует
dispatch для target. После успешных download, всех включённых publisher и cleanup основной workflow
обновляет status в default branch. Конфигурация ручного run при этом намеренно не входит в status:
оператор сам отвечает за завершение частичных ручных публикаций.

## Reusable publisher workflows

Оркестратор вызывает каждый data-репозиторий напрямую по полному SHA:

```yaml
uses: wotstat/wot-src/.github/workflows/publish-snapshot.yml@<commit-sha>
uses: wotstat/wot-gui-assets/.github/workflows/publish-snapshot.yml@<commit-sha>
```

Это обычные reusable workflows внутри caller run, а не cross-repository dispatch. Каждый workflow
checkout’ит собственный репозиторий через `job.workflow_repository` и `job.workflow_sha`, поэтому
исполняемый publisher-код совпадает с SHA в `uses`. Он получает выделенный JIT runner, локальный
snapshot path, target, version name, snapshot ID и descriptor digest; branch и правила проекции
выводятся из конфигурации самого data-репозитория.

Отсутствующая data-ветка создаётся первой публикацией сразу на version commit. Существующая ветка
обязана содержать `.publication.json`; markerless ref считается чужим состоянием и приводит к hard
failure. Повтор идентичных публикуемых данных возвращает `unchanged` без commit.

Если изменённые Git blobs суммарно превышают 1 ГБ, publisher загружает bounded batches во
временную `publication-staging/...` ветку, кумулятивно собирает полный tree и проверяет его object
ID. Затем GitHub Git Database API создаёт один final version commit, а production-ref обновляется
без force. Staging commits не входят в production-историю и временный ref удаляется также при
ошибке. Подробные transport-инварианты находятся в документации data-репозиториев.

## Cleanup и безопасность

- VM получает три одноразовые JIT-конфигурации с отдельными HOME и runner directories.
- GitHub App выпускает короткоживущие installation tokens: `Administration: write` только для
  регистрации runner и `Contents: write` только для выбранного data-репозитория.
- `GH_APP_PRIVATE_KEY` хранится как repository-level Actions secret только в оркестраторе и явно
  передаётся reusable publisher workflows. `SELECTEL_OS_PASSWORD` хранится только в Environment
  `selectel`; Telegram credentials — только в Environment `telegram`.
- У security group нет ingress rules. GitHub Actions Runner скачивается с официального release URL
  и проверяется по SHA-256.
- Основной cleanup выполняется с `always()` после ошибок downloader и publisher. Reconciler
  идемпотентно ищет только ресурсы с точными ownership-маркерами в `ru-7` и `ru-9`.
- Основной run всегда отправляет Telegram-отчёт после cleanup. Recovery alert приходит только если
  reconciler машинно подтвердил `deleted_count > 0`.
- Для обычного ручного run reconciler стартует по `workflow_run`. Если snapshot был запущен
  checker'ом через repository `GITHUB_TOKEN`, основной workflow после cleanup явно создаёт
  эквивалентный manual reconciler run: GitHub не порождает следующий `workflow_run` в такой
  token-originated цепочке.

Cron/schedule, внешний status store, полная история запусков, GitHub Pages, S3 и БД не входят в
текущую систему.

## Настройка и проверка

Полная настройка Selectel, GitHub App, Environments, variables и Telegram описана в
[`docs/setup.md`](docs/setup.md). `Ephemeral snapshot` сразу создаёт тарифицируемые ресурсы;
`Check game releases` по умолчанию работает как безопасный dry-run.

```text
.github/actions/setup-openstack/
.github/scripts/
.github/workflows/check-game-releases.yml
.github/workflows/ephemeral-snapshot.yml
.github/workflows/reconcile-ephemeral-resources.yml
contracts/v1/
scripts/bootstrap-actions-runner.sh
scripts/release_status.py
scripts/runner_lifecycle.py
src/game_downloader/
tests/
docs/setup.md
```

Локальные проверки не обращаются к GitHub или Selectel:

```bash
./scripts/check.sh
```
