# Автоматический учёт трат из пуш-уведомлений Сбербанка

Внедренческая инструкция. Цель: ноль ручного ввода — пуш Сбера ловится на телефоне,
парсится, показывается одно своё уведомление с кнопками, по тапу строка улетает в Google-таблицу.

Проверено под: **Samsung Galaxy Z Fold 5, One UI 8.5 / Android 16** (актуально на май 2026),
приложение **СберБанк Онлайн** `ru.sberbankmobile`.

---

## 1. Архитектура (что куда течёт)

```
[Пуш Сбера]  ──►  [MacroDroid: Notification trigger, фильтр пакета ru.sberbankmobile]
                        │  regex → сумма, продавец, тип (расход/доход)
                        ▼
              [Своё уведомление с кнопками: ✓ Добавить · Категория… · Пропустить]
                        │  тап
                        ▼
              [HTTP POST (HTTPS) на ваш Apps Script Web App + секретный токен]
                        ▼
              [doPost: проверка токена, автокатегория, дедуп, appendRow] ──► [Google Sheet]
```

Данные идут **только** с телефона напрямую в вашу личную Google-таблицу. Никаких сторонних
облаков-посредников в основном варианте.

---

## 2. Выбор инструмента

| Инструмент | Чтение пушей | Кнопки/диалог | HTTP | Цена | Вердикт |
|---|---|---|---|---|---|
| **MacroDroid** | Триггер `Notification` из коробки, magic-text `{notification}`, `{not_text_big}`, `{not_title}` | `Display Notification` до 3 кнопок + `Choice/Options Dialog` | `HTTP Request` (POST, form/JSON) | **разовая** Pro-лицензия (free — лимит 5 макросов) | **РЕКОМЕНДАЦИЯ** |
| Tasker + AutoNotification | Мощнее, гибкий парсинг/JSON | да | да | 2 разовые покупки | Альтернатива для «хочу всё руками» |
| Automate (LlamaLab) | блок Notification Posted | да, но возни больше | да | free до 30 блоков | Бесплатно, но UX кнопок собирать дольше |

**Рекомендация — MacroDroid:** один app, всё нужное встроено, разовая оплата (без подписки),
самый короткий путь к сценарию «да/нет тапом». Tasker мощнее в парсинге, но это две платные
программы и круче кривая входа — берите его только если хотите сложную логику.

> **Вариант 2 (мощнее, если приватность позволяет) — переиспользовать вашего Telegram-бота.**
> MacroDroid POST‑ит **сырой текст** пуша вашему боту на Railway → Claude API парсит сумму/продавца/категорию
> (LLM ест любой формат, включая скупые пуши) → бот шлёт сообщение с inline-кнопками `[Добавить][Категории]`
> → пишет в тот же Sheet. Плюсы: парсинг качественнее регулярок, кнопки в Telegram надёжнее и работают
> с любого устройства, переиспользуется готовая инфраструктура. Минус: банковский текст проходит через
> Railway и Claude API — это **сторонние сервисы**, что противоречит строгому требованию «только в Google».
> Но вы **уже** отправляете туда скриншоты выписок, т.е. граница доверия та же. Если она вас устраивает —
> это объективно лучший путь по качеству парсинга. Ниже описан **основной (строгий по приватности)** вариант
> на Apps Script; чтобы переключиться на Вариант 2, замените URL и тело POST на эндпоинт бота — остальное
> (триггер, battery, дедуп) идентично.

---

## Часть A — сторона Google (Apps Script Web App)

### A1. Подготовка таблицы
1. Откройте вашу бюджетную Google-таблицу.
2. Создайте лист **`Траты`** со строкой заголовков:
   `Дата | Сумма | Валюта | Продавец | Категория | Тип | Сырой текст`
3. Скопируйте **ID таблицы** из URL: `…/spreadsheets/d/`**`<ЭТОТ_ID>`**`/edit`.

### A2. Код `doPost`
**Расширения → Apps Script**, вставьте (замените 3 константы вверху):

