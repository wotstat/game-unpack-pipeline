# Brief для cleanup publisher-кода

## Цель

Упростить `wot-src` и `wot-gui-assets`: удалить остатки неудачных transport-итераций, ненужную
обратную совместимость и shallow abstractions. Итоговый publisher должен оставаться одним глубоким
модулем за существующим CLI interface: caller передаёт snapshot/identity/target/branch и получает
проверенную атомарную публикацию, не зная деталей Git-транспорта.

Сохранять текущую форму приватных функций и тестов не требуется. Сохранять перечисленные ниже
результаты требуется.

## Обязательное чтение

- `../wot-src/AGENTS.md` и `../wot-src/docs/publication-transport.md`;
- `../wot-assets/AGENTS.md` и `../wot-assets/docs/publication-transport.md`;
- локальный reusable caller `.github/workflows/publish-snapshot.yml`;
- pinned publisher revisions в `.github/workflows/ephemeral-light-snapshot.yml`.

Transport-документы содержат историю реальных отказов, рабочий протокол и датированный inventory
bootstrap-веток. Remote inventory нужно перепроверить перед удалением совместимости.

## Что можно свободно упрощать

- приватные helper-функции, dataclass'ы, context manager и их имена;
- тесты приватных helper'ов, если они заменены тестами поведения через CLI/module interface;
- неиспользуемые bootstrap README hashes после проверки всех текущих remote heads;
- всю bootstrap compatibility branch после того, как ни одна production-ветка больше не является
  README-only `init`;
- дублированное вычисление и shallow pass-through layers внутри одного publisher.

Проекция snapshot и Git publication — разные причины изменения. Их можно сделать двумя глубокими
внутренними модулями, но не нужно вводить новые ports/adapters без двух реальных реализаций.
Cross-repository runtime dependency между publisher'ами не добавлять без отдельного решения:
оркестратор намеренно checkout'ит каждый data-репозиторий по независимому pinned SHA.

## Что нельзя принять за legacy-костыль

- commit/tree-only partial fetch существующей data-ветки;
- порог 1 ГБ, batches не больше 1 ГБ и проверка отдельного blob против 100 МиБ;
- уникальный временный ref и кумулятивная сборка полного tree;
- применение deletions до batches;
- равенство object ID staging tree и локального publication tree;
- создание одного final commit через Git Database API на уже загруженном tree;
- non-force update production-ref и сериализация publisher/data-branch без отмены предыдущего run;
- удаление staging-ref при успехе и ошибке;
- разрешение ambiguous network success чтением remote state;
- `unchanged` для тех же данных и новый commit для изменённых данных той же версии;
- независимая полная проверка sealed snapshot каждым publisher.

Следующие уже проверенные идеи не возвращать:

- один большой final Git push после загрузки blobs во временную несвязанную ветку — `send-pack`
  снова формирует большой pack;
- включение staging commits в ancestry version commit — загрязняет production-историю;
- некумулятивные staging commits — последний ref не владеет полным tree;
- сборка десятков тысяч tree entries через GitHub API — наблюдался HTTP 502;
- force push production-ref — скрывает race и может потерять чужую публикацию.

## Acceptance

1. Оба publisher сохраняют свои projection tests и одинаковые transport-инварианты.
2. Их локальные `pytest`, Ruff и mypy проходят.
3. `./scripts/check.sh` проходит в оркестраторе, pinned SHA обновлены полностью и симметрично.
4. Small publication path проверен локально.
5. Если изменён large path, до production pin выполнен реальный full-run с дельтой больше порога;
   проверены final tree, ровно один version commit и отсутствие `publication-staging/*` refs.

Измерять cleanup следует уменьшением interface/implementation complexity и удалёнными строками, а
не числом новых обёрток. Если после удаления модуля его сложность расползается по CLI, workflow и
тестам, модуль был глубоким и удалять его не следовало.
