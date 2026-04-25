#!/usr/bin/env python3
"""
Скачивает / обновляет ветку астрономического форума astronomy.ru.
Используется внутри GitHub Actions.

Использование:
    python3 parse_thread.py <URL> [<NAME>]

Поведение:
- Если TOPIC_ID есть в threads/_index.json -> ОБНОВЛЕНИЕ существующей.
  NAME игнорируется (используется уже сохранённое имя).
- Если TOPIC_ID нет в индексе -> НОВАЯ ВЕТКА. NAME обязателен.

Результат:
- threads/<NAME>.txt — текстовый файл со всеми постами
- threads/_index.json — реестр всех скачанных веток
"""

import sys
import os
import re
import json
import time
import urllib.request
from datetime import datetime
from bs4 import BeautifulSoup

# === НАСТРОЙКИ ===
SLEEP_BETWEEN_REQUESTS = 10  # секунд между запросами
USER_AGENT = "Mozilla/5.0 (Linux; Android 13)"
TIMEOUT = 30
THREADS_DIR = "threads"
INDEX_FILE = os.path.join(THREADS_DIR, "_index.json")
# =================


# ---------- УТИЛИТЫ ----------

def extract_topic_id(url):
    """Извлекает TOPIC_ID из URL форума в любом формате."""
    match = re.search(r'topic[,=](\d+)', url.strip())
    if not match:
        raise ValueError(f"Не могу найти TOPIC_ID в URL: {url}")
    return match.group(1)


