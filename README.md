# game-unpack-pipeline

Публичный оркестратор ручной production-сборки и публикации снимков клиентов World of Tanks и
«Мира танков». Репозиторий управляет временной инфраструктурой и GitHub Actions jobs; скачивание,
распаковка и преобразование клиента находятся в отдельном
[`game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder).

## Поток

```text
workflow_dispatch
  → одна временная VM, direct public IP и egress-only security group в Selectel
  → три изолированных repository-level JIT runner на этой VM
  → game-snapshot-builder@v0.4.0 собирает полный sealed GameSnapshot
  → выбранные pinned reusable workflows wot-src и wot-gui-assets публикуют snapshot параллельно
  → cleanup с always() удаляет runner registrations и ресурсы Selectel
  → Telegram-отчёт
  → workflow_run reconciler повторно проверяет ru-7 и ru-9
```

Snapshot не загружается в Actions artifact. Builder и оба publisher читают один абсолютный путь
на локальном диске VM, работая под отдельными Unix-пользователями и в разных runner directories.
Только builder имеет `sudo`.

Несколько ручных запусков независимы и получают разные имена, labels и облачные ресурсы на основе
`github.run_id` и `github.run_attempt`. Отменяющий `concurrency` на уровне orchestrator не
используется. Обновления одной data-ветки сериализуются reusable workflow соответствующего
publisher с `cancel-in-progress: false`.

## Границы компонентов

| Компонент | Ответственность |
| --- | --- |
| Этот репозиторий | Provision/cleanup Selectel, lifecycle трёх JIT runner, вызов builder и publisher workflows |
| [`game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder) | Resolve версии, download/verify, VFS, Python/XML/MO/AS3, stubs и sealed `GameSnapshot` |
| [`wot-src`](https://github.com/wotstat/wot-src) | Reusable publication workflow, проверка snapshot и публикация исходников, XML/PO, AS3, stubs и Gameface |
| [`wot-gui-assets`](https://github.com/wotstat/wot-gui-assets) | Reusable publication workflow, проверка snapshot и публикация `res/gui` без `.py` |

Оркестратор не содержит протоколы WGUS/LSTUS, реализацию builder или правила проекции
data-репозиториев.

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
| `selectel_location` | `ru-7b` по умолчанию или `ru-9a` |

Publisher можно независимо отключить для ручного рерана. Если отключить оба, workflow только
соберёт и проверит snapshot, после чего удалит VM. Production flavor зафиксирован как HighFreq
`HFL1.16-32768-240`; Standard и выбор flavor не поддерживаются. Location определяет OpenStack
region, availability zone и Selectel Public Network endpoint.

## Reusable workflows publisher

Оркестратор вызывает каждый data-репозиторий напрямую по полному SHA:

```yaml
uses: wotstat/wot-src/.github/workflows/publish-snapshot.yml@<commit-sha>
uses: wotstat/wot-gui-assets/.github/workflows/publish-snapshot.yml@<commit-sha>
```

Это обычные reusable workflows внутри caller run, а не cross-repository dispatch. Каждый workflow
checkout’ит собственный репозиторий через `job.workflow_repository` и `job.workflow_sha`, поэтому
исполняемый publisher-код совпадает с SHA в `uses`. Он получает выделенный JIT runner, локальный
snapshot path, target, snapshot ID и descriptor digest; branch и правила проекции выводятся из
конфигурации самого data-репозитория.

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
- Основной cleanup выполняется с `always()` после ошибок builder и publisher. Reconciler
  идемпотентно ищет только ресурсы с точными ownership-маркерами в `ru-7` и `ru-9`.
- Основной run всегда отправляет Telegram-отчёт после cleanup. Recovery alert приходит только если
  reconciler машинно подтвердил `deleted_count > 0`.

Автоматическое обнаружение релизов, cron, публичный status store, GitHub Pages, S3 и БД не входят в
текущую систему.

## Настройка и проверка

Полная настройка Selectel, GitHub App, Environments, variables и Telegram описана в
[`docs/setup.md`](docs/setup.md). Нажатие **Run workflow** сразу создаёт тарифицируемые ресурсы;
dry-run режима нет.

```text
.github/actions/setup-openstack/
.github/workflows/ephemeral-snapshot.yml
.github/workflows/reconcile-ephemeral-resources.yml
scripts/bootstrap-actions-runner.sh
scripts/runner_lifecycle.py
tests/test_runner_lifecycle.py
docs/setup.md
```

Локальные проверки не обращаются к GitHub или Selectel:

```bash
./scripts/check.sh
```
