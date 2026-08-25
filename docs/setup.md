# Настройка и эксплуатация pipeline

Эта инструкция описывает текущий ручной production workflow и ручной checker новых версий. В ней
нет реальных credentials: пароли и private keys нужно вводить непосредственно в GitHub Settings,
не отправляя их в issues, commits, чаты или логи.

`Process game release` не имеет dry-run режима: сразу после запуска он создаёт тарифицируемую VM и
direct public IP в Selectel. `Check game releases`, напротив, по умолчанию только сравнивает версии
и ничего не создаёт.

## 1. Selectel project и ёмкость

1. Создать отдельный Cloud Platform project, например `game-unpack-pipeline`.
2. Создать отдельного service user, например `game-unpack-pipeline-ci`.
3. Выдать service user роль `member` только в этом project.
4. Сохранить пароль service user в менеджере секретов: после создания его нельзя будет прочитать
   повторно.
5. Пополнить баланс и проверить квоты проекта.
6. Выбрать образ Ubuntu 24.04 x64 для `SELECTEL_IMAGE_ID` в region `ru-7`. Production
   flavor уже зафиксирован в workflow как HighFreq с выделенными ядрами
   `HFL2.16-32768-256-AMD`.

Один параллельный pipeline run требует:

- 1 cloud server;
- 1 direct public IP;
- 16 vCPU и 32 ГБ RAM при текущих профилях;
- 256 ГБ local disk HighFreq с выделенными ядрами `HFL2.16-32768-256-AMD`.

Для `N` одновременно работающих runs нужны как минимум `N` server и direct-IP slots, `16 × N`
vCPU и `32 × N` ГБ RAM. Оркестратор не отменяет предыдущий ручной run, поэтому лимит реального
параллелизма определяется квотами Selectel и балансом аккаунта.

Локальный диск обязателен для текущей архитектуры: snapshot читают три runner на одной VM, а после
cleanup диск удаляется вместе с сервером. Отдельный network volume workflow не создаёт.

Так как Selectel не поддерживает выделенные ядра в `ru-9a`, production location зафиксирована
в workflow и не вынесена в dispatch input:

| Availability zone | OpenStack region | Public Network endpoint |
| --- | --- | --- |
| `ru-7b` | `ru-7` | `https://ru-7.cloud.api.selcloud.ru/public-network` |

Flavor зафиксирован как `HFL2.16-32768-256-AMD`. Он соответствует включённой в панели
опции **Выделенные ядра**; Hyper-Threading (SMT) остаётся включённым по умолчанию. Standard,
обычный HighFreq, выбор flavor и location не поддерживаются.

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
- reusable publisher job `wot-src` — `Contents: write` только для `wot-src`;
- reusable publisher job `wot-gui-assets` — `Contents: write` только для `wot-gui-assets`.

`Actions: write` приложению больше не нужен: publisher не запускаются внешним dispatch. Private
key хранится как repository-level Actions secret `GH_APP_PRIVATE_KEY` только в
`game-unpack-pipeline` и явно передаётся обоим reusable workflows. Data-репозитории собственных
секретов не хранят. Publisher использует ключ на self-hosted job лишь для выпуска
repository-scoped installation token; checkout сохраняет этот token как credentials для push
data-ветки.

## 3. GitHub Environment

В `game-unpack-pipeline` открыть `Settings → Environments` и создать Environment с точным именем
`selectel`.

В **Deployment branches and tags** разрешить только branch `main`. Не включать required reviewers,
если автоматический `workflow_run` reconciler должен выполнять аварийную очистку без ожидания
ручного approval.

Добавить Environment secret:

| Secret | Значение |
| --- | --- |
| `SELECTEL_OS_PASSWORD` | Пароль Selectel service user |

В `Settings → Secrets and variables → Actions → Secrets` добавить repository secret:

| Secret | Значение |
| --- | --- |
| `GH_APP_PRIVATE_KEY` | Полный PEM private key GitHub App |

