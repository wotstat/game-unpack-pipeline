# game-unpack-pipeline

Публичный оркестратор обработки клиентов World of Tanks и «Мира танков».

Текущая интеграционная итерация проверяет вертикальный сценарий до параллельной публикации
исходников и GUI-ресурсов:

```text
workflow_dispatch
  → GitHub-hosted provision job
  → временная VM в Selectel
  → три repository-level GitHub Actions JIT runner на одной VM
  → light, benchmark или full GameSnapshot через `game-snapshot-builder@v0.3.16`
  → параллельные native workflow `wotstat/wot-src@main` и `wotstat/wot-gui-assets@main`
  → независимая проверка snapshot и version commit в две pure-data ветки
  → удаление трёх runner, VM, direct public IP и security group
```

Несколько ручных запусков независимы: имена, labels и облачные ресурсы включают уникальные
`github.run_id` и `github.run_attempt`. Отменяющий `concurrency` намеренно не используется.

## Устройство workflow

- [`ephemeral-light-snapshot.yml`](.github/workflows/ephemeral-light-snapshot.yml) — ручная точка
  входа и lifecycle одной единицы работы:
  `provision → workload + queue watchdog → publish-wot-src + publish-wot-gui-assets → cleanup`.
  Input `light` выбирает минимальный smoke-сценарий, `benchmark_percent` — детерминированную
  неполную performance-выборку, а `until` ограничивает последнюю запускаемую стадию. Benchmark
  нельзя довести до production snapshot. Каждая стадия выполняется отдельным видимым GitHub step;
  переход проверяет SHA-256 непосредственного checkpoint, но не обходит заново весь завершённый
  prefix. Для каждого шага сохраняются отдельные логи и метрики ресурсов. `runner_profile`
  по умолчанию выбирает Standard 16 vCPU / 32 ГБ для московского `ru-7a`; фиксированный
  HighFreq 16 vCPU / 32 ГБ доступен в `ru-9a`. `selectel_location` атомарно выбирает region,
  availability zone и endpoint публичной сети; по умолчанию используется `ru-7a`, а `ru-9a`
  оставлен fallback.
  Произвольные flavor и location из dispatch передать нельзя.
- [`reconcile-ephemeral-resources.yml`](.github/workflows/reconcile-ephemeral-resources.yml) —
  независимая повторная очистка после завершения или отмены основного workflow. Её также можно
  запустить вручную для конкретных `run_id` и `run_attempt`.

Builder workload вызывает versioned reusable workflow из публичного
[`wotstat/game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder). Все стадии от
`resolve` до `snapshot` остаются одной job: JIT runner выполняет не более одного job, а стадии
отображаются отдельными GitHub Actions steps. После seal две управляющие GitHub-hosted job
параллельно вызывают `publish-snapshot.yml` из веток `main` репозиториев `wot-src` и
`wot-gui-assets`, затем ждут конкретные возвращённые Run ID. В light-режиме данные попадают в
`test/light-<target>`, а полный snapshot — в production-ветку `<target>`. Snapshot не загружается
через Actions: builder и оба publisher читают один локальный путь на VM.
В diagnostic artifact попадают только небольшие JSON reports, stderr-логи стадий и performance
telemetry.

## Гарантии lifecycle

- VM получает только три короткоживущие JIT-конфигурации. Selectel credentials и
  private key GitHub App на VM не передаются.
- Builder и оба publisher работают под разными Unix-пользователями и в разных runner work
  directories. У publisher нет `sudo`; после seal им открывается traversal только к immutable
  snapshot, но не к cache, checkpoints или builder work directory.
- Runner скачивается с официального GitHub release, а архив проверяется по опубликованному
  SHA-256 digest.
- У security group нет ingress rules. Runner сам открывает исходящие HTTPS-соединения к GitHub;
  stateful-фильтрация пропускает ответный трафик.
- Queue watchdog удаляет VM и отменяет workflow, если builder workload не был назначен за 10
  минут.
- Основной cleanup работает с `always()`. Второй `workflow_run` cleanup повторяет удаление по
  точным ownership-маркерам во всех поддерживаемых регионах.
- Cleanup проверяет имя, description и project перед удалением ресурсов. Отсутствующий ресурс
  считается уже очищенным.
- При ошибке публикуется только очищенный хвост serial console. Необработанные VM-логи не
  сохраняются как artifact.

## Настройка

До первого запуска необходимо вручную настроить Selectel, GitHub App и Environment. Точные шаги и
полный список variables/secrets находятся в [docs/setup.md](docs/setup.md).

Workflow создаёт реальные тарифицируемые ресурсы сразу после нажатия **Run workflow**. Отдельного
dry-run режима нет.

## Локальные проверки

Проверки не обращаются к GitHub или Selectel и не создают ресурсы:

```bash
./scripts/check.sh
```