```javascript
const SHEET_ID   = 'ВСТАВЬТЕ_ID_ТАБЛИЦЫ';
const SHEET_NAME = 'Траты';
const TOKEN      = 'ЗАМЕНИТЕ_НА_ДЛИННУЮ_СЛУЧАЙНУЮ_СТРОКУ'; // напр. 40+ символов
const DEDUP_MIN  = 3;  // окно дедупликации (минут): 1 списание = 2 пуша

// продавец (подстрока, нижний регистр) -> категория
const CATEGORY_MAP = {
  'пятёроч':'Продукты','пятероч':'Продукты','магнит':'Продукты','перекрест':'Продукты',
  'вкусвилл':'Продукты','лента':'Продукты','ашан':'Продукты','дикси':'Продукты',
  'yandex.taxi':'Такси','яндекс.такси':'Такси','uber':'Такси','citymobil':'Такси',
  'ozon':'Маркетплейсы','озон':'Маркетплейсы','wildberries':'Маркетплейсы','wb ':'Маркетплейсы',
  'аптек':'Здоровье','апте':'Здоровье','здравсити':'Здоровье',
  'azs':'Авто','газпромнефт':'Авто','лукойл':'Авто','роснефт':'Авто','tatneft':'Авто',
  'yandex.plus':'Подписки','okko':'Подписки','kinopoisk':'Подписки','vk ':'Подписки',
};

function doPost(e) {
  try {
    const b = readBody_(e);
    if (b.token !== TOKEN) return json_({ ok:false, error:'bad token' });

    const amount   = normAmount_(b.amount);
    const merchant = String(b.merchant || '').trim();
    const type     = String(b.type || 'расход').trim();
    const currency = String(b.currency || '₽').trim();
    const raw      = String(b.raw || '');
    let   category = String(b.category || '').trim();
    const when     = b.datetime ? new Date(b.datetime) : new Date();

    if (!category) category = guessCategory_(merchant);

    // дедуп: одно списание может прийти двумя пушами
    const cache = CacheService.getScriptCache();
    const key = 'tx_' + Utilities.base64EncodeWebSafe(type + '|' + amount + '|' + merchant);
    if (cache.get(key)) return json_({ ok:true, dup:true });
    cache.put(key, '1', DEDUP_MIN * 60);

    // знак суммы: доход +, расход −
    const isIncome = /^доход/i.test(type) || /пополн|зачисл|поступл|возврат|внесен/i.test(type + ' ' + raw);
    const signed = isIncome ? Math.abs(amount) : -Math.abs(amount);

    const sh = SpreadsheetApp.openById(SHEET_ID).getSheetByName(SHEET_NAME);
    sh.appendRow([when, signed, currency, merchant, category, type, raw]);
    return json_({ ok:true, category:category, amount:signed });
  } catch (err) {
    return json_({ ok:false, error:String(err) });
  }
}

function doGet() { return json_({ ok:true, ping:'alive' }); } // для быстрой проверки в браузере

function readBody_(e) {
  // поддержка и form-urlencoded (надёжнее для банковского текста), и JSON
  if (e && e.postData && e.postData.type && e.postData.type.indexOf('json') !== -1) {
    try { return JSON.parse(e.postData.contents); } catch (_) {}
  }
  return (e && e.parameter) ? e.parameter : {};
}

function normAmount_(v) {
  if (v == null) return 0;
  const s = String(v).replace(/[\s ]/g, '').replace(',', '.').replace(/[^\d.]/g, '');
  const n = parseFloat(s);
  return isNaN(n) ? 0 : Math.round(n * 100) / 100;
}

function guessCategory_(merchant) {
  const m = merchant.toLowerCase();
  for (const k in CATEGORY_MAP) if (m.indexOf(k) !== -1) return CATEGORY_MAP[k];
  return 'Без категории';
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
                       .setMimeType(ContentService.MimeType.JSON);
}
```

### A3. Деплой как Web App
1. В редакторе: **Deploy → New deployment → ⚙ → Web app**.
2. **Execute as:** `Me (ваш аккаунт)`.
3. **Who has access:** `Anyone`.
   *Почему «Anyone», а не «Anyone with Google account»: MacroDroid не проходит Google-OAuth,
   поэтому эндпоинт должен принимать анонимный POST. Защита — секретный `TOKEN` в теле запроса.*
4. **Deploy** → подтвердите права (Authorize → ваш аккаунт → Advanced → Go to … → Allow).
5. Скопируйте **Web app URL** вида `https://script.google.com/macros/s/XXXX/exec`.

> При изменении кода делайте **Deploy → Manage deployments → ✏ → Version: New version**, иначе
> правки не подхватятся (URL остаётся прежним).