def fetch_page(url):
    """Скачивает HTML страницы. Возвращает None при ошибке."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"    ОШИБКА скачивания {url}: {e}", flush=True)
        return None


def find_max_offset(topic_id, first_page_html):
    """Находит максимальный offset из навигации первой страницы."""
    pattern = rf'topic,{topic_id}\.(\d+)\.html'
    offsets = [int(m) for m in re.findall(pattern, first_page_html)]
    if not offsets:
        return 0  # ветка из одной страницы
    return max(offsets)


def sanitize_name(name):
    """Превращает имя ветки в безопасное имя файла."""
    name = name.strip().lower()
    name = re.sub(r'[^a-zа-я0-9_-]+', '_', name)
    name = name.strip('_')
    if not name:
        raise ValueError("Имя ветки пустое после очистки")
    return name


def load_index():
    if not os.path.exists(INDEX_FILE):
        return {}
    with open(INDEX_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_index(idx):
    os.makedirs(THREADS_DIR, exist_ok=True)
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


# ---------- ПАРСИНГ ПОСТА ----------

def clean_text(s):
    if not s:
        return ''
    s = s.replace('\xa0', ' ')
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]+', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def extract_post_body(post_div):
    if not post_div:
        return ''
    body = BeautifulSoup(str(post_div), 'lxml')
    post = body.find()

    for qh in post.select('div.quoteheader'):
        author = qh.get_text(strip=True)
        qh.replace_with(f'\n[ЦИТАТА {author}]\n')
    for bq in post.select('blockquote.bbc_standard_quote, blockquote'):
        preview = bq.get_text(strip=True)[:80]
        bq.replace_with(f'> {preview}...\n[/конец цитаты]\n')
    for qf in post.select('div.quotefooter'):
        qf.decompose()

    for img in post.find_all('img'):
        src = img.get('src', '')
        if 'Smiley' in src or 'smile' in src.lower():
            img.decompose()
        else:
            img.replace_with(f'[изображение: {src}]')

    for a in post.find_all('a'):
        href = a.get('href', '')
        text = a.get_text(strip=True)
        if href and text and href != text:
            a.replace_with(f'{text} [→ {href}]')

    text = post.get_text(separator='\n')
    return clean_text(text)


def extract_post(wrapper):
    result = {'msg_id': None, 'author': '', 'date': '', 'subject': '', 'body': ''}

    inner = wrapper.find('div', id=re.compile(r'^msg_\d+$'))
    if inner:
        m = re.search(r'msg_(\d+)', inner.get('id', ''))
        if m:
            result['msg_id'] = m.group(1)

    h4 = wrapper.find('h4')
    if h4:
        result['author'] = clean_text(h4.get_text())

    h5 = wrapper.find('h5')
    if h5:
        result['subject'] = clean_text(h5.get_text())

    smalls = wrapper.find_all(class_='smalltext')
    for s in smalls:
        text = s.get_text(strip=True)
        if re.search(r'\d{1,2}.*\d{2}:\d{2}', text):
            date = text.replace('«', '').replace('»', '').strip()
            date = re.sub(r'^[:\s]*Ответ\s*#?\d*\s*:?\s*', '', date)
            date = re.sub(r'^[:\s]+', '', date).strip()
            result['date'] = date
            break

    body_div = wrapper.find('div', class_='post')
    if body_div:
        result['body'] = extract_post_body(body_div)

    return result


def parse_page(html):
    """Возвращает (список постов, заголовок страницы)."""
    soup = BeautifulSoup(html, 'lxml')
    wrappers = soup.select('div.post_wrapper')
    posts = []
    for w in wrappers:
        try:
            p = extract_post(w)
            if p['body'] or p['author']:
                posts.append(p)
        except Exception as e:
            posts.append({
                'msg_id': '?', 'author': '?', 'date': '?', 'subject': '?',
                'body': f'[ОШИБКА ПАРСИНГА: {e}]',
            })
    title_tag = soup.find('title')
    page_title = clean_text(title_tag.get_text()) if title_tag else ''
    return posts, page_title


# ---------- ФОРМАТИРОВАНИЕ ВЫВОДА ----------

def format_post_block(post, post_num):
    """Форматирует один пост для записи в текстовый файл."""
    out = []
    out.append(f'[Пост #{post_num}')
    if post['msg_id']:
        out.append(f' msg_id={post["msg_id"]}')
    out.append(']\n')
    if post['author']:
        out.append(f'Автор: {post["author"]}\n')
    if post['date']:
        out.append(f'Дата: {post["date"]}\n')
    if post['subject']:
        out.append(f'Тема: {post["subject"]}\n')
    out.append('\n')
    out.append(post['body'])
    out.append('\n\n' + '-' * 50 + '\n')
    return ''.join(out)


def format_page_marker(page_num, page_label):
    return f'\n--- Страница {page_num} ({page_label}) ---\n\n'


def format_update_marker(date_str, posts_added):
    return (
        f'\n\n{"=" * 70}\n'
        f'=== ОБНОВЛЕНИЕ {date_str}: добавлено {posts_added} постов ===\n'
        f'{"=" * 70}\n\n'
    )


def format_header(title, url, total_posts, name):
    return (
        f'ТЕМА: {title}\n'
        f'НАЗВАНИЕ: {name}\n'
        f'URL: {url}\n'
        f'Всего постов: {total_posts}\n'
        f'{"=" * 70}\n\n'
    )


# ---------- ОСНОВНОЙ ПРОЦЕСС ----------

def download_pages(topic_id, start_offset, max_offset):
    """Качает страницы от start_offset до max_offset включительно.
    Возвращает список (page_num, page_label, posts_list)."""
    base_url = f"https://astronomy.ru/forum/index.php/topic,{topic_id}"
    pages_data = []
    offsets = list(range(start_offset, max_offset + 1, 20))
    total = len(offsets)

    for i, offset in enumerate(offsets, start=1):
        if i > 1:
            time.sleep(SLEEP_BETWEEN_REQUESTS)
        page_num = (offset // 20) + 1
        page_label = f"page_{page_num:04d}.html"
        url = f"{base_url}.{offset}.html"
        html = fetch_page(url)
        if html is None:
            print(f"  [{i}/{total}] offset={offset}: ПРОПУЩЕНА (ошибка)", flush=True)
            pages_data.append((page_num, page_label, []))
            continue
        posts, _ = parse_page(html)
        pages_data.append((page_num, page_label, posts))
        print(f"  [{i}/{total}] offset={offset}: {len(posts)} постов", flush=True)

    return pages_data


def existing_msg_ids(content):
    """Извлекает все msg_id из текста файла."""
    return set(re.findall(r'msg_id=(\d+)', content))


def truncate_at_page(content, page_num):
    """Обрезает файл до маркера указанной страницы.
    Возвращает обрезанный кусок (всё что выше маркера)."""
    marker = f'--- Страница {page_num} '
    pos = content.find(marker)
    if pos == -1:
        return content  # маркер не найден — возвращаем как есть
    # Откатываемся к началу строки (там \n перед '---')
    line_start = content.rfind('\n', 0, pos)
    if line_start > 0:
        return content[:line_start + 1]
    return content[:pos]


def count_posts_in_text(content):
    """Считает посты по маркерам [Пост #N]."""
    return len(re.findall(r'\[Пост #\d+', content))


def write_pages_to_file(file_handle, pages_data, start_post_num, skip_msg_ids=None):
    """Записывает страницы в открытый файл, пропуская дубликаты по msg_id.
    Возвращает (последний номер поста, число записанных постов)."""
    skip = skip_msg_ids or set()
    post_num = start_post_num
    written = 0

    for page_num, page_label, posts in pages_data:
        # Фильтруем дубликаты
        unique_posts = [p for p in posts if not p['msg_id'] or p['msg_id'] not in skip]
        if not unique_posts:
            continue
        file_handle.write(format_page_marker(page_num, page_label))
        for p in unique_posts:
            post_num += 1
            file_handle.write(format_post_block(p, post_num))
            if p['msg_id']:
                skip.add(p['msg_id'])
            written += 1

    return post_num, written


def create_new_thread(url, name, topic_id, idx):
    """Скачивает ветку с нуля."""
    safe_name = sanitize_name(name)
    output_path = os.path.join(THREADS_DIR, f"{safe_name}.txt")

    # Проверка: имя файла не должно быть занято другой веткой
    for tid, info in idx.items():
        if info.get('name') == safe_name and tid != topic_id:
            raise ValueError(
                f"Имя '{safe_name}' уже занято веткой TOPIC_ID={tid}. Выбери другое."
            )

    base_url = f"https://astronomy.ru/forum/index.php/topic,{topic_id}"
    first_url = f"{base_url}.0.html"
    print(f"Скачиваю первую страницу: {first_url}", flush=True)
    first_html = fetch_page(first_url)
    if not first_html:
        raise RuntimeError("Не удалось скачать первую страницу")

    max_offset = find_max_offset(topic_id, first_html)
    total_pages = (max_offset // 20) + 1
    estimated_min = total_pages * (SLEEP_BETWEEN_REQUESTS + 1) // 60
    print(f"MAX_OFFSET={max_offset}, страниц={total_pages}, ~{estimated_min} мин", flush=True)

    # Парсим первую страницу
    posts0, page_title = parse_page(first_html)
    pages_data = [(1, "page_0001.html", posts0)]
    print(f"  [1/{total_pages}] offset=0: {len(posts0)} постов", flush=True)

    # Качаем остальные
    if max_offset > 0:
        rest = download_pages(topic_id, 20, max_offset)
        pages_data.extend(rest)

    total_posts = sum(len(p[2]) for p in pages_data)

    # Запись
    os.makedirs(THREADS_DIR, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as out:
        out.write(format_header(page_title, url, total_posts, safe_name))
        post_num, _ = write_pages_to_file(out, pages_data, start_post_num=0)

    # Собираю все msg_ids для индекса
    all_msg_ids = set()
    for _, _, posts in pages_data:
        for p in posts:
            if p['msg_id']:
                all_msg_ids.add(p['msg_id'])

    # Обновляю индекс
    idx[topic_id] = {
        'name': safe_name,
        'url': url,
        'title': page_title,
        'last_offset': max_offset,
        'total_posts': total_posts,
        'updated_at': datetime.utcnow().isoformat() + 'Z',
        'created_at': datetime.utcnow().isoformat() + 'Z',
    }
    save_index(idx)

    size_kb = os.path.getsize(output_path) // 1024
    print(f"\nГотово (новая). Постов: {total_posts}. Файл: {output_path} ({size_kb} КБ)", flush=True)


def update_thread(url, topic_id, idx):
    """Дозагружает новые посты в существующую ветку."""
    info = idx[topic_id]
    safe_name = info['name']
    output_path = os.path.join(THREADS_DIR, f"{safe_name}.txt")
    old_last_offset = info['last_offset']

    if not os.path.exists(output_path):
        raise RuntimeError(
            f"В индексе TOPIC_ID={topic_id}, но файл {output_path} отсутствует. "
            f"Удали запись из _index.json и запусти заново как новую ветку."
        )

    print(f"Обновление ветки '{safe_name}' (TOPIC_ID={topic_id})", flush=True)
    print(f"Старый last_offset={old_last_offset}", flush=True)

    # 1. Скачиваем первую страницу для определения нового max_offset
    base_url = f"https://astronomy.ru/forum/index.php/topic,{topic_id}"
    first_url = f"{base_url}.0.html"
    print(f"Проверяю размер ветки: {first_url}", flush=True)
    first_html = fetch_page(first_url)
    if not first_html:
        raise RuntimeError("Не удалось скачать первую страницу")
    new_max_offset = find_max_offset(topic_id, first_html)
    print(f"Новый max_offset={new_max_offset}", flush=True)

    # 2. Определяем стартовый offset для перекачки
    # Перекачиваем последнюю запомненную страницу + всё после неё
    # (страховка от того что на ней был неполный контент)
    start_offset = max(0, old_last_offset - 20)
    if start_offset == 0 and old_last_offset == 0:
        # ветка из одной страницы — перекачаем её всю
        start_offset = 0
    print(f"Перекачиваю с offset={start_offset} до {new_max_offset}", flush=True)

    # 3. Качаем нужные страницы
    pages_data = download_pages(topic_id, start_offset, new_max_offset)

    # 4. Читаем старый файл, обрезаем до начала перекачиваемой области
    with open(output_path, 'r', encoding='utf-8') as f:
        old_content = f.read()

    cut_page_num = (start_offset // 20) + 1
    truncated_content = truncate_at_page(old_content, cut_page_num)

    # 5. Собираем msg_ids которые остались в обрезанной части — для дедупа
    kept_msg_ids = existing_msg_ids(truncated_content)

    # 6. Считаем сколько постов уже в обрезанной части
    posts_in_kept = count_posts_in_text(truncated_content)

    # 7. Открываем файл на запись, пишем обрезанную часть + новые страницы
    with open(output_path, 'w', encoding='utf-8') as out:
        out.write(truncated_content)
        # Маркер обновления
        date_str = datetime.utcnow().strftime('%d.%m.%Y')
        # Считаем сколько новых постов будет добавлено
        all_new_msg_ids = set()
        new_posts_count = 0
        for _, _, posts in pages_data:
            for p in posts:
                if p['msg_id'] and p['msg_id'] not in kept_msg_ids:
                    all_new_msg_ids.add(p['msg_id'])
                    new_posts_count += 1
        # Сравниваем с предыдущим total_posts чтобы понять реальный прирост
        prev_total = info.get('total_posts', 0)
        actual_new = (posts_in_kept + new_posts_count) - prev_total
        if actual_new < 0:
            actual_new = 0

        if actual_new > 0:
            out.write(format_update_marker(date_str, actual_new))

        post_num, _ = write_pages_to_file(
            out, pages_data, start_post_num=posts_in_kept,
            skip_msg_ids=kept_msg_ids
        )

    # 8. Обновляем индекс
    new_total = post_num
    info['last_offset'] = new_max_offset
    info['total_posts'] = new_total
    info['url'] = url  # на случай если URL немного отличается
    info['updated_at'] = datetime.utcnow().isoformat() + 'Z'
    save_index(idx)

    diff = new_total - prev_total
    size_kb = os.path.getsize(output_path) // 1024
    print(f"\nГотово (обновление). Было {prev_total}, стало {new_total} постов "
          f"(+{diff}). Файл: {output_path} ({size_kb} КБ)", flush=True)


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 parse_thread.py <URL> [<NAME>]", file=sys.stderr)
        return 1

    url = sys.argv[1].strip()
    name_arg = sys.argv[2].strip() if len(sys.argv) >= 3 else ''

    try:
        topic_id = extract_topic_id(url)
    except ValueError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1

    print(f"TOPIC_ID = {topic_id}", flush=True)

    idx = load_index()

    if topic_id in idx:
        # Существующая ветка → обновление
        existing_name = idx[topic_id].get('name')
        if name_arg and sanitize_name(name_arg) != existing_name:
            print(
                f"⚠ Ветка TOPIC_ID={topic_id} уже есть под именем '{existing_name}'. "
                f"Введённое имя '{name_arg}' игнорируется. Если хочешь переименовать — "
                f"отредактируй _index.json вручную и переименуй файл.",
                flush=True
            )
        try:
            update_thread(url, topic_id, idx)
        except Exception as e:
            print(f"ОШИБКА обновления: {e}", file=sys.stderr)
            return 1
    else:
        # Новая ветка
        if not name_arg:
            print(
                f"ОШИБКА: ветка TOPIC_ID={topic_id} новая, но имя не задано. "
                f"Запусти workflow снова, заполнив поле 'Имя ветки'.",
                file=sys.stderr
            )
            return 1
        try:
            create_new_thread(url, name_arg, topic_id, idx)
        except Exception as e:
            print(f"ОШИБКА создания: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
