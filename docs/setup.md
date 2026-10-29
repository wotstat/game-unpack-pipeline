# Настройка и эксплуатация pipeline

`Process game release` создаёт одну платную почасовую VM Aeza и четыре одноразовых JIT runner.
`Check game releases` проверяет все семь targets каждый час (`23 * * * *`) и dispatch'ит
основной workflow для неопубликованных версий с учётом существующего retry state.

## 1. Aeza и ёмкость

Используется только Moscow Shared **MSKs-5**, product ID `157`, group `2`:
16 vCPU, 32 ГБ RAM, 480 ГБ NVMe и один публичный IPv4. ОС — `ubuntu_2404`.
Заказ использует `term: hour`, `termCount: 1`, `autoProlong: true` и оплату с баланса.
Текущая проверенная цена — €0,53/час. Preflight отклоняет более дорогую котировку,
недоступный тариф, недостаточный баланс на четыре часа и заполненный shared/hourly limit.

В аккаунте на 28 октября 2026 доступны две одновременные почасовые VM.
При нехватке лимита target завершается ошибкой; существующий checker повторяет его
по расписанию retry. Автоматического переключения тарифов и Selectel fallback нет.
Выключение VM не прекращает аренду: нужно удалить услугу через API и подтвердить
`status: deleted` или HTTP 404. Отдельные public port и security group в Aeza не создаются.

API-ключ создаётся в настройках аккаунта Aeza. Он имеет права на все услуги аккаунта:
ограничения одним сервером и срока действия у него нет. Это согласованное ограничение
переноса. На VM ключ хранится только в root-файле `0600` для аварийного удаления.

## 2. Repository-level GitHub App

Создать GitHub App, например `wotstat-game-unpack-runner-manager`:

1. Homepage URL: URL `game-unpack-pipeline`.
2. Webhook: отключён.
3. Callback URL и OAuth не нужны.
4. Repository permissions → **Administration: Read and write** — генерация и удаление repository
   JIT runner.
5. Repository permissions → **Contents: Read and write** — checkout publisher и push data-веток.
6. Установить App в организации `wotstat` с **Only select repositories**:
   `game-unpack-pipeline`, `wot-src` и `wot-gui-assets`.
7. Скопировать **Client ID** приложения. Workflow использует `client-id` в
   `actions/create-github-app-token`, числовой App ID не нужен.
8. Сгенерировать private key и сохранить весь PEM, включая строки `BEGIN...` и `END...`.

Workflows создают минимально scoped короткоживущие installation tokens:

- provision, queue-timeout cleanup, основной cleanup и reconciler — `Administration: write`
  только для `game-unpack-pipeline`, где зарегистрированы все четыре JIT runner;
- reusable publisher job `wot-src` — `Contents: write` только для `wot-src`;
- reusable publisher job `wot-gui-assets` — `Contents: write` только для `wot-gui-assets`.

`Actions: write` приложению больше не нужен: publisher не запускаются внешним dispatch. Private
key хранится как repository-level Actions secret `GH_APP_PRIVATE_KEY` только в
`game-unpack-pipeline` и явно передаётся обоим reusable workflows. Data-репозитории собственных
секретов не хранят. Publisher использует ключ на self-hosted job лишь для выпуска
repository-scoped installation token; checkout сохраняет этот token как credentials для push
data-ветки.

## 3. GitHub Environments

Создать Environment **`aeza`** и сохранить в нём secret **`AEZA_API_KEY`**.
Provision, runtime watchdog, cleanup и reconciler используют этот Environment.
Для production разрешить deployment только из `main`; required reviewers не нужны,
если аварийная очистка должна выполняться без ручного подтверждения.
Repository secret `GH_APP_PRIVATE_KEY` и variable `GH_APP_CLIENT_ID` остаются прежними.

Environment `selectel`, его secrets и repository variables `SELECTEL_*` сохранены для
возможного отката из Git-истории. Текущие workflows их не читают.

Создать Environment с точным именем `wotstat-assets-uploader`. В **Deployment branches and tags**
разрешить только branch `main`. Не включать required reviewers, если checker должен автоматически
запускать uploader; если автоматический запуск не нужен, reviewer можно использовать как ручной gate.

Добавить в `wotstat-assets-uploader` чувствительные параметры как Environment secrets:

| Secret |
| --- |
| `CLICKHOUSE_PASSWORD` |
| `AWS_ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` |

Остальные параметры добавить как Environment variables:

