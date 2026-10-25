# Следующие кандидаты HighFreq: 25 октября 2026

Поиск дополнительного пула после неудачных попыток размещения HFL2 в `ru-6c`.
Реальный контрольный run подтвердил полный успешный pipeline `wot-na` на HighFreq
8/16/240 в `ru-2c`, включая три consumer, cleanup, status и Pages.
По результатам проверки `ru-2c` выбран по умолчанию в основном workflow и checker,
включая cron. Полный workflow занял 60 мин 11 с; cleanup завершился через 59 мин 35 с
после dispatch. Строгое выполнение всего workflow менее чем за час не подтверждено.
Публичный каталог и статус-панель сами по себе не подтверждают свободную ёмкость.

## Кандидаты

| Приоритет | Сегмент / region | Фиксированный flavor | ID в таблице региона | vCPU / RAM / локальный диск |
| --- | --- | --- | --- | --- |
| 1 | `ru-8a` / `ru-8` | `HFL1.16-32768-240` | `8315` | 16 / 32 ГБ / 240 ГБ |
| 2 | `ru-2c` / `ru-2` | `HFL1.8-32768-640` | `8321` | 8 / 32 ГБ / 640 ГБ |

После проверки лимитов проекта вариант `ru-2c` 8/32/640 исключён: доступно
не более 320 ГБ локального диска. Для следующего теста выбран опубликованный
`HFL1.8-16384-240` (ID `8320`): 8 vCPU, 16 ГБ RAM, 240 ГБ диска.
Для `ru-6a`/`ru-6b` также опубликованы меньшие выделенные HighFreq
`HFL2.8-32768-256-AMD` (ID `8058`) и `HFL2.8-32768-256-INTEL` (ID `8107`):
8 vCPU, 32 ГБ RAM, 256 ГБ диска.