### A4. Проверка из терминала
```bash
# ping
curl -sL "https://script.google.com/macros/s/XXXX/exec"
# тестовая запись (-L обязателен: exec редиректит на googleusercontent)
curl -sL "https://script.google.com/macros/s/XXXX/exec" \
  --data-urlencode "token=ВАШ_ТОКЕН" \
  --data-urlencode "type=расход" \
  --data-urlencode "amount=1 234,56" \
  --data-urlencode "merchant=Пятёрочка" \
  --data-urlencode "currency=₽" \
  --data-urlencode "raw=Покупка 1 234,56 ₽ Пятёрочка"
```
Ожидаемо: `{"ok":true,"category":"Продукты","amount":-1234.56}` и новая строка в листе `Траты`.

---

## Часть B — сторона телефона (MacroDroid)

### B0. Доступ к уведомлениям + ПЕРВЫМ ДЕЛОМ снять реальный формат пуша
Формат пушей Сбера разнится по версиям (в 2025 добавили «дружелюбный тон»: `Покупка …` vs
`Потратили …`), и в скупых случаях бывает `Списание 1234 ₽` вообще без продавца. **Не угадывайте —
снимите свой текст:**

1. Установите MacroDroid, выдайте доступ: **Настройки телефона → Уведомления → Дополнительно →
   Доступ к уведомлениям** (или *Настройки → Приложения → Спец. доступ → Доступ к уведомлениям*) →
   включите **MacroDroid**.
2. Сделайте временный макрос «DUMP»:
   - **Триггер:** `Уведомления (Notification)` → Applications: **СберБанк Онлайн** → *Trigger when: Notification received*.
   - **Действие:** `Уведомление (Display Notification)`, заголовок `DUMP`, текст:
     `T:{not_title} | B:{notification} | BIG:{not_text_big}`
3. Проведите мелкую реальную операцию (купите кофе / переведите 1 ₽). Прочитайте свой точный текст —
   на нём стройте регулярки ниже. Удалите DUMP-макрос потом.

Доступные magic-text переменные триггера: `{notification}` (тело), `{not_text_big}` (развёрнутый текст —
часто полнее), `{not_title}`, `{not_app_name}`, `{not_app_package}`.

### B1. Основной макрос «Sber → подтверждение»

**Триггер:**
- `Уведомления` → Applications: **СберБанк Онлайн** (`ru.sberbankmobile`) → *Notification received*.

**Действие 1 — отсечь не-транзакционные пуши (реклама и т.п.):**
- `Условие/IF` → *Expression* → **Regular expression** над `{not_text_big}`:
  `(?i)(\d[\d  ]*[.,]?\d*)\s*(₽|руб|р\.?|RUB|\$|€|USD|EUR)`
  Если **не** совпало → `Stop Macro` (дальше не идём).

**Действие 2 — вытащить сумму (Text Manipulation → Regular Expression):**
- Вход: `{not_text_big}`; операция **Regular Expression**; шаблон:
  `(\d[\d  ]*(?:[.,]\d{1,2})?)\s*(?:₽|руб|р\.?|RUB|\$|€|USD|EUR)`
- Результат в массив `match` → возьмите `match[1]` в переменную **`lv_amount`**.

**Действие 3 — валюта:**
- Regular Expression над `{not_text_big}`: `(₽|руб|RUB|\$|€|USD|EUR)` → `lv_currency` (по умолчанию `₽`).

**Действие 4 — тип операции (расход/доход):**
- `IF` Regular expression над `{not_text_big}`: `(?i)(пополн|зачисл|поступл|возврат|внесен)`
  - совпало → `lv_type = доход`
  - иначе → `lv_type = расход`

**Действие 5 — продавец (самое форматозависимое — адаптируйте по DUMP):**
- Частые шаблоны (попробуйте по очереди, оставьте подходящий):
  - после суммы и до «Баланс/Доступно»:
    `(?:₽|руб|р)\.?\s*[,. ]*([^,.\n]+?)\s*(?:Баланс|Доступно|$)`
  - продавец в конце строки: `([A-Za-zА-Яа-я0-9.\- ]{2,})\s*$`
- Результат → `lv_merchant`. Если пусто — оставьте пустым (сервер поставит «Без категории»).