Self-hosted downloader и reusable publisher jobs не используют Environment `selectel`. Publisher jobs
получают только явно переданный `GH_APP_PRIVATE_KEY` и не обращаются к `SELECTEL_OS_PASSWORD`.

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

`SELECTEL_OS_PROJECT_ID` может быть UUID с дефисами или 32 шестнадцатеричными символами. Image и
фиксированный HighFreq flavor с выделенными ядрами разрешаются OpenStack CLI в `ru-7`. До создания
ресурсов preflight проверяет authentication, image, flavor, availability zone и наличие свободного
direct-public-IP slot; при отсутствующем или неоднозначном имени run завершается на этом этапе.

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

## 6. Ручная проверка новых версий

Открыть `Actions → Check game releases → Run workflow` на `main`. Workflow пока не имеет schedule.
Он предоставляет отдельный checkbox для каждого из семи targets; по умолчанию включён только
`check_wot_eu`. `dispatch_pipelines` по умолчанию выключен, поэтому первый запуск безопасно покажет
в логе одно из состояний:

- `action=none` — release name совпадает со status;
- `action=would-dispatch` — найдено расхождение, но dry-run ничего не запускает;
- `action=already-running` — для target уже есть ожидающий или работающий pipeline.

Итоговый job `Release check report` собирает результаты всех выбранных targets в одну таблицу Job
Summary: сохранённый и найденный release name, результат сравнения и фактическое действие checker.
Ошибки probe, чтения status, поиска активного run и dispatch отображаются в строке своего target.

При `dispatch_pipelines: true` отличающиеся targets запускаются параллельно через основной workflow
на default branch с фиксированными `client_type: sd`, `languages: ALL` и обоими publisher. Ошибка
одного WGUS/LSTUS endpoint не останавливает остальные matrix jobs, но оставляет общий checker run
красным.

Status-файлы находятся в `status/<target>.json` и содержат только `release_name`. Bootstrap `null`
является валидным несовпадением; отсутствующий или повреждённый файл блокирует target. Lightweight
probe выполняет только запрос metadata и один patches chain для default language, не создавая
workspace и не скачивая клиент.

## 7. Ручная обработка игрового релиза

Открыть `Actions → Process game release`, выбрать branch `main` и заполнить inputs.

Рекомендуемый первый run:

| Input | Значение |
| --- | --- |
| `target` | `wot-eu` |
| `client_type` | `sd` |
| `languages` | `EN` |
| `publish_wot_src` | `true` |
| `publish_wot_gui_assets` | `true` |
Такой run собирает полный sealed snapshot и публикует production-ветку `wot-eu` в обоих
data-репозиториях. Первый запуск может занимать много времени и скачивает полный клиент.

Publisher можно включать независимо для каждого run. Например, чтобы обновить только `wot-src`,
оставить `publish_wot_src: true` и установить `publish_wot_gui_assets: false`. Оба переключателя
включены по умолчанию; если отключить оба, workflow соберёт snapshot без публикации.

## 8. Ожидаемая последовательность jobs

1. `Provision` устанавливает pinned `python-openstackclient==10.2.1`, выполняет preflight и создаёт
   egress-only security group с direct-public port.
2. Provision резервирует все три runner в `game-unpack-pipeline`: downloader и по одному runner для
   jobs `wot-src`/`wot-gui-assets`, затем создаёт VM.
3. Cloud-init скачивает официальный Linux/x64 GitHub Actions Runner, проверяет SHA-256 и запускает
   три systemd service под разными Unix-пользователями.
4. Provision ждёт статус VM `ACTIVE` и состояние `online` всех трёх runner.
5. `Download` checkout’ит текущий `game-unpack-pipeline` и запускает встроенный downloader. Все
   стадии от `resolve` до `snapshot` видны отдельными Actions steps; metrics и небольшие
   diagnostic files загружаются как artifact.
6. После seal downloader возвращает version name из WGUS/LSTUS, читаемую версию
   `x.x.x.x #xxx` из корневого `version.xml`, snapshot ID, абсолютный path и SHA-256 canonical
   descriptor.