| Variable |
| --- |
| `CLICKHOUSE_HOST` |
| `CLICKHOUSE_USER` |
| `AWS_REGION` |
| `AWS_ENDPOINT_URL` |
| `AWS_BUCKET` |

Имя Environment зафиксировано в reusable workflow uploader. Caller не передаёт его как input, но
использует `secrets: inherit`, поскольку GitHub иначе не открывает environment secrets cross-repository
called job. Это включает project-owned workflow uploader в trusted boundary repository secrets
caller, включая техническую доступность `GH_APP_PRIVATE_KEY`, хотя uploader его не использует.

Весь набор остаётся внутри отдельного Environment и доступен только job uploader. `DATA_DIR` не
сохраняется в Environment — основной workflow передаёт абсолютный путь к конкретному sealed snapshot
для каждого run.

## 4. Repository Variables

Обязательна только прежняя `GH_APP_CLIENT_ID`. Тариф, ОС и URL Aeza API зафиксированы
в lifecycle-коде. Credentials и account identifiers не хранятся в коде или variables.

## 5. Telegram-уведомления

1. Создать отдельного бота через [`@BotFather`](https://t.me/BotFather) и сохранить выданный токен
   непосредственно в GitHub Actions Secrets.
2. Написать боту сообщение. Для группы или канала сначала добавить туда бота и отправить новое
   сообщение.
3. Получить числовой `chat.id` через метод
   [`getUpdates`](https://core.telegram.org/bots/api#getupdates). Токен из URL нельзя помещать в
   issue, commit или workflow log.
4. В `Settings → Environments` создать отдельный Environment с точным именем `telegram`.
5. В **Deployment branches and tags** разрешить только branch `main`. Не включать required
   reviewers, wait timer или другие protection rules, которые задержат обязательное финальное
   уведомление.
6. Добавить в Environment `telegram` следующие secrets:

| Secret | Значение |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Токен созданного Telegram-бота |
| `TELEGRAM_CHAT_ID` | Числовой ID личного чата, группы или канала |

Обе notification jobs работают на GitHub-hosted runner с Environment `telegram` и не получают
Aeza credentials из Environment `aeza`. Основной workflow всегда отправляет итоговый отчёт
после своей cleanup job. Reconciler отправляет отдельный аварийный alert только если его
`deleted_count` больше нуля. Используемый `appleboy/telegram-action` закреплён на полном commit SHA
версии `v1.1.1`.

## 6. GitHub Pages

Один раз открыть `Settings → Pages` и в **Build and deployment → Source** выбрать
**GitHub Actions**. Workflow не пытается сам включать Pages через административный token.

После merge страницу можно впервые опубликовать через
`Actions → Deploy status page → Run workflow` на `main`. Дальше основной pipeline вызывает этот
workflow непосредственно после status commit; отдельный `push` trigger также пересобирает страницу
после обычных изменений `status-page`, генератора или workflow. Environment `github-pages` и URL
deployment использует стандартный Pages flow.

Страница собирается только из публичных `status/<target>.json` и их Git-истории. Для этого build job
делает checkout с `fetch-depth: 0`, генерирует `_site`, загружает Pages artifact и передаёт его
deployment job с минимальными `pages: write` и `id-token: write`. Секреты Aeza, Telegram,
ClickHouse и S3 в Pages workflow недоступны.

Стандартный URL проекта после первого deployment:
`https://wotstat.github.io/game-unpack-pipeline/`.

## Диагностика скорости скачивания

`Actions → Benchmark game download → Run workflow` на `main` арендует отдельную почасовую
MSKs-5 с production firewall и четырёхчасовым kill switch. Workflow получает один Acquisition
Plan `wot-eu`/`sd`/`ALL` и сравнивает curl, aria2 с одним соединением на файл, aria2 с Range
и production downloader. Наборы включают 16 файлов 64–256 МиБ и четыре файла от 512 МиБ.
Порядок инструментов меняется между повторами; каждый инструмент получает пустой каталог,
размеры и source hash проверяются, SHA-256 всех результатов должны совпасть.

Время включает `fsync` payload и проверку целостности. Нативные клиенты явно сбрасывают все
загруженные файлы и каталог на диск перед проверкой. Артефакт `download-benchmark-<run_id>-<attempt>` содержит
отдельно время передачи, завершающего `fsync` и проверки, CPU/I/O/network samples, версии инструментов и манифест
выбранных файлов без URL query/credentials. У downloader его внутренняя проверка входит во
время передачи. После измерений cleanup подтверждает удаление VM, временного SSH-ключа и runner.
Workflow не публикует snapshot и не меняет production status.
HTTP payload production downloader передаёт одному процессу aria2 с 16 file workers на
MSKs-5. Хеши и запись в CAS остаются ответственностью Python. Обычный benchmark использует
16 workers; `diagnose: true` дополнительно сравнивает один и четыре workers.
Optional `baseline_sha` принимает полный SHA из истории `main` и запускает прежний downloader
на той же VM и тех же файлах. `baseline_workers` задаёт его file budget (4 по умолчанию).
Исторический импорт проверяется явно; source hash и SHA-256 должны совпасть для всех вариантов.
Reconciler также обрабатывает завершившиеся benchmark runs.
Benchmark сохраняет семейства адресов и fingerprints DNS-ответов CDN без записи IP VM.
Перед замерами один разрешённый public IP каждого CDN hostname закрепляется в `/etc/hosts`
временной VM. Все клиенты используют системный resolver; после измерений удаляются только
собственные строки benchmark. Это исключает выбор разных DNS endpoints между инструментами.
Перед скоростными замерами workflow запускает native HTTP acceptance tests на Linux.
`loopback_control: true` дополнительно сравнивает curl, aria2 с Range и downloader
на тех же проверенных байтах через HTTPS на `127.0.0.1`. Источник держит только
выбранные файлы в памяти; отдельный сертификат доверен только дочерним benchmark-процессам.
Измерения повторяются с записью на обычный диск VM и в `/dev/shm`, чтобы отделить WAN/CDN,
запись на диск и затраты клиента. Контроль также включает исторический downloader, если указан
`baseline_sha`. Публичный ingress остаётся закрытым; исходники игровых файлов в отчёт не входят.
Результаты контролируемого сравнения и ограничения выводов описаны в
[download-performance.md](download-performance.md).

## 7. Автоматическая и ручная проверка новых версий

Workflow автоматически запускается каждый час по расписанию `23 * * * *`: в `00:23`,
`01:23`, ... UTC. Scheduled run проверяет все семь targets и dispatch'ит основной pipeline для
найденных новых версий.

Для ручной проверки открыть `Actions → Check game releases → Run workflow` на `main`. Форма
предоставляет отдельный checkbox для каждого из семи targets. По умолчанию включены все target-чекбоксы
и `dispatch_pipelines`, поэтому ручной запуск сразу запустит основной workflow для всех найденных новых версий.
Все targets используют Aeza Moscow Shared MSKs-5.

- `action=none` — release name совпадает со status;
- `action=would-dispatch` — найдено расхождение, но dry-run ничего не запускает;
- `action=already-running` — для target уже есть ожидающий или работающий pipeline;
- `action=retry-wait` — после ошибки ещё не наступило время следующей попытки;
- `action=force-retry` — ручная проверка обходит ожидание retry, затем проверяет активный pipeline.

Итоговый job `Release check report` собирает результаты всех выбранных targets в одну таблицу Job
Summary: сохранённый и найденный release name, результат сравнения и фактическое действие checker.
Ошибки probe, чтения status, поиска активного run и dispatch отображаются в строке своего target.

При `dispatch_pipelines: true` отличающиеся targets запускаются параллельно через основной workflow
на default branch с фиксированными `client_type: sd`, `languages: ALL` и всеми тремя consumer. Ошибка
одного WGUS/LSTUS endpoint не останавливает остальные matrix jobs, но оставляет общий checker run
красным. После отчёта checker напрямую пересобирает Pages artifact с кликабельным временем проверки;
status-коммит для этого не создаётся.

Status-файлы находятся в `status/<target>.json`. Checker сравнивает найденную версию с
верхнеуровневой успешной `release_name`, а `last_run.release_name` использует только для подавления
повтора `failure` или `cancelled`. Bootstrap `null` является валидным несовпадением, а отсутствующий
или повреждённый файл блокирует target. Lightweight probe выполняет только запрос metadata и один
patches chain для default language, не создавая workspace и не скачивая клиент.

## 8. Ручная обработка игрового релиза

Открыть `Actions → Process game release`, выбрать branch `main` и заполнить inputs.

Рекомендуемый первый run:

| Input | Значение |
| --- | --- |
| `target` | `wot-eu` |
| `client_type` | `sd` |
| `languages` | `EN` |
| `detected_release_name` | оставить пустым |
| `publish_wot_src` | `true` |
| `publish_wot_gui_assets` | `true` |
| `publish_wotstat_assets` | `true` |

Такой run собирает полный sealed snapshot, публикует production-ветку `wot-eu` в обоих
data-репозиториях и запускает временную загрузку в ClickHouse/S3. Первый запуск может занимать много
времени и скачивает полный клиент.

Consumer можно включать независимо для каждого run. Например, чтобы обновить только `wot-src`,
оставить `publish_wot_src: true`, а два других переключателя выключить. Все три переключателя
включены по умолчанию; если отключить все, workflow соберёт snapshot без публикации или upload.

## 9. Lifecycle и аварийная очистка

1. Preflight проверяет каталог, котировку, баланс и лимиты Aeza.
2. Provision генерирует временный Ed25519 ключ, регистрирует его с `autoAssign: false`
   и один раз отправляет заказ. Повтор POST после 502/обрыва запрещён: заказ уже мог
   создать оплачиваемую VM. Reconciler найдёт её без job outputs.
3. Имя VM содержит hash caller-репозитория, исходные Run ID/attempt и instance key.
   Перед удалением проверяются точное имя, product ID и почасовой payment term.
   SSH-ключ дополнительно проверяется по полному ownership descriptor в комментарии public key.
4. После активации VM provision выпускает четыре JIT-конфигурации и по SSH загружает
   root-файлы через stdin. Приватный ключ существует только во временном каталоге
   provisioner. SSH использует отдельный known_hosts с `accept-new`, последующие изменения
   host key завершают provisioning ошибкой. Запрос bootstrap не повторяется.
5. До установки runner вооружается systemd timer с абсолютным дедлайном через четыре
   часа от начала аренды. Аварийный скрипт сверяет ownership и вызывает Aeza DELETE;
   systemd повторяет проверку до подтверждённого удаления.
6. Bootstrap устанавливает nftables: входящие и forwarding закрыты, разрешены loopback,
   established/related и необходимые IPv6 control messages. Одноразовый SSH public key
   удаляется, SSH отключается. Только затем запускаются runner. Первичный SSH-доступ
   необходим до установки firewall; cloud-init в этой интеграции не используется.
7. Архив Actions Runner проверяется по digest официального GitHub release.
   У runner отдельные Unix user, HOME и directory; sudo есть только у downloader.
8. Runtime watchdog на GitHub-hosted runner следит за downloader и включёнными consumer
   в пределах того же дедлайна. При потере VM/дедлайне он отменяет run. После ошибок
   очередь и основной cleanup удаляют VM и регистрации runner; reconciler повторяет
   очистку по детерминированным именам. Немедленных полных rerun нет.
9. HTTP 204 от Aeza DELETE означает постановку в очередь. Cleanup считает ресурс
   удалённым только после `deleted`/404 и затем удаляет загруженный SSH-ключ.
   При сбое очистки VM ключ сохраняется для последующей reconciliation.
10. Для ручных запусков reconciler вызывается через `workflow_run`. Token-originated
    runs явно dispatch'ят его после cleanup и отчётов. Concurrency сериализована по
    исходным Run ID/attempt без отмены. Recovery Telegram alert отправляется только при
    подтверждённом `deleted_count > 0`.

## 10. Отмена и повторная очистка

После отмены запустить `Reconcile release resources`, указав исходные `source_run_id`
и `source_run_attempt`. Повтор безопасен: чужие имена, тарифы и registrations не удаляются.
Reconciler продолжает обработку runner, даже если Aeza недоступен, и удаляет платную VM
независимо от ошибок GitHub. При неуспешной очистке job завершится ошибкой.

## 11. Диагностика и проверка

Артефакты `aeza-attempt-<run_id>-<attempt>-provision|monitor` содержат только provider,
тариф, режим оплаты, reason и timestamp. Полные service payloads, `secureParameters`,
passwords, API keys, SSH private keys и JIT-конфигурации не выводятся.
Для bootstrap используются systemd units `gup-bootstrap` и
`gup-emergency-self-destruct.timer`. Локальная команда `./scripts/check.sh` не обращается
к Aeza или GitHub.

## Первичные справочники

- [Aeza API v2 OpenAPI](https://my.aeza.net/api/v2/docs-json)
- [Aeza Swagger UI](https://my.aeza.net/api/v2/docs)
- [Почасовые серверы Aeza](https://wiki.aeza.net/ru/cabinet/hourly-servers/)
