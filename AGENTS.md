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
  → provision одной почасовой Aeza MSKs-5 VM и одноразового SSH-ключа
  → SSH bootstrap вооружает systemd kill switch и закрывает ingress через nftables
  → четыре JIT runner в game-unpack-pipeline: downloader, wot-src, wot-gui-assets и wotstat-assets
  → встроенные downloader stages на downloader runner
  → sealed snapshot на локальном диске VM
  → параллельные reusable workflows из main двух data-репозиториев и wotstat-assets-uploader
  → выбранные production data-ветки target
  → cleanup с always()
  → параллельно запись результата run в status и итоговый Telegram-отчёт
  → после status commit сборка и deployment GitHub Pages
  → workflow_run reconciler подтверждает удаление услуги Aeza и SSH-ключа
  → recovery alert только при deleted_count > 0
```

Отдельный `.github/workflows/check-game-releases.yml` через lightweight WGUS/LSTUS probe проверяет
targets, сравнивает `release_name` с `status/<target>.json` и в dispatch-режиме параллельно запускает
основной workflow для отличающихся targets. Cron `23 * * * *` проверяет все семь targets и всегда
работает в dispatch-режиме. Ручной запуск по умолчанию также проверяет все семь targets и работает в
dispatch-режиме; для dry-run `dispatch_pipelines` нужно явно выключить.

Основной workflow всегда строит полный snapshot до `snapshot`. Dispatch предоставляет
target, client type, languages
и три независимых переключателя `publish_wot_src`,
`publish_wot_gui_assets` и `publish_wotstat_assets`, включённых по умолчанию.

Ручной диагностический `.github/workflows/benchmark-game-download.yml` сравнивает curl,
aria2 и внутренний downloader на одинаковом подмножестве pinned файлов `wot-eu`.
Он использует обычный lifecycle временной VM, проверяет размеры/source hash/SHA-256,
сохраняет performance artifacts и всегда выполняет cleanup. Snapshot consumer и status
production-версий в этом workflow не вызываются.

## Границы компонентов

- `src/game_downloader`, `contracts/v1`, `config`: resolve WGUS/LSTUS, download/verify,
  client/VFS/readable pipeline, Python/XML/MO/web transforms, stubs, seal и verify `GameSnapshot`.
- `.github/workflows`, `.github/scripts`, `scripts`, `status-page`: production entrypoint, stage
  execution и telemetry, Aeza lifecycle, JIT runner bootstrap, publisher calls,
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

- Единственный production workflow — `.github/workflows/process-game-release.yml`; checker только
  dispatch'ит его и не создаёт downloader Run самостоятельно.
- `.github/workflows/check-game-releases.yml` запускается по cron `23 * * * *` и вручную. Cron
  проверяет все семь targets с включённым dispatch; ручной запуск имеет отдельные boolean whitelist
  inputs, все семь target-inputs и `dispatch_pipelines: true` по умолчанию. Реальный dispatch всегда
  использует default branch, `sd`, `ALL` и все три snapshot consumer.
- Единственный профиль — Aeza Moscow Shared `MSKs-5`, product ID `157`, group `2`:
  16 vCPU, 32 ГБ RAM, 480 ГБ NVMe, ОС `ubuntu_2404`. Заказ оплачивается с баланса,
  `term: hour`, `termCount: 1`, `autoProlong: true`. Preflight проверяет доступность,
  котировку не выше €0,53/час, баланс на четыре часа и shared/hourly account limits.
  При нехватке лимитов действует существующий checker retry. Выбора региона, preemptible,
  немедленных полных rerun и Selectel fallback нет.
- Runtime watchdog на GitHub-hosted runner отслеживает VM до завершения downloader и
  включённых consumer в пределах исходного четырёхчасового дедлайна. Артефакты
  `aeza-attempt-<run_id>-<attempt>-provision|monitor` содержат только provider, тариф,
  почасовой режим, reason и timestamp.
- Checker не создаёт downloader Run и не скачивает payload: lightweight probe запрашивает metadata,
  затем один patches chain для объявленной default language. Отсутствующий или некорректный status
  блокирует target; сбои targets изолированы через matrix `fail-fast: false`.
- `status/<target>.json` содержит последнюю успешную пару `release_name`/`readable_version` либо
  bootstrap `null`, а также `last_run` с result, обеими версиями, timestamps, duration, Actions Run
  ID/attempt и URL. После cleanup status-job с `always()` коммитит каждый завершённый run; ошибка не
  продвигает успешную пару. Результат в status определяется provision, downloader и всеми включёнными
  consumer; пропуск включённого consumer считается ошибкой. Ошибки cleanup, queue watchdog и
  уведомлений не отменяют успех обработки в status и остаются видны в GitHub Actions.
  Checker сравнивает только `release_name`. Status-job сериализованы общей
  non-cancelling concurrency-группой и выполняются параллельно Telegram.
- После `failure`/`cancelled` неопубликованная версия повторяется существующим checker: через
  12 часов первые три дня, через 24 часа следующие семь дней, далее через семь дней. Объект `retry`
  в status хранит `release_name`, `first_failed_at` и `last_failed_at`; интервал выбирается по
  возрасту цикла на момент последнего сбоя и отсчитывается от его завершения. До `next_retry_at`
  checker показывает `retry_wait`; активный pipeline того же target исключает повторный dispatch.
  В ручном checker `force_retry: false` по умолчанию; включение обходит только ожидание retry для
  неопубликованных версий. Оно не отменяет dry-run и проверку активного pipeline; cron сохраняет
  расписание повторов.
  Новая версия запускается сразу и при ошибке начинает новый цикл, успех очищает retry state.
  Прямой ручной запуск не ограничен расписанием; ошибка с известной версией учитывается в нём.
- `.github/workflows/deploy-status-page.yml` checkout'ит default branch с полной историей, строит
  `_site` из текущих status и предыдущих версий тех же файлов, создаёт Shields endpoint для каждого
  target и публикует GitHub Pages artifact. Бейдж показывает последнюю успешную `readable_version`,
  а цвет — результат последнего run. Основной workflow вызывает Pages прямо после status commit,
  включая token-originated runs;
  `push` и `workflow_dispatch` используются для изменений страницы и ручной пересборки. История не
  дублируется в накопительном JSON или отдельных run-файлах.
- При чтении checker runs/jobs для Pages HTTP 502 допускает не более четырёх попыток на запрос
  с паузами 5/15/30 секунд; остальные ошибки не повторяются.
- Download job реализован прямо в основном workflow и последовательно выполняет все стадии от
  `resolve` до `snapshot`; внешнего downloader workflow contract нет.
- Для всех targets средняя суммарная скорость download ниже 1 МиБ/с за пять минут выдаёт только
  предупреждение, один раз за период замедления. Для последних 5% предупреждение отложено на
  20 минут с момента входа в хвост. Сетевые таймауты, ограниченные повторы, пять минут без полезного
  прогресса обычного HTTP, лимит download-шага 60 минут и четырёхчасовой kill switch сохраняются.
  HTTP payload скачивает один общий процесс aria2 с file concurrency по числу workers
  (16 на MSKs-5, максимум 32). Крупные файлы используют до 16 соединений с минимальным
  размером split 128 МиБ; без валидатора или advertised Range используется один поток.
  Python опрашивает точный completedLength, сохраняет общий полезный прогресс между
  попытками, делает fsync и проверяет source hash/SHA-256 перед CAS commit; хеширование
  ограничено четырьмя workers. Resume требует совпадающий strong ETag/Last-Modified
  и родной .aria2 bitmap либо проверенный legacy sequential prefix. Sparse payload без
  control-файла перезапускается. Старые Python Range partials перезапускаются однократно.
  RPC слушает только loopback, одноразовый token хранится в private config 0600/dir 0700,
  отсутствует в argv/env/logs. Любая ошибка или отмена останавливает и дожидается aria2.
- Targets: `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru`,
  `mt-public-test`; client types: `sd`/`hd`; languages: список или `ALL`.
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
- Cleanup выполняется после ошибок всех consumer и удаляет платную услугу независимо от
  ошибок GitHub. Reconciler идемпотентен: точное имя содержит hash репозитория и Run ID/attempt;
  до удаления сверяются имя, product ID и `paymentTerm: hour`. У SSH-ключа дополнительно
  сверяется полный ownership descriptor в комментарии public key. HTTP 204 только ставит
  удаление в очередь: cleanup подтверждает `deleted`/404, затем удаляет SSH-ключ.
- API service payload, `secureParameters`, пароли и account identifiers не выводятся.
  Диагностика ошибок содержит HTTP status без тела provider response.
- Ошибки torrent descriptor содержат исходный и известный конечный URL. Userinfo, query и fragment
  удаляются, включая URL в транспортной ошибке; control characters экранируются.
- Bootstrap выполняется через временный Ed25519 SSH-ключ с `autoAssign: false`.
  Приватный ключ существует только во временном каталоге provisioner; host key использует
  отдельный known_hosts с `accept-new`, изменения host key запрещены. JIT и API key передаются
  через SSH stdin, не argv или user data. Заказ и bootstrap не повторяются после неизвестного
  результата. Reconciler находит ресурсы без provision outputs.
- До установки runner вооружается абсолютный systemd timer на четыре часа от начала аренды.
  Root-файл `0600` содержит общий `AEZA_API_KEY`, ID и имя конкретной VM для kill switch;
  ограничений одним сервером/TTL Aeza не предоставляет, этот риск согласован пользователем.
  Аварийный скрипт проверяет ownership и повторяет DELETE/проверку до подтверждения удаления.
- До старта runner nftables закрывает ingress/forward, разрешает established/related,
  loopback и необходимые IPv6 control messages; SSH отключается, authorized_keys удаляется.
  Первичный SSH-доступ необходим до установки firewall.
- Для ручного run reconciler вызывается через `workflow_run`. Token-originated runs и
  отменённые runs явно dispatch'ят его после cleanup и отчётов. Входы source Run ID/attempt
  сохраняются; reconciliation сериализована по этой паре без отмены.
- Основной workflow отправляет после cleanup компактный HTML Telegram-отчёт с человекочитаемым
  target, downloader `readable_version` в формате `x.x.x.x #xxx`, client type, языками и consumer
  state. Введённый `ALL` сохраняется в заголовке буквально, а последняя строка показывает полную
  длительность run рядом со ссылкой. Стоимость VM текущей попытки читается после удаления
  из выполненных Aeza `buy`/`prolong` с учётом бонусного баланса и возвратов `refund`/`compensation`,
  только по service ID этой попытки. Отдельный best-effort шаг передаёт в Environment `telegram`
  только сумму в евроцентах и признак окончательности; ключ остаётся в Environment `aeza`.
  Недоступный billing не мешает cleanup/отчёту и показывается как «стоимость недоступна»;
  если удаление VM не подтверждено, сумма помечается как уже начисленная.
  Reconciler отправляет recovery alert только при машинно
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
  `AEZA_API_KEY` хранится только в Environment `aeza`. Старые Environment `selectel`,
  его secrets и variables `SELECTEL_*` сохранены для отката; текущий код их не читает.
  `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` — только в Environment `telegram`.