7. Включённые `Publish wot-src` и `Publish wot-gui-assets` параллельно вызывают reusable workflow
   соответствующего data-репозитория через прямой `uses: ...@<commit-sha>`. Jobs входят в основной
   run, checkout’ят собственный publisher-код через `job.workflow_repository` и
   `job.workflow_sha` и получают одинаковый snapshot contract. Отключённая job получает статус
   `skipped` и не считается ошибкой cleanup.
8. Каждый включённый publisher независимо проверяет snapshot и либо создаёт version commit, либо
   успешно завершает повторную публикацию как `unchanged`.
9. `Cleanup` удаляет все runner registrations, VM, direct-public port и security group.
10. После успешного cleanup `Record release status` записывает WGUS/LSTUS version name в
    `status/<target>.json` на default branch. Параллельные status-job сериализуются и не отменяют
    друг друга. Любой полностью успешный ручной run обновляет региональный status независимо от
    client type, languages и отключённых publisher.
11. `Telegram report` параллельно status-job отправляет компактный HTML-отчёт с читаемой версией
    `x.x.x.x #xxx`, target, client type, языками и publisher state; `published` ведёт ссылкой на
    точный commit, а `ALL` сохраняется в заголовке буквально. Полная длительность run от
    `run_started_at` до формирования отчёта выводится рядом со ссылкой на pipeline.
12. `Reconcile release resources` после завершения повторяет безопасный поиск и удаление в
    `ru-7` и `ru-9`. Обычный ручной run запускает его через `workflow_run`; bot-dispatched run
    явно вызывает тот же workflow после cleanup, поскольку GitHub подавляет следующий
    `workflow_run` в цепочке repository `GITHUB_TOKEN`. Если reconciler вынужден что-либо удалить,
    приходит отдельный recovery alert.

Queue watchdog ждёт назначения download job на downloader runner 10 минут. Каждая self-hosted publisher
job ограничена 60 минутами; её состояние и шаги видны непосредственно в основном run.

## 9. Отмена и повторная очистка

Обычная кнопка **Cancel workflow** не должна оставлять инфраструктуру: основной cleanup использует
`always()`, а после завершения run с branch `main` запускается отдельный `workflow_run` reconciler.

Если автоматический reconciler завершился с ошибкой:

1. Скопировать numeric Run ID исходного `Process game release` из URL.
2. Посмотреть attempt в интерфейсе run; у первого запуска это `1`.
3. Открыть `Actions → Reconcile release resources → Run workflow` на `main`.
4. Передать `source_run_id` и `source_run_attempt`.
5. Повторять manual reconciler безопасно, если Selectel или GitHub API временно недоступны.

Reconciler проверяет обе region и удаляет только ресурсы с детерминированными именами и совпавшими
repository/run/attempt ownership-маркерами. Уже отсутствующий ресурс считается очищенным. При
ошибке отдельной операции cleanup продолжает удалять остальные ресурсы, а затем завершает job с
перечнем незавершённых действий.

## 10. Диагностика без утечки секретов

При ошибке cleanup запрашивает serial console и выводит только очищенный хвост. Из него удаляются
JIT-конфигурации, известные tokens/passwords и распространённые GitHub token formats. Полный console
log не сохраняется как artifact.

Нельзя вставлять в issue или лог содержимое cloud-init user data, `/run/actions-runner/*/jit-config`,
GitHub App private key или Selectel password. Если нужно показать сбой, использовать Actions
summary, diagnostic artifact downloader и уже очищенный console tail из cleanup job.

## Первичные справочники

- [GitHub: generate a JIT configuration for a repository runner](https://docs.github.com/en/rest/actions/self-hosted-runners#create-configuration-for-a-just-in-time-runner-for-a-repository)
- [GitHub: `actions/create-github-app-token`](https://github.com/actions/create-github-app-token)
- [Selectel: Public Network API](https://docs.selectel.ru/api/cloud-public-network/)
- [OpenStack CLI: `server create`](https://docs.openstack.org/python-openstackclient/latest/cli/command-objects/server.html#server-create)
