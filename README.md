# game-unpack-pipeline

Публичный оркестратор обработки клиентов World of Tanks и «Мира танков».

Текущая интеграционная итерация проверяет минимальный вертикальный сценарий:

```text
workflow_dispatch
  → GitHub-hosted provision job
  → временная VM в Selectel
  → repository-level GitHub Actions JIT runner
  → light или full GameSnapshot через `game-snapshot-builder@v0.3.6`
  → удаление runner, VM, direct public IP и security group
```

Несколько ручных запусков независимы: имена, labels и облачные ресурсы включают уникальные
`github.run_id` и `github.run_attempt`. Отменяющий `concurrency` намеренно не используется.

## Устройство workflow

- [`ephemeral-light-snapshot.yml`](.github/workflows/ephemeral-light-snapshot.yml) — ручная точка
  входа и lifecycle одной единицы работы: `provision → workload + queue watchdog → cleanup`.
  Input `light` выбирает минимальный интеграционный или полный snapshot.
- [`reconcile-ephemeral-resources.yml`](.github/workflows/reconcile-ephemeral-resources.yml) —
  независимая повторная очистка после завершения или отмены основного workflow. Её также можно
  запустить вручную для конкретных `run_id` и `run_attempt`.

Workload вызывает versioned reusable workflow из публичного
[`wotstat/game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder). Все стадии от
`resolve` до `snapshot` отображаются отдельными GitHub Actions steps, но остаются одной job: JIT
runner выполняет не более одного job. Snapshot живёт только на временной VM; в diagnostic artifact
попадают небольшие JSON reports и stderr-логи стадий.

## Гарантии lifecycle

- VM получает только короткоживущую JIT-конфигурацию конкретного runner. Selectel credentials и
  private key GitHub App на VM не передаются.
- Runner скачивается с официального GitHub release, а архив проверяется по опубликованному
  SHA-256 digest.
- У security group нет ingress rules. Runner сам открывает исходящие HTTPS-соединения к GitHub;
  stateful-фильтрация пропускает ответный трафик.
- Queue watchdog удаляет VM и отменяет workflow, если workload не был назначен за 10 минут.
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