- `CLICKHOUSE_PASSWORD`, `AWS_ACCESS_KEY_ID` и `AWS_SECRET_ACCESS_KEY` uploader хранятся как
  Environment secrets `wotstat-assets-uploader` в `game-unpack-pipeline`. `CLICKHOUSE_HOST`,
  `CLICKHOUSE_USER`, `AWS_REGION`, `AWS_ENDPOINT_URL` и `AWS_BUCKET` хранятся как variables того же
  Environment.
- JIT-конфигурации, installation tokens, SSH private keys и Aeza API key считаются секретами.
  `AEZA_API_KEY` на VM доступен только root для аварийного удаления.
- Не добавлять credentials, project/account identifiers, runner configs или tokens в код,
  fixtures, документацию, summaries и логи.
- Реальные Aeza и GitHub mutations выполнять только по явной задаче пользователя. Локальные
  unit/lint checks безопасны и не обращаются к облаку.
- Не ослаблять masking, отсутствие ingress после bootstrap, разделение Unix-пользователей, digest check runner
  archive или ownership checks ради упрощения.

## Что не входит в систему

- отдельная telemetry задержек scheduled checker;
- отдельная release identity сверх `release_name` и внешний планировщик повторов;
- внешний status store или история запусков вне Git-истории региональных status-файлов;
- долгоживущие self-hosted runners и постоянная инфраструктура.
- внешний watchdog или отдельный TTL-reaper вне GitHub Actions и временной VM.

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