**Действие 6 — показать своё уведомление с кнопками** (`Display Notification`):
- Заголовок: `{lv_type} {lv_amount} {lv_currency}`
- Текст: `{lv_merchant}`
- **Кнопка 1:** `✓ Добавить` → действие: *HTTP POST* (см. ниже), `lv_category` пустой (категорию подберёт сервер).
- **Кнопка 2:** `Категория…` → действие: `Options Dialog`/`Choice Dialog` со списком
  (`Продукты, Кафе, Транспорт, Здоровье, Дом, Развлечения, Прочее`) → выбранное в `lv_category` → затем тот же *HTTP POST*.
- **Кнопка 3:** `Пропустить` → `Cancel Notification` (ничего не шлём).

**HTTP POST (действие `HTTP Request`):**
- Method: **POST**; URL: ваш `…/exec`; **Follow redirects: ON**.
- Тип: **параметры key/value** (form-urlencoded — MacroDroid сам кодирует, безопасно для запятых/кавычек банка):

  | key | value |
  |---|---|
  | `token` | `ВАШ_ТОКЕН` |
  | `type` | `{lv_type}` |
  | `amount` | `{lv_amount}` |
  | `merchant` | `{lv_merchant}` |
  | `category` | `{lv_category}` |
  | `currency` | `{lv_currency}` |
  | `raw` | `{not_text_big}` |

- После ответа: `Cancel Notification` + (опц.) `Toast`/вибро для подтверждения.

### B2. Автодобавление без подтверждения (доверенные продавцы)
Добавьте в начало макроса ветку:
- `IF` Regular expression над `{lv_merchant}`: `(?i)(пятёроч|магнит|перекрест|вкусвилл)` **И** `lv_type = расход`
  → сразу *HTTP POST* (категорию подберёт сервер) → тихий `Toast` «➕ {lv_amount}» → `Stop Macro`
  (т.е. без уведомления-подтверждения).
- `Else` → обычный путь B1 (уведомление с кнопками).

---

## Часть C — надёжность на Samsung One UI 8.5

Агрессивный Doze/battery One UI — частая причина, по которой автоматизаторы «молча умирают».
Выполните **всё**:

1. **Настройки → Приложения → MacroDroid → Батарея → Без ограничений (Unrestricted).**
2. **Настройки → Батарея → Ограничения фоновой активности →** убедитесь, что MacroDroid **нет** в
   *«Спящие»/«Глубоко спящие»*. Добавьте его в **«Приложения, которые не переводятся в спящий режим»**.
3. **Настройки → Батарея → «Переводить неиспользуемые приложения в спящий режим» → ВЫКЛ**
   (иначе Samsung вернёт MacroDroid в сон через несколько дней).
4. **Доступ к уведомлениям** для MacroDroid — оставить включённым; **перепроверять после обновлений One UI**
   (обновление иногда сбрасывает спец-доступы).
5. В самом MacroDroid не отключайте его постоянное уведомление/foreground-сервис — на нём держится
   фоновая работа и автозапуск.
6. **Автозапуск после ребута:** MacroDroid стартует сам (слушает BOOT_COMPLETED). После перезагрузки
   проверьте, что иконка-сервис активна; при необходимости запустите MacroDroid вручную один раз.

**Если пуши перестали ловиться:**
- Выключите и снова включите **Доступ к уведомлениям** для MacroDroid (пере-привязывает listener — классический фикс), либо перезагрузите телефон.
- Проверьте, что Сбер шлёт именно **пуш** (нужен интернет; офлайн → банк присылает СМС, и тогда нужен отдельный SMS-триггер).
- One UI 8/Android 16 умеет **группировать** уведомления и делать AI-сводки — триггер может сработать на «сводку».
  Поэтому в B1 стоит фильтр «есть сумма+валюта»: сводки без суммы отсекаются.

---

## Edge cases

- **Скупой пуш** `Списание 1234 ₽` без продавца → `lv_merchant` пустой → сервер ставит «Без категории».
  Жмёте `Категория…` и выбираете тапом. Сумма и тип всё равно распарсятся.
- **Перевод vs покупка vs зачисление** → тип определяется по ключевым словам (Действие 4). Знак суммы
  проставляет сервер: доход `+`, расход `−`.
