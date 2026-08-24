# Контекст проекта для агентов

## Назначение репозитория

`game-unpack-pipeline` — публичный оркестратор ручной сборки и публикации снимков клиентов World of
Tanks и «Мира танков». Сейчас репозиторий владеет GitHub Actions workflows, жизненным циклом
временной VM и трёх repository-level JIT runner, вызовом reusable builder/publisher и повторной очисткой
ресурсов.

Скачивание и распаковка игрового клиента, преобразования и формат `GameSnapshot` здесь не
реализуются. Автоматическое обнаружение новых версий и публичная история статусов также пока
отсутствуют.

## Реализованный поток

```text
manual workflow_dispatch
  → provision одной VM, direct public IP и egress-only security group в Selectel
  → три JIT runner в game-unpack-pipeline: builder, wot-src и wot-gui-assets
  → game-snapshot-builder@v0.3.16 на builder runner
  → sealed snapshot на локальном диске VM
  → параллельный workflow_call локального publish-snapshot.yml с pinned publisher commit SHA
  → выбранный light publish в test/light-<target> или full publish в <target>
  → cleanup с always()
  → итоговый Telegram-отчёт
  → отдельный workflow_run reconciler в ru-7 и ru-9
  → Telegram recovery alert только при фактическом удалении остаточных ресурсов
```

Основной workflow поддерживает light, full, benchmark и остановку на выбранной стадии. Benchmark
является неполной выборкой, не может дойти до `snapshot` и не публикуется. Publisher jobs создаются
только когда reusable workflow вернул непустой `snapshot_path` и включён соответствующий
`publish_wot_src`/`publish_wot_gui_assets`. Оба publisher включены по умолчанию. Light и benchmark
взаимно исключаются; `ALL` нельзя смешивать с отдельными кодами языков.

## Границы компонентов и локальные зеркала

- Этот репозиторий: `.github/workflows`, Selectel lifecycle, JIT runner bootstrap, native reusable
  publisher jobs, cleanup/reconciliation.
- [`wotstat/game-snapshot-builder`](https://github.com/wotstat/game-snapshot-builder), локально
  обычно `../game-unpacker`: resolve WGUS/LSTUS, download/verify, client/VFS/readable pipeline,
  Python/XML/MO/AS3 transforms, engine stubs, seal и verify `GameSnapshot`. Оркестратор сейчас
  закрепляет reusable workflow на `v0.3.16`.
- [`wotstat/wot-src`](https://github.com/wotstat/wot-src), локально обычно `../wot-src`:
  независимая проверка snapshot и проекция исходников, XML/PO, AS3, stubs и Gameface.
- [`wotstat/wot-gui-assets`](https://github.com/wotstat/wot-gui-assets), локальный каталог в
  текущем workspace обычно называется `../wot-assets`: независимая проверка snapshot и проекция
  `res/gui` без `.xml` и `.py`.

Не переносить в этот репозиторий протокол WGUS/LSTUS, реализацию builder или правила publisher без
отдельного архитектурного решения.

## Текущие контракты

- Единственная точка запуска сборки — ручной `workflow_dispatch` в
  `.github/workflows/ephemeral-light-snapshot.yml`; имя файла историческое и не означает, что
  workflow ограничен light-режимом.
- Поддерживаются targets `wot-eu`, `wot-na`, `wot-asia`, `wot-common-test`, `wot-cn`, `mt-ru` и
  `mt-public-test`, client types `sd`/`hd`, список языков или `ALL`.
- Light snapshot публикуется только в `test/light-<target>`. Full snapshot публикуется в
  production data-ветку `<target>`. Каждый publisher можно независимо отключить для конкретного
  run; если включены оба, они должны получить одинаковые identity, target, profile и descriptor
  digest.
- Publisher lifecycle находится в локальном reusable workflow оркестратора. Он является частью
  caller run и checkout’ит publisher-код из data-репозитория по точному commit SHA; не возвращать
  cross-repository dispatch/polling и не закреплять publisher-код на плавающем `main`.
- Изменённые Git blobs суммарным размером более 1 ГБ publisher загружает bounded staging pushes во
  временную ветку. Финальный tree и один version commit создаются через GitHub Git Database API с
  проверкой локального tree hash; production-ref обновляется без force, а staging commits не
  входят в production-историю.
- Snapshot не загружается в Actions artifact. Все три runner находятся на одной VM и читают один
  абсолютный путь; builder открывает publisher только traversal к sealed snapshot.
- Все три runner зарегистрированы в `game-unpack-pipeline`. Каждый имеет уникальные имя и label на
  основе `run_id`/`run_attempt`, отдельного
  Unix-пользователя, HOME, runner directory и одноразовую JIT-конфигурацию. Только builder имеет
  `sudo`.
- По умолчанию используется `configured-standard` в `ru-7a`. Профиль
  `highfreq-16c-32g` фиксирован как `HFL1.16-32768-240` и разрешён только в `ru-9a`.
- Основной cleanup обязан выполняться после ошибок обоих publisher. Reconciler должен оставаться
  идемпотентным, искать ресурсы по точным ownership-маркерам и проверять обе поддерживаемые region.
- Основной workflow отправляет Telegram-отчёт после cleanup при любом результате. Reconciler
  отправляет отдельный аварийный alert только при машинно подтверждённом `deleted_count > 0`.
- Несколько pipeline runs независимы. Не добавлять отменяющий concurrency на уровень всего
  оркестратора; publisher сами сериализуют обновления одной data-ветки без отмены предыдущего run.

## Секреты и реальные операции

- Все репозитории, код, workflow, несекретная конфигурация и публикуемые данные должны оставаться
  публичными.
- `GH_APP_PRIVATE_KEY` и `SELECTEL_OS_PASSWORD` хранятся только в Environment `selectel`.
  `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` хранятся только в Environment `telegram`.
  JIT-конфигурации и installation tokens считаются секретами даже при коротком TTL.
- Не добавлять реальные credentials, project/account identifiers, runner configs или tokens в
  код, fixtures, документацию, summaries и логи.
- Не угадывать secret values, фактические квоты или доступность flavor/image. Реальные Selectel и
  GitHub mutations выполнять только по явной задаче пользователя. Локальные unit/lint checks
  безопасны и не обращаются к облаку.
- Не ослаблять masking, отсутствие ingress, разделение Unix-пользователей, проверку digest runner
  archive или ownership checks ради упрощения workflow.

## Что пока не входит в систему

- cron и watcher новых WGUS/LSTUS releases;
- модель release identity/state/retry на уровне оркестратора;
- публичный status store или GitHub Pages;
- processors для S3 и БД;
- долгоживущие self-hosted runners и постоянная инфраструктура.

Не проектировать и не добавлять эти части без нового запроса. Если они становятся текущей задачей,
сначала отделить подтверждённые требования от предложений и обновить этот файл вместе с кодом.

## Правила изменения

- Перед правками проверять фактические workflow и lifecycle-скрипты этого репозитория, текущий
  `build-snapshot.yml` закреплённого builder tag и publisher-код на pinned SHA обоих репозиториев.
- При обновлении builder tag сверять inputs, outputs, stage names, profile semantics и требования к
  runner. Не ссылаться на плавающий `main` builder из production orchestration.
- При изменении publisher contract обновлять оба reusable call path симметрично, закреплять код на
  полных commit SHA и сохранять cleanup при ошибке любого из них.
- Документация должна описывать реализованное состояние. Будущие идеи явно помечать как
  нереализованные, а не как текущую итерацию.
- После изменений запускать `./scripts/check.sh`. При правках, затрагивающих соседние контракты,
  дополнительно использовать их собственные test/lint команды.
