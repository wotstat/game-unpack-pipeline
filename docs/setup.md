# Настройка и эксплуатация pipeline

Эта инструкция описывает текущий ручной light/full/benchmark workflow. В ней нет реальных
credentials: пароли и private keys нужно вводить непосредственно в GitHub Settings, не отправляя
их в issues, commits, чаты или логи.

Workflow не имеет dry-run режима. Сразу после нажатия **Run workflow** он создаёт тарифицируемую VM
и direct public IP в Selectel.

## 1. Selectel project и ёмкость

1. Создать отдельный Cloud Platform project, например `game-unpack-pipeline`.
2. Создать отдельного service user, например `game-unpack-pipeline-ci`.
3. Выдать service user роль `member` только в этом project.
4. Сохранить пароль service user в менеджере секретов: после создания его нельзя будет прочитать
   повторно.
5. Пополнить баланс и проверить квоты проекта.
6. Выбрать образ Ubuntu 24.04 x64 и Standard flavor с локальным диском для
   `SELECTEL_IMAGE_ID`/`SELECTEL_FLAVOR_ID`.

Один параллельный pipeline run требует:

- 1 cloud server;
- 1 direct public IP;
- 16 vCPU и 32 ГБ RAM при текущих профилях;
- 256 ГБ local disk для рекомендуемого Standard `SL1.16-32768-256` либо 240 ГБ для
  `HFL1.16-32768-240`.

Для `N` одновременно работающих runs нужны как минимум `N` server и direct-IP slots, `16 × N`
vCPU и `32 × N` ГБ RAM. Оркестратор не отменяет предыдущий ручной run, поэтому лимит реального
параллелизма определяется квотами Selectel и балансом аккаунта.

Локальный диск обязателен для текущей архитектуры: snapshot читают три runner на одной VM, а после
cleanup диск удаляется вместе с сервером. Отдельный network volume workflow не создаёт.

Поддерживаемые location зафиксированы в workflow:

| `selectel_location` | OpenStack region | Public Network endpoint | Профили |
| --- | --- | --- | --- |
| `ru-7a` (по умолчанию) | `ru-7` | `https://ru-7.cloud.api.selcloud.ru/public-network` | `configured-standard` |
| `ru-9a` | `ru-9` | `https://ru-9.cloud.api.selcloud.ru/public-network` | `configured-standard`, `highfreq-16c-32g` |

`configured-standard` берёт flavor из `SELECTEL_FLAVOR_ID`. Рекомендуемое значение для текущей
конфигурации — стабильное имя `SL1.16-32768-256`. Профиль `highfreq-16c-32g` игнорирует эту
переменную и использует `HFL1.16-32768-240`; он разрешён только в `ru-9a`. Если планируется
использовать обе location, image и configured Standard flavor должны однозначно разрешаться в
каждой из них.

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
  только для `game-unpack-pipeline`, где зарегистрированы все три JIT runner;
- reusable publisher `wot-src` — `Contents: write` только для `wot-src`;
- reusable publisher `wot-gui-assets` — `Contents: write` только для `wot-gui-assets`.

`Actions: write` приложению больше не нужен: publisher не запускаются внешним dispatch. Private
key хранится только в Environment `selectel`. Закреплённый reusable publisher использует его на
self-hosted job лишь для выпуска repository-scoped installation token; checkout сохраняет этот
token как credentials для push data-ветки.

## 3. GitHub Environment

В `game-unpack-pipeline` открыть `Settings → Environments` и создать Environment с точным именем
`selectel`.

В **Deployment branches and tags** разрешить только branch `main`. Не включать required reviewers,
если автоматический `workflow_run` reconciler должен выполнять аварийную очистку без ожидания
ручного approval.

Добавить Environment secrets:

| Secret | Значение |
| --- | --- |
| `GH_APP_PRIVATE_KEY` | Полный PEM private key GitHub App |
| `SELECTEL_OS_PASSWORD` | Пароль Selectel service user |