- **Дубли (1 списание = 2 пуша: холд + фактическое)** → серверный дедуп по `тип|сумма|продавец`
  в окне `DEDUP_MIN` (3 мин). Две **разные** одинаковые покупки в пределах 3 мин (редко) тоже схлопнутся —
  при необходимости уменьшите окно или добавьте время в ключ.
- **Валюта ≠ ₽** → `lv_currency` ловит символ; сервер пишет как есть, без конвертации (пометьте такие строки в таблице правилом).
- **Реклама/служебные пуши** → отсекаются фильтром «есть сумма+валюта» (Действие 1).

---

## Приватность (это банковские данные — важно)

- `NotificationListenerService` (на нём работает MacroDroid) технически **видит уведомления всех приложений**.
  Минимизация: **триггер строго по пакету `ru.sberbankmobile`**, нигде не логировать полный текст,
  удалить DUMP-макрос после настройки.
- Единственный исходящий канал — ваш **HTTPS POST на собственный Apps Script** (ваш Google-аккаунт).
  Никаких сторонних облаков в основном варианте.
- Эндпоинт открыт как `Anyone` (иначе MacroDroid не достучится) → защищён **секретным токеном**;
  держите токен и `SHEET_ID` в секрете, не коммитьте их.
- MacroDroid умеет облачную синхронизацию макросов — **отключите** её, чтобы токен/правила не утекали в их облако.
- Вариант 2 (через бота/Claude API) расширяет границу доверия на Railway + Anthropic — выбирайте осознанно.

---

## Тест-чек-лист

1. ☐ В браузере открыть `…/exec` → `{"ok":true,"ping":"alive"}`.
2. ☐ `curl` из A4 → ответ `ok:true` и строка в листе `Траты` с правильным знаком и категорией.
3. ☐ DUMP-макрос: реальная операция на 1 ₽ → прочитан точный текст пуша.
4. ☐ Регулярки B2–B5 проверены на этом тексте (в MacroDroid → тест magic-text/переменных).
5. ☐ Реальная мелкая покупка → появилось своё уведомление с верными суммой/продавцом.
6. ☐ Тап `✓ Добавить` → строка в таблице, верный знак, категория подобрана.
7. ☐ Тап `Категория…` → выбор тапом → строка с выбранной категорией.
8. ☐ Перезагрузка телефона → повторить п.5–6, всё работает.
9. ☐ Симуляция дубля (повторный POST тем же `тип|сумма|продавец` в пределах 3 мин) → строка одна.
10. ☐ Доверенный продавец (Пятёрочка) → строка добавляется без уведомления (если включили B2-автодобавление).

---

## Источники

- [Trigger: Notification — MacroDroid Wiki](https://macrodroidforum.com/wiki/index.php/Trigger:_Notification)
- [Magic text — MacroDroid Wiki](https://macrodroidforum.com/wiki/index.php/Magic_text)
- [Action: Display Notification — MacroDroid Wiki](https://macrodroidforum.com/wiki/index.php/Action:_Display_Notification)
- [How to listen to notifications in MacroDroid — nesin.io](https://nesin.io/blog/macrodroid-notification-listener)
- [Web Apps — Google Apps Script (doPost, деплой, доступ)](https://developers.google.com/apps-script/guides/web)
- [Вопросы и ответы об Уведомлениях по карте — Сбербанк](https://www.sberbank.ru/ru/person/help/dist_services_faqsms)
- [Push-уведомления от «Сбера» в дружелюбной тональности (2025) — CNews](https://www.cnews.ru/news/line/2025-01-14_teper_push-uvedomleniya_ot)
- [СберБанк Онлайн `ru.sberbankmobile` — RuStore](https://www.rustore.ru/catalog/app/ru.sberbankmobile)
- [Galaxy Z Fold 5 получил One UI 8.5 (Android 16) — AndroidHeadlines, май 2026](https://www.androidheadlines.com/2026/05/galaxy-z-fold-5-flip-5-stable-one-ui-8-5-update.html)
- [Samsung — Don't kill my app! (battery optimization)](https://dontkillmyapp.com/samsung)
- [NotificationListenerService — Android Developers](https://developer.android.com/reference/android/service/notification/NotificationListenerService)
- [Android 16 notification controls — AndroidPolice](https://www.androidpolice.com/android-16-takes-a-hard-stance-against-notification-overload/)
</content>
</invoke>