Это классический HighFreq без выделенных ядер. В опубликованной таблице
`ru-2` нет варианта HFL1 на 16 vCPU; отсутствие строки не заменяет проверку
каталога API. В `ru-8` также перечислены `HFL1.8-32768-640` и более крупные
конфигурации.
[Таблицы flavor по регионам](https://docs.selectel.ru/cloud-servers/create/configurations/).

В HighFreq без выделенных ядер для `ru-9a`, `ru-2c` и `ru-8a` указан Intel
Xeon Gold 6354: 3,0 ГГц, Turbo Boost 3,6 ГГц при полной нагрузке. Для `ru-2c`
также указан AMD EPYC 9474F 3,6 ГГц. Поэтому `ru-8a` предлагает другой пул
ёмкости, но по одному названию flavor нельзя обещать ускорение относительно
`ru-9a`.
[Процессоры HighFreq](https://docs.selectel.ru/cloud-servers/create/configurations/).

Оба сегмента официально поддерживают HighFreq и Ubuntu; `ru-8a` находится
в Новосибирске, `ru-2c` — в Москве.
[Доступность серверов и образов](https://docs.selectel.ru/cloud-servers/about/cloud-servers-availability-by-location/),
[локации](https://docs.selectel.ru/infrastructure/locations/).

На момент проверки статус-панель показывает `ru-2c` и `ru-8a` работающими.
Это не гарантия успешного размещения выбранной VM.
[Статус-панель](https://selectel.live/?lang=ru).

## API и образ

| Region | Public Network endpoint | Предлагаемая необязательная variable |
| --- | --- | --- |
| `ru-8` | `https://ru-8.cloud.api.selcloud.ru/public-network` | `SELECTEL_IMAGE_ID_RU_8` |
| `ru-2` | `https://ru-2.cloud.api.selcloud.ru/public-network` | `SELECTEL_IMAGE_ID_RU_2` |

Auth URL общий: `https://cloud.api.selcloud.ru/identity/v3`.
[Официальный список URL](https://docs.selectel.ru/api/urls/).

Для образа подходит существующий подход pipeline: variable региона, иначе
поиск уникального `Ubuntu 24.04 LTS 64-bit` в нужном регионе. Уникальность
и фактическое наличие этого имени должны проверяться preflight. UUID образа
из другого региона использовать нельзя.
[Правила выбора образа для CLI](https://docs.selectel.ru/cloud-servers/create/create-server/).

## Почему не следует угадывать HFL1 в ru-6

Матрица доступности упоминает HighFreq без выделенных ядер в `ru-6a` и
`ru-6c`, но таблица фиксированных flavor `ru-6` содержит только HFL2 AMD/INTEL;
таблица процессоров HighFreq без выделенных ядер также не перечисляет `ru-6`.
Эти страницы не доказывают существование конкретного HFL1 в этом пуле.
Перед новым тестом такого варианта нужен положительный ответ каталога
flavor и подтверждение параметров в панели; подстановка имени от `ru-9`
сама по себе недостаточна.
[Матрица доступности](https://docs.selectel.ru/cloud-servers/about/cloud-servers-availability-by-location/),
[конфигурации](https://docs.selectel.ru/cloud-servers/create/configurations/).

## Порядок проверки

Меньшие выделенные профили `ru-6a`/`ru-6b` также получили отказ размещения.
Независимый `ru-2c` с 8/16/240 подтвердил запуск VM и downloader. Скорость нужно
измерить на полном pipeline до cleanup. При добавлении region необходимо одновременно расширить
reconciler и проверку отсутствия VM перед удалением глобального emergency
credential. Контрольные запуски выполнять только для `wot-na`; текущий
`wot-eu` в `ru-9a` не трогать.

## Результаты контрольных запусков

Preflight неудачных запусков подтвердили образ Ubuntu 24.04, выбранный flavor и сегмент.
Запрос создания VM принимался, после чего VM переходила в `ERROR` с fault 500:
`No valid host was found. There are not enough hosts available.` Это подтверждает
отказ размещения выбранных конфигураций, но не всех размеров HighFreq в этих пулах.

| Сегмент | Target / профиль | Run |
| --- | --- | --- |
| `ru-6a` | `wot-na`, `HFL2.16-32768-256-AMD` | [40158120638](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40158120638) |
| `ru-6b` | `wot-na`, `HFL2.16-32768-256-AMD` | [40158328862](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40158328862) |
| `ru-8a` | `wot-na`, `HFL1.16-32768-240` | [40158498209](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40158498209) |
| `ru-6a` | `wot-na`, `HFL2.8-32768-256-AMD` | [40158824711](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40158824711) |
| `ru-6b` | `wot-na`, `HFL2.8-32768-256-INTEL` | [40159106942](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40159106942) |

Успешное размещение: [контрольный `wot-na` в `ru-2c`](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40158920928),
`HFL1.8-16384-240`. Provision подтвердил `VM ACTIVE`, четыре JIT runner подключились,
job downloader дошёл до `Download artifacts`. Все три consumer включены.
Это HighFreq без выделенных ядер, 8 vCPU / 16 ГБ RAM / 240 ГБ локального диска.

## Итог полного прогона ru-2c

Run `40158920928` завершился `success`: опубликованы `wot-src`, `wot-gui-assets`
и данные uploader, записана версия `2.4.1.0 #983` / `2.4.1.5497`, обновлены status и Pages.
Оба Git publisher подтвердили один snapshot identity и descriptor digest.
Cleanup завершился успешно; registrations четырёх runner удалены. Последующий
[reconciler](https://github.com/wotstat/game-unpack-pipeline/actions/runs/40162594770)
завершился успешно, recovery alert пропущен.

Длительности по timestamps GitHub Actions:

| Часть | Длительность |
| --- | --- |
| Provision job | 2 мин 17 с |
| Downloader job, включая установку и отчёты | 45 мин 5 с |
| Скачивание 61,6 ГиБ | 6 мин 7 с |
| Materialize VFS | 7 мин 11 с |
| Python/XML/переводы/web transforms | 7 мин 49 с |
| ActionScript | 6 мин 28 с |
| Seal snapshot | 5 мин 8 с |
| `wot-src` | 5 мин 22 с |
| `wot-gui-assets` | 10 мин 45 с |
| Uploader | 3 мин 17 с |
| Cleanup job | 1 мин 13 с |
| Весь workflow, включая status и Pages | 60 мин 11 с |

Consumer работают параллельно: их длительности не складываются.
От dispatch до завершения cleanup прошло 59 мин 35 с; сохранённый status показывает
59 мин 42 с, потому что записывается после cleanup и до завершения Pages.

Artifact `game-downloader-40158920928-1`, `host-performance.json` и stage performance JSON:

- VM сообщает Intel Xeon Gold 6354, 8 logical CPU; выбраны 8 workers.
- Свежая загрузка: 316 artifacts / 316 attempts, без reuse, resume и range fallback;
  средняя полезная скорость около 172 МиБ/с.
- На `transform-readable` CPU busy 90,25% (7,22 из 8 ядер), на ActionScript 76,81%.
  Максимальный средний CPU steal среди стадий 0,007%.
- У `materialize-vfs` CPU iowait 60,10%, у snapshot 26,77%: увеличение числа CPU
  не устранит всё ожидание диска на этих стадиях.
- Среди измеренных стадий downloader пик занятой памяти 10,89 ГиБ; минимум
  `MemAvailable` 4,73 ГиБ. Swap не использовался. Метрики не покрывают последующие consumer.
- Пик занятого файлового пространства downloader 141,74 ГиБ, минимум свободного
  94,44 ГиБ. Места для этого SD snapshot хватило с запасом; это не замер HD и других targets.

Прямое сравнение скорости с `ru-9a` требует одинакового target и набора данных;
параллельный run `wot-eu` использовал другой target и больше CPU, поэтому ускорение
по сравнению с ним не заявляется. Время менее часа также не гарантирует один
оплачиваемый час: [Selectel учитывает максимум ресурсов за каждый астрономический час](https://docs.selectel.ru/cloud-servers/about/payment/).

Повторная проверка панели 25 октября подтвердила в `ru-2c` нулевое потребление
локального SSD после cleanup и квоту/лимит 320/320 ГБ. При фиксированном диске
240 ГБ это только одна одновременная VM. Для двух runs необходимо минимум 480 ГБ,
для семи — 1680 ГБ, плюс 56 vCPU, 112 ГБ RAM и 7 direct public IP для семи runs.
Checker сохраняет параллельный dispatch разных targets; перед таким запуском
нужно увеличить лимиты. Смена default не вводит сериализацию или failover.