Self-hosted builder не использует Environment `selectel`. Reusable publisher jobs используют его
для `GH_APP_PRIVATE_KEY`, но не обращаются к `SELECTEL_OS_PASSWORD`.

## 4. Repository Variables

В `Settings → Secrets and variables → Actions → Variables` добавить:

| Variable | Значение |
| --- | --- |
| `GH_APP_CLIENT_ID` | Client ID GitHub App |
| `SELECTEL_OS_AUTH_URL` | `https://cloud.api.selcloud.ru/identity/v3` |
| `SELECTEL_OS_USERNAME` | Имя Selectel service user |
| `SELECTEL_OS_USER_DOMAIN_NAME` | ID аккаунта Selectel |
| `SELECTEL_OS_PROJECT_ID` | ID отдельного Cloud Platform project |
| `SELECTEL_IMAGE_ID` | Уникальное имя или UUID выбранного Ubuntu 24.04 x64 image |
| `SELECTEL_FLAVOR_ID` | Уникальное имя или UUID Standard flavor; рекомендуется `SL1.16-32768-256` |

`SELECTEL_OS_PROJECT_ID` может быть UUID с дефисами или 32 шестнадцатеричными символами. Image и
flavor разрешаются OpenStack CLI в выбранном run region. До создания ресурсов preflight проверяет
authentication, image, flavor, availability zone и наличие свободного direct-public-IP slot; при
отсутствующем или неоднозначном имени run завершается на этом этапе.

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
Selectel credentials из Environment `selectel`. Основной workflow всегда отправляет итоговый отчёт
после своей cleanup job. Reconciler отправляет отдельный аварийный alert только если его
`deleted_count` больше нуля. Используемый `appleboy/telegram-action` закреплён на полном commit SHA
версии `v1.1.1`.

## 6. Выбор режима запуска

Открыть `Actions → Ephemeral snapshot`, выбрать branch `main` и заполнить inputs.

Рекомендуемый первый smoke run:

| Input | Значение |
| --- | --- |
| `target` | `wot-eu` |
| `client_type` | `sd` |
| `languages` | `EN` |
| `light` | `true` |
| `benchmark_percent` | `0` |
| `until` | `snapshot` |
| `workers` | `0` |
| `publish_wot_src` | `true` |
| `publish_wot_gui_assets` | `true` |
| `runner_profile` | `configured-standard` |
| `selectel_location` | `ru-7a` |

Такой run собирает sealed light snapshot и публикует обе ветки `test/light-wot-eu`.

Publisher можно включать независимо для каждого run. Например, чтобы обновить только `wot-src`,
оставить `publish_wot_src: true` и установить `publish_wot_gui_assets: false`. Оба переключателя
включены по умолчанию; если отключить оба, workflow соберёт snapshot без публикации.

Для production publish установить `light: false`, оставить `benchmark_percent: 0` и
`until: snapshot`. Результат попадёт в production data-ветку `<target>` обоих репозиториев.
Full run скачивает существенно больше данных и дольше использует VM.

Для performance benchmark установить `light: false`, `benchmark_percent` от `1` до `99` и выбрать
`until` не позднее `finalize-readable`. Benchmark — неполная выборка; builder запрещает стадию
`snapshot`, а publisher jobs без `snapshot_path` не запускаются. `light: true` и
`benchmark_percent > 0` взаимно исключаются.

Для локализации узкого сбоя можно оставить `benchmark_percent: 0` и остановить обычный light/full
run на нужной стадии через `until`. Значение `workers: 0` выбирает число CPU автоматически с
ограничением 32.

## 7. Ожидаемая последовательность jobs

1. `Provision` устанавливает pinned `python-openstackclient==10.2.1`, выполняет preflight и создаёт
   egress-only security group с direct-public port.
2. Provision резервирует все три runner в `game-unpack-pipeline`: builder и по одному runner для
   jobs `wot-src`/`wot-gui-assets`, затем создаёт VM.
