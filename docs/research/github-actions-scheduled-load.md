# Поминутная нагрузка scheduled GitHub Actions

Дата исследования: 30 августа 2026 года.

## Вывод

Публичной поминутной heatmap глобальной нагрузки, длины очереди или задержки запуска
scheduled GitHub Actions найти не удалось. GitHub публикует только один устойчивый
паттерн: `schedule` может задерживаться при высокой нагрузке, а к периодам высокой
нагрузки относится начало каждого часа. Если нагрузка достаточно высока, отдельные
queued jobs могут быть отброшены; официальная рекомендация — запускать workflow в
другую минуту часа.

Для выбора конкретного смещения есть дополнительный ориентир: draft-спецификация
[fuzzy schedule от GitHub Next][fuzzy-schedule] описывает известные hotspot-минуты и
предпочтительные окна. Она не является SLA обычного GitHub Actions и не заменяет
реальные метрики, но это более сильное основание, чем случайно выбранная нечётная
минута.

Поэтому для проверки раз в два часа разумная начальная настройка —
`23 */2 * * *`. Минута `:23`:

- находится вне границ часа `[0,4]` и `[55,59]`;
- не попадает в избегаемый диапазон `[27,33]` в 06:00–09:59 UTC;
- не попадает в диапазоны `[12,18]` и `[42,48]` в 14:00–18:59 UTC;
- входит в набор предпочтительных нечётных минут `{7,13,23,37,43,53}`.

Это всё ещё эвристика распределения, а не результат публичной поминутной
статистики. При расписании по умолчанию запуск будет происходить в `00:23`,
`02:23`, ... UTC; GitHub также поддерживает явную IANA timezone в `schedule`.

Источник: [событие `schedule` в документации GitHub Actions][schedule-docs].

## Какие публичные данные доступны

| Источник | Что измеряется | Гранулярность | Подходит для выбора минуты |
| --- | --- | --- | --- |
| GitHub Actions `schedule` docs | Задокументированный риск задержки или потери запуска при высокой нагрузке | Качественное указание: особенно начало часа | Частично: обосновывает уход от `:00`, но не выбор конкретной минуты |
| GitHub Status | Состояние компонента Actions, 90-day uptime и инциденты | Текущее состояние, дневная история uptime и timestamps сообщений об инцидентах | Нет: это аномалии и доступность, а не обычная поминутная нагрузка |
| GitHub Status API | Текущий rollup, состояния компонентов, incidents и maintenance | Снимок текущего состояния и события инцидентов | Нет: API не публикует utilization, queue depth или queue latency time series |
| Actions Performance Metrics | Средние run time, queue time и failure rate своей организации или репозитория | Периоды агрегированы по UTC-дням | Нет: данные не глобальные и не разбиваются по minute-of-hour |
| smplmark Scheduler Latency | Задержка одного публичного hourly workflow GitHub Actions относительно `:00` | Один пробный запуск в час | Частично: подтверждает ненадёжность `:00`, но не сравнивает разные минуты и не измеряет глобальную нагрузку |
| Workflow Runs и Jobs REST API | Timestamps отдельных runs и jobs | Один запуск или job | Только для собственной эмпирики после включения cron |

Ссылки: [GitHub Status][github-status], [GitHub Status API][github-status-api],
[Actions metrics][actions-metrics] и [правила агрегации metrics][metrics-aggregation].

[smplmark Scheduler Latency][smplmark] — ближайший найденный публичный live-сайт с
историей задержек GitHub Actions. Его методика запускает единственный workflow каждый
час строго в `:00`, поэтому он показывает latency конкретного canary, а не поминутную
нагрузку GitHub и не позволяет сравнить `:17`, `:23`, `:37` и другие смещения.

Важно различать две задержки. Предупреждение GitHub относится к созданию scheduled
workflow run в периоды общей нагрузки, а не только к занятости пула
`ubuntu-latest`. После создания run job также может ждать runner. Публичного
глобального ряда ни для одной из этих стадий GitHub не предоставляет.

## Сторонние сайты

Найденные специализированные сервисы — например, [Crontify][crontify],
[Cronping][cronping] и [Sandglass][sandglass] — работают как heartbeat/dead-man's
switch. Конкретный workflow посылает ping после запуска или завершения, а сервис
сообщает о пропущенном, задержанном или неуспешном выполнении. Их публичные описания
не заявляют глобальную агрегацию GitHub Actions queue time по минутам часа.

Такие сервисы могут быть полезны для контроля уже выбранного расписания, но не дают
данных, по которым можно заранее выбрать наименее загруженную минуту.

## Как проверить решение на собственных данных

После включения cron можно фильтровать runs по `event=schedule` через
[Workflow Runs API][workflow-runs-api]. Ответ содержит `created_at` и
`run_started_at`; [Jobs API][workflow-jobs-api] содержит `started_at` и
`completed_at`. Отдельного поля `scheduled_for` API не возвращает, поэтому
ожидаемый timestamp нужно восстанавливать из известного cron-выражения.

Для каждого запуска можно считать:

- задержку создания run: `created_at - expected_cron_time`;
- задержку старта workflow: `run_started_at - expected_cron_time`;
- время от создания run до старта первого job:
  `first_job.started_at - run.created_at`.

Последняя величина включает оркестрацию workflow и ожидание runner. Для зависимых
jobs она дополнительно включает выполнение предыдущих jobs. Jobs API не возвращает
отдельный timestamp постановки job в очередь, поэтому чистую runner queue latency
из этих полей вычислить нельзя.

Несколько недель таких наблюдений покажут поведение именно этого workflow и этого
репозитория. Это полезнее внешней «глобальной» heatmap, которой сейчас нет, но не
доказывает, что та же минута всегда будет свободной: нагрузка GitHub меняется.

## Практическое решение

Переход на cron не стоит блокировать поиском публичной heatmap:

1. Начать с `23 */2 * * *`.
2. Сохранить ручной `workflow_dispatch` для восстановления и тестов.
3. Не добавлять автоматический retry после неуспешного обнаруженного release — это
   соответствует выбранной ручной политике восстановления.
4. На первом этапе не добавлять сбор задержек scheduled runs; при необходимости их можно будет
   оценить позже по Actions API.

[schedule-docs]: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
[fuzzy-schedule]: https://github.github.io/gh-aw/specs/fuzzy-schedule-specification/#64-peak-minutes-avoidance
[github-status]: https://www.githubstatus.com/
[github-status-api]: https://www.githubstatus.com/api/
[actions-metrics]: https://docs.github.com/en/actions/concepts/metrics
[metrics-aggregation]: https://docs.github.com/en/actions/how-tos/administer/view-metrics#understanding-github-actions-metrics-aggregation
[smplmark]: https://www.smplmark.org/benchmarks/smplkit.com/scheduler-latency
[workflow-runs-api]: https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow
[workflow-jobs-api]: https://docs.github.com/en/rest/actions/workflow-jobs
[crontify]: https://crontify.com/blog/github-actions-scheduled-workflow-monitoring
[cronping]: https://cronping.com/solutions/github-actions
[sandglass]: https://sandglass.it/blog/monitor-github-actions-scheduled-workflows
