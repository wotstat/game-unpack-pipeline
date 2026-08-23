# Настройка первой интеграции

В инструкции нет реальных credentials. Пароли и private keys следует вводить непосредственно в
GitHub Settings, не отправляя их в issues, commits или логи.

## 1. Selectel

1. Создать отдельный Cloud Platform project `game-unpack-pipeline`.
2. Пополнить баланс аккаунта.
3. Установить для проекта верхние квоты не меньше:
   - 8 cloud servers;
   - для одного runner: 16 vCPU, 32 ГБ RAM и 256 ГБ local disk;
   - для восьми одновременных runner: 128 vCPU, 256 ГБ RAM и 2 ТБ local disk;
   - 8 direct public IP addresses.
4. Создать service user `game-unpack-pipeline-ci`.
5. Выдать ему роль `member` только в проекте `game-unpack-pipeline`.
6. Сохранить пароль service user в менеджере секретов до добавления в GitHub: после создания его
   нельзя будет прочитать повторно.
7. В pool `ru-9`, segment `ru-9a` выбрать:
   - стандартный образ с точным именем `Ubuntu 24.04 LTS 64-bit`;
   - flavor `SL1.16-32768-256` (16 vCPU, 32 ГБ RAM, 256 ГБ локального диска).

Локальный диск принципиален для этой итерации: он удаляется вместе с VM и не оставляет отдельный
network volume.

## 2. Repository-level GitHub App

Создать GitHub App, например `wotstat-game-unpack-runner-manager`:

1. Homepage URL: URL этого репозитория.
2. Webhook: отключён.
3. Callback URL и OAuth не нужны.
4. Repository permissions → **Administration: Read and write**.
5. Другие изменяемые permissions не выдавать.
6. Установить App в организации `wotstat`, выбрав **Only select repositories** и только
   `game-unpack-pipeline`.
7. Скопировать **Client ID** приложения. Числовой App ID для
   `actions/create-github-app-token@v3` не нужен.
8. Сгенерировать private key и сохранить весь PEM, включая строки `BEGIN...` и `END...`.

`Administration: write` нужен для repository endpoints создания JIT-конфигурации и удаления
self-hosted runner. Workflows выпускают короткоживущий installation token через официальный
`actions/create-github-app-token`; private key не покидает GitHub-hosted управляющие jobs.

## 3. GitHub Environment

В `Settings → Environments` создать Environment с точным именем `selectel`.

В **Deployment branches and tags** разрешить только branch `main`. Required reviewers не нужны:
будущий scheduled workflow должен запускаться без ручного approval.

Добавить Environment secrets:

| Secret | Значение |
| --- | --- |
| `GH_APP_PRIVATE_KEY` | Полный PEM private key GitHub App |
| `SELECTEL_OS_PASSWORD` | Пароль service user `game-unpack-pipeline-ci` |

Self-hosted workload job не использует Environment `selectel` и не имеет доступа к этим secrets.

## 4. Repository Variables

В `Settings → Secrets and variables → Actions → Variables` добавить:

| Variable | Значение для текущей итерации |
| --- | --- |
| `GH_APP_CLIENT_ID` | Client ID созданного GitHub App |
| `SELECTEL_OS_AUTH_URL` | `https://cloud.api.selcloud.ru/identity/v3` |
| `SELECTEL_OS_USERNAME` | Имя service user |
| `SELECTEL_OS_USER_DOMAIN_NAME` | ID аккаунта Selectel |
| `SELECTEL_OS_PROJECT_ID` | ID отдельного Cloud Platform project |
| `SELECTEL_OS_REGION_NAME` | `ru-9` |
| `SELECTEL_AVAILABILITY_ZONE` | `ru-9a` |
| `SELECTEL_IMAGE_ID` | `Ubuntu 24.04 LTS 64-bit` (точное OpenStack-имя; UUID тоже поддерживается) |
| `SELECTEL_FLAVOR_ID` | `SL1.16-32768-256` (имя стабильно между пулами, numeric ID — нет) |
| `SELECTEL_PUBLIC_NETWORK_API_URL` | `https://ru-9.cloud.api.selcloud.ru/public-network` |

ID проекта может быть записан как UUID с дефисами или как 32 шестнадцатеричных символа.
OpenStack CLI разрешает передавать образ по уникальному имени. Preflight разрешает его в текущем
регионе и останавливает workflow до создания ресурсов, если имя отсутствует или неоднозначно.
Ручной input `runner_profile=highfreq-16c-32g` временно заменяет эту переменную на стабильное имя
`HFL1.16-32768-240`; этот профиль нужен для контролируемого performance A/B и также проходит
обычный preflight до создания ресурсов.

## 5. Первый запуск

1. Открыть `Actions → Ephemeral snapshot`.
2. Убедиться, что выбрана branch `main`.
3. Выбрать target, client type, languages и режим `light`. Для первого запуска подходят
   `wot-eu`, `sd`, `EN` и включённый `light`.
4. Нажать **Run workflow**.

Ожидаемая последовательность в одном run:

1. `Provision` проверяет authentication, image, flavor, zone и direct-IP quota.
2. Создаёт egress-only security group и direct-public port.
3. Создаёт repository-level JIT runner и VM.
4. Ждёт `VM ACTIVE` и `runner online`.
5. `Workload` устанавливает runtime и выполняет выбранный light или full pipeline от `resolve` до
   `snapshot` видимыми Actions steps.
6. Финальный step публикует таблицу статистики; JSON reports и stderr-логи сохраняются как
   небольшой diagnostic artifact. Сам snapshot с VM не выгружается.
7. `Cleanup` удаляет runner registration, VM, direct-public port и security group.
8. После завершения отдельный `Reconcile ephemeral runner cleanup` подтверждает, что остаточных
   ресурсов нет.

Нормальная кнопка **Cancel workflow** не должна оставлять инфраструктуру: cleanup использует
`always()`, а reconciler запускается по событию `workflow_run: completed`.

## 6. Ручная повторная очистка

Если автоматический reconciler завершился с ошибкой:

1. Скопировать numeric Run ID основного `Ephemeral snapshot` из URL запуска.
2. Посмотреть attempt в интерфейсе запуска (для первого запуска это `1`).
3. Открыть `Actions → Reconcile ephemeral runner cleanup → Run workflow`.
4. Ввести `source_run_id` и `source_run_attempt`.

Reconciler удаляет только ресурсы с детерминированными именами и совпавшими ownership-маркерами.
Если Selectel API временно недоступен, запуск cleanup можно безопасно повторить.

## Первичные справочники

- [GitHub: generate a JIT configuration for a repository runner](https://docs.github.com/en/rest/actions/self-hosted-runners#create-configuration-for-a-just-in-time-runner-for-a-repository)
- [GitHub: `actions/create-github-app-token`](https://github.com/actions/create-github-app-token)
- [Selectel: Public Network API](https://docs.selectel.ru/api/cloud-public-network/)
- [OpenStack CLI: `server create`](https://docs.openstack.org/python-openstackclient/latest/cli/command-objects/server.html#server-create)
