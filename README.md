# Game Unpack Pipeline

[![wot-eu status](https://img.shields.io/endpoint?url=https%3A%2F%2Fwotstat.github.io%2Fgame-unpack-pipeline%2Fbadges%2Fwot-eu.json)](https://wotstat.github.io/game-unpack-pipeline/#wot-eu)
[![wot-na status](https://img.shields.io/endpoint?url=https%3A%2F%2Fwotstat.github.io%2Fgame-unpack-pipeline%2Fbadges%2Fwot-na.json)](https://wotstat.github.io/game-unpack-pipeline/#wot-na)
[![wot-asia status](https://img.shields.io/endpoint?url=https%3A%2F%2Fwotstat.github.io%2Fgame-unpack-pipeline%2Fbadges%2Fwot-asia.json)](https://wotstat.github.io/game-unpack-pipeline/#wot-asia)
[![wot-cn status](https://img.shields.io/endpoint?url=https%3A%2F%2Fwotstat.github.io%2Fgame-unpack-pipeline%2Fbadges%2Fwot-cn.json)](https://wotstat.github.io/game-unpack-pipeline/#wot-cn)
[![wot-common-test status](https://img.shields.io/endpoint?url=https%3A%2F%2Fwotstat.github.io%2Fgame-unpack-pipeline%2Fbadges%2Fwot-common-test.json)](https://wotstat.github.io/game-unpack-pipeline/#wot-common-test)\
[![mt-ru status](https://img.shields.io/endpoint?url=https%3A%2F%2Fwotstat.github.io%2Fgame-unpack-pipeline%2Fbadges%2Fmt-ru.json)](https://wotstat.github.io/game-unpack-pipeline/#mt-ru)
[![mt-public-test status](https://img.shields.io/endpoint?url=https%3A%2F%2Fwotstat.github.io%2Fgame-unpack-pipeline%2Fbadges%2Fmt-public-test.json)](https://wotstat.github.io/game-unpack-pipeline/#mt-public-test)

Этот проект отслеживает новые версии клиентов World of Tanks и «Мира танков». Когда
проверка обнаруживает обновление, pipeline скачивает клиент, распаковывает его и публикует
полученные данные в открытых репозиториях.

## Как это работает

```text
Проверка новой версии → скачивание и распаковка клиента → публикация данных
```

После завершения обработки обновляется публичная статус-страница. По ней можно узнать актуальную
версию каждого клиента, состояние последнего запуска и посмотреть короткую историю обновлений.

## Результаты распаковки

- [`wot-src`](https://github.com/wotstat/wot-src) содержит читаемые исходники и текстовые данные:
  Python-скрипты, XML, переводы, ActionScript и Gameface.
- [`wot-gui-assets`](https://github.com/wotstat/wot-gui-assets) содержит файлы графического
  интерфейса, изображения и другие ресурсы из `res/gui`.

## Поддерживаемые клиенты

| Игра | Регионы и клиенты |
| --- | --- |
| World of Tanks | Europe, North America, Asia, China и Common Test |
| Мир танков | Россия и Public Test |

## История версий

Каждый клиент публикуется в отдельной региональной ветке. Новая версия игры сохраняется отдельным
Git-коммитом, поэтому историю репозиториев можно использовать для просмотра и сравнения изменений
между выпусками.

## Ссылки

- [Текущий статус и история запусков](https://wotstat.github.io/game-unpack-pipeline/)
- [Текстовые исходники и скрипты](https://github.com/wotstat/wot-src)
- [Графический интерфейс и изображения](https://github.com/wotstat/wot-gui-assets)
- [Техническое введение для разработчиков](docs/introduction.md)
