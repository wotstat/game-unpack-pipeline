# game-unpack-pipeline

Публичный оркестратор обработки клиентов World of Tanks и «Мира танков».

Текущая интеграционная итерация проверяет вертикальный сценарий до публикации исходников:

```text
workflow_dispatch
  → GitHub-hosted provision job
  → временная VM в Selectel
  → два repository-level GitHub Actions JIT runner на одной VM
  → light, benchmark или full GameSnapshot через `game-snapshot-builder@v0.3.15`
  → native workflow `wotstat/wot-src@main`
  → проверка snapshot и commit в временную pure-data ветку
  → удаление обоих runner, VM, direct public IP и security group
```

Несколько ручных запусков независимы: имена, labels и облачные ресурсы включают уникальные
`github.run_id` и `github.run_attempt`. Отменяющий `concurrency` намеренно не используется.

## Устройство workflow

- [`ephemeral-light-snapshot.yml`](.github/workflows/ephemeral-light-snapshot.yml) — ручная точка
  входа и lifecycle одной единицы работы:
  `provision → workload + queue watchdog → publish-wot-src → cleanup`.
  Input `light` выбирает минимальный smoke-сценарий, `benchmark_percent` — детерминированную
  неполную performance-выборку, а `until` ограничивает последнюю запускаемую стадию. Benchmark
  нельзя довести до production snapshot. Каждая стадия выполняется отдельным видимым GitHub step;
  переход проверяет SHA-256 непосредственного checkpoint, но не обходит заново весь завершённый
  prefix. Для каждого шага сохраняются отдельные логи и метрики ресурсов. `runner_profile`
  по умолчанию выбирает фиксированный HighFreq 16 vCPU / 32 ГБ, а Standard оставлен как
  контрольный профиль. Произвольные flavor из dispatch передать нельзя.
- [`reconcile-ephemeral-resources.yml`](.github/workflows/reconcile-ephemeral-resources.yml) —
  независимая повторная очистка после завершения или отмены основного workflow. Её также можно
  запустить вручную для конкретных `run_id` и `run_attempt`.

Builder workload вызывает versioned reusable workflow из публичного
[`wotstat/game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder). Все стадии от
`resolve` до `snapshot` остаются одной job: JIT runner выполняет не более одного job, а стадии
отображаются отдельными GitHub Actions steps. После seal управляющая GitHub-hosted job вызывает
`publish-snapshot.yml` из ветки `main` репозитория `wot-src` и ждёт конкретный возвращённый Run ID.
В тестовом режиме данные попадают в `test/light-<target>`. Snapshot не загружается через Actions:
оба workload читают один локальный путь на VM. В diagnostic artifact попадают только небольшие
JSON reports, stderr-логи стадий и performance telemetry.

## Гарантии lifecycle

- VM получает только две короткоживущие JIT-конфигурации. Selectel credentials и
  private key GitHub App на VM не передаются.
- Builder и publisher работают под разными Unix-пользователями и в разных runner work
  directories. У publisher нет `sudo`; после seal ему открывается traversal только к immutable
  snapshot, но не к cache, checkpoints или builder work directory.
- Runner скачивается с официального GitHub release, а архив проверяется по опубликованному
  SHA-256 digest.
- У security group нет ingress rules. Runner сам открывает исходящие HTTPS-соединения к GitHub;
  stateful-фильтрация пропускает ответный трафик.
- Queue watchdog удаляет VM и отменяет workflow, если builder workload не был назначен за 10
  минут.
- Основной cleanup работает с `always()`. Второй `workflow_run` cleanup повторяет удаление по
  точным ownership-маркерам.
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