3. Cloud-init скачивает официальный Linux/x64 GitHub Actions Runner, проверяет SHA-256 и запускает
   три systemd service под разными Unix-пользователями.
4. Provision ждёт статус VM `ACTIVE` и состояние `online` всех трёх runner.
5. `Workload` вызывает `game-snapshot-builder@v0.3.16`. Стадии от `resolve` до выбранного `until`
   видны отдельными Actions steps; metrics и небольшие diagnostic files загружаются как artifact.
6. После seal builder возвращает snapshot ID, абсолютный path и SHA-256 canonical descriptor.
7. Включённые `Publish wot-src` и `Publish wot-gui-assets` параллельно вызывают
   `publish-snapshot.yml` по закреплённым commit SHA. Reusable jobs входят в основной run,
   checkout’ят код своего data-репозитория через `job.workflow_repository`/`job.workflow_sha` и
   получают одинаковый snapshot contract. Отключённая job получает статус `skipped` и не считается
   ошибкой cleanup.
8. Каждый включённый publisher независимо проверяет snapshot и либо создаёт version commit, либо
   успешно завершает повторную публикацию как `unchanged`.
9. `Cleanup` удаляет все runner registrations, VM, direct-public port и security group.
10. `Telegram report` отправляет итоговые статусы и publisher commit/state после cleanup.
11. `Reconcile ephemeral runner cleanup` после завершения повторяет безопасный поиск и удаление в
    `ru-7` и `ru-9`. Если он вынужден что-либо удалить, приходит отдельный recovery alert.

Queue watchdog ждёт назначения builder workload 10 минут. Каждая self-hosted reusable publisher
job ограничена 60 минутами; её состояние и шаги видны непосредственно в основном run.

## 8. Отмена и повторная очистка

Обычная кнопка **Cancel workflow** не должна оставлять инфраструктуру: основной cleanup использует
`always()`, а после завершения run с branch `main` запускается отдельный `workflow_run` reconciler.

Если автоматический reconciler завершился с ошибкой:

1. Скопировать numeric Run ID исходного `Ephemeral snapshot` из URL.
2. Посмотреть attempt в интерфейсе run; у первого запуска это `1`.
3. Открыть `Actions → Reconcile ephemeral runner cleanup → Run workflow` на `main`.
4. Передать `source_run_id` и `source_run_attempt`.
5. Повторять manual reconciler безопасно, если Selectel или GitHub API временно недоступны.

Reconciler проверяет обе region и удаляет только ресурсы с детерминированными именами и совпавшими
repository/run/attempt ownership-маркерами. Уже отсутствующий ресурс считается очищенным. При
ошибке отдельной операции cleanup продолжает удалять остальные ресурсы, а затем завершает job с
перечнем незавершённых действий.

## 9. Диагностика без утечки секретов

При ошибке cleanup запрашивает serial console и выводит только очищенный хвост. Из него удаляются
JIT-конфигурации, известные tokens/passwords и распространённые GitHub token formats. Полный console
log не сохраняется как artifact.

Нельзя вставлять в issue или лог содержимое cloud-init user data, `/run/actions-runner/*/jit-config`,
GitHub App private key или Selectel password. Если нужно показать сбой, использовать Actions
summary, diagnostic artifact builder и уже очищенный console tail из cleanup job.

## Первичные справочники

- [GitHub: generate a JIT configuration for a repository runner](https://docs.github.com/en/rest/actions/self-hosted-runners#create-configuration-for-a-just-in-time-runner-for-a-repository)
- [GitHub: `actions/create-github-app-token`](https://github.com/actions/create-github-app-token)
- [Selectel: Public Network API](https://docs.selectel.ru/api/cloud-public-network/)
- [OpenStack CLI: `server create`](https://docs.openstack.org/python-openstackclient/latest/cli/command-objects/server.html#server-create)
