# game-unpack-pipeline

Публичный оркестратор ручной сборки и публикации снимков клиентов World of Tanks и «Мира
танков». Репозиторий управляет временной инфраструктурой и GitHub Actions jobs; скачивание,
распаковка и преобразование клиента находятся в отдельном
[`game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder).

## Что уже работает

Один ручной запуск выполняет полный вертикальный сценарий:

```text
workflow_dispatch
  → GitHub-hosted provision job
  → временная VM в Selectel
  → три repository-level GitHub Actions JIT runner на одной VM
  → light, benchmark или full pipeline через game-snapshot-builder@v0.3.16
  → sealed GameSnapshot на локальном диске VM
  → параллельные workflow wot-src@main и wot-gui-assets@main
  → независимая проверка и обновление двух pure-data веток
  → удаление runner registrations, VM, direct public IP и security group
  → независимая повторная очистка
```

Несколько ручных запусков получают разные имена, labels и облачные ресурсы на основе
`github.run_id` и `github.run_attempt`. Отменяющий `concurrency` у оркестратора намеренно не
используется. Публикации в одну и ту же data-ветку сериализуются уже в workflow соответствующего
publisher с `cancel-in-progress: false`.

Автоматическое обнаружение релизов, cron, публичная история состояния запусков, GitHub Pages, S3 и
БД пока не реализованы.

## Границы компонентов

| Компонент | Ответственность |
| --- | --- |
| Этот репозиторий | Provision/cleanup Selectel, lifecycle трёх JIT runner, вызов builder и двух publisher |
| [`game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder) | Resolve версии, download/verify, сборка VFS, преобразование Python/XML/MO/AS3, stubs и sealed `GameSnapshot` |
| [`wot-src`](https://github.com/wotstat/wot-src) | Независимая проверка snapshot и публикация исходников, XML/PO, AS3, stubs и Gameface |
| [`wot-gui-assets`](https://github.com/wotstat/wot-gui-assets) | Независимая проверка snapshot и публикация `res/gui` без `.xml` и `.py` |

Оркестратор не содержит и не дублирует протоколы WGUS/LSTUS, логику распаковки клиента или
правила проекции data-репозиториев.

## Режимы запуска

Главная точка входа — [`ephemeral-light-snapshot.yml`](.github/workflows/ephemeral-light-snapshot.yml).
Имя файла историческое: workflow поддерживает не только light, но также full и benchmark runs.

| Сценарий | `light` | `benchmark_percent` | `until` | Результат |
| --- | ---: | ---: | --- | --- |
| Smoke + publish | `true` | `0` | `snapshot` | Sealed light snapshot и две ветки `test/light-<target>` |
| Production publish | `false` | `0` | `snapshot` | Sealed full snapshot и две production-ветки `<target>` |
| Benchmark | `false` | `1`–`99` | До `finalize-readable` | Детерминированная неполная выборка, telemetry, без snapshot и publish |
| Диагностика стадии | По задаче | `0` | Любая стадия до `snapshot` | Checkpoints и diagnostics, без publish |

`light` и `benchmark_percent > 0` взаимно исключаются. Builder также не разрешает довести
benchmark до `snapshot`: неполную выборку нельзя случайно опубликовать как GameSnapshot.

Полный набор dispatch inputs:

| Input | Допустимые значения и поведение |
| --- | --- |
| `target` | `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru`, `mt-public-test` |
| `client_type` | `sd` или `hd` |
| `languages` | Коды через запятую или отдельное значение `ALL`; по умолчанию — `EN` |
| `light` | Минимальный, но проверяемый snapshot; по умолчанию включён |
| `benchmark_percent` | `0` отключает benchmark; `1`–`99` выбирает репрезентативную долю данных |
| `until` | Последняя стадия от `resolve` до `snapshot` |
| `workers` | `0` выбирает число CPU автоматически с ограничением 32; иначе `1`–`32` |
| `runner_profile` | `configured-standard` или `highfreq-16c-32g` |
| `selectel_location` | `ru-7a` по умолчанию или `ru-9a` |

`configured-standard` использует repository variable `SELECTEL_FLAVOR_ID`; текущая конфигурация
рассчитана на Standard 16 vCPU / 32 ГБ в `ru-7a`. Профиль `highfreq-16c-32g` выбирает фиксированный
`HFL1.16-32768-240` и доступен только в `ru-9a`. Location одновременно определяет OpenStack
region, availability zone и endpoint Selectel Public Network API, поэтому произвольные сочетания
region/zone/flavor через dispatch не принимаются.

## Выполнение workflow

1. `Provision` на GitHub-hosted runner получает короткоживущий GitHub App installation token,
   проверяет Selectel authentication, image, flavor, availability zone и свободную квоту direct
   public IP.
2. Lifecycle создаёт security group без ingress rules, direct-public port, три JIT-конфигурации и
   одну VM. На VM запускаются отдельные runner для builder, `wot-src` и `wot-gui-assets`.
3. `Workload` вызывает versioned reusable workflow
   `wotstat/game-snapshot-builder/.github/workflows/build-snapshot.yml@v0.3.16`. Все стадии остаются
   одной job, но отображаются отдельными GitHub Actions steps с собственными логами и resource
   telemetry.
4. `Queue watchdog` отменяет run и удаляет инфраструктуру, если builder workload не получил runner
   за 10 минут.
5. Если builder вернул `snapshot_path`, две GitHub-hosted orchestration jobs параллельно вызывают
   `publish-snapshot.yml@main` в data-репозиториях и ждут именно возвращённые GitHub Run ID.
6. Оба publisher читают один sealed snapshot с локального диска VM, независимо проверяют identity,
   descriptor и payload, после чего обновляют свои data-ветки. Большой snapshot не передаётся через
   Actions artifacts.
7. `Cleanup` с `always()` удаляет три runner registration и ресурсы Selectel даже после ошибки
   workload или любого publisher.
8. [`reconcile-ephemeral-resources.yml`](.github/workflows/reconcile-ephemeral-resources.yml)
   запускается после завершения основного workflow и повторяет идемпотентную очистку в `ru-7` и
   `ru-9`. Его можно запустить вручную по исходным `run_id` и `run_attempt`.

Light snapshot публикуется в `test/light-<target>`, full snapshot — в `<target>`. Точный layout,
формат `.publication.json`, locale overlays и поведение повторной публикации документированы в
README репозиториев [`wot-src`](https://github.com/wotstat/wot-src) и
[`wot-gui-assets`](https://github.com/wotstat/wot-gui-assets).

## Безопасность и lifecycle

- Selectel credentials и private key GitHub App используются только в GitHub-hosted jobs с
  Environment `selectel` и не передаются на VM.
- VM получает три одноразовые JIT-конфигурации. Каждая конфигурация хранится в отдельном файле с
  mode `0600`, удаляется перед стартом runner process и обслуживает не более одной job.
- Builder и publisher работают под разными Unix-пользователями и в разных runner work
  directories. Только builder имеет `sudo`; publisher получают traversal к каталогу sealed
  snapshot, но не к cache, checkpoints или builder work directory.
- GitHub Actions Runner скачивается с официального release URL, а архив проверяется по SHA-256
  digest из GitHub release metadata.
- У security group нет ingress rules. Для работы runner достаточно исходящих HTTPS-соединений.
- Все ресурсы имеют детерминированные ownership-маркеры. Cleanup перед удалением сверяет repository,
  run ID, attempt, имя и description; отсутствующий ресурс считается уже удалённым.
- При неуспешном run cleanup выводит только очищенный хвост serial console. JIT-конфигурации, токены
  и известные secret values маскируются; полный VM log не загружается как artifact.
- Diagnostic artifact builder содержит только небольшие JSON reports, stderr стадий и performance
  telemetry, а не GameSnapshot.

## Настройка и эксплуатация

Перед первым запуском нужно настроить Selectel project/service user, repository-level GitHub App,
Environment `selectel`, secrets и repository variables. Полная инструкция, рекомендации по
квотам, варианты запуска и ручное восстановление находятся в [docs/setup.md](docs/setup.md).

Нажатие **Run workflow** сразу создаёт реальные тарифицируемые ресурсы. Dry-run режима нет.

## Структура репозитория

```text
.github/actions/setup-openstack/          # pinned OpenStack CLI
.github/workflows/ephemeral-light-snapshot.yml
.github/workflows/reconcile-ephemeral-resources.yml
scripts/bootstrap-actions-runner.sh       # cloud-init bootstrap трёх runner
scripts/runner_lifecycle.py                # provision/watch/dispatch/cleanup
tests/test_runner_lifecycle.py
docs/setup.md
```

## Локальные проверки

Проверки не обращаются к GitHub или Selectel и не создают ресурсы:

```bash
./scripts/check.sh
```

Обязательны Python 3 и Ruby с YAML-библиотекой. Если локально установлены `shellcheck` и
`actionlint`, скрипт также запускает их; иначе эти две проверки явно пропускаются.
