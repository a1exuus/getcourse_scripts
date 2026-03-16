# extractor.py
"""
GetCourse -> Методист extractor (переписанный).
Файлы:
 - modules.txt  : список модулей (id или полный URL), по одному в строке
 - state.json   : playwright storage state (создаётся после ручного логина)
 - output/      : результат (структура описана ниже)

Запуск:
  python extractor.py

Поведение:
 - Если нет state.json — откроется браузер и попросит залогиниться вручную.
   После Enter состояние сохранится в state.json.
 - После этого скрипт будет использовать state.json для повторных запусков.
"""

import os
import re
import time
import json
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as mdify
from playwright.sync_api import sync_playwright

BASE = "https://buro20.ru"
OUTPUT_DIR = "output"
MODULES_FILE = "modules.txt"
STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0"}
MAX_STEP_NAV = 1000  # safety cap to avoid infinite loops

os.makedirs(OUTPUT_DIR, exist_ok=True)


def normalize_module_url(line: str) -> str:
    """Принимает id или полный url; возвращает корректный module url (stream/view)"""
    s = line.strip()
    if not s:
        return None
    if s.startswith("http"):
        return s
    # если просто число — считаем это id модуля -> stream/view
    if re.fullmatch(r"\d+", s):
        return f"{BASE}/teach/control/stream/view/id/{s}"
    # возможно передали путь вида /teach/control/stream/view/id/933...
    if s.startswith("/"):
        return urljoin(BASE, s)
    # иначе пробуем собрать как kebab-case? но пока — считаем название и не трансформируем
    return s


def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def save_binary(url, dest_path):
    """Скачать двоичный файл и добавить расширение по Content-Type если нужно.
       Возвращает реальный путь сохранённого файла."""
    # поправим относительные src
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = BASE + url

    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    content_type = (r.headers.get("Content-Type") or "").lower()
    ext = ""
    if "png" in content_type:
        ext = ".png"
    elif "jpeg" in content_type or "jpg" in content_type:
        ext = ".jpg"
    elif "webp" in content_type:
        ext = ".webp"
    elif "gif" in content_type:
        ext = ".gif"
    elif "svg" in content_type:
        ext = ".svg"

    # если dest_path уже имеет расширение — не трогаем
    if ext and not os.path.splitext(dest_path)[1]:
        dest_path = dest_path + ext

    with open(dest_path, "wb") as f:
        f.write(r.content)

    return dest_path


def block_to_md(block):
    """Преобразует один lite-block в markdown-строки. Возвращает MD-текст."""
    main_class = block.get("data-main-class") or ""
    cls = " ".join(block.get("class", []))
    text_md = ""

    # header-like blocks (заголовки секций)
    if "header" in main_class or "lessonHdr01" in cls or "lt-lesson-header" in cls:
        b = block.select_one("p > b") or block.select_one("p > strong")
        if b:
            title = b.get_text(strip=True)
            text_md += f"\n# {title}\n\n"
        else:
            txt = block.get_text("\n", strip=True)
            if txt:
                text_md += f"\n# {txt}\n\n"
        return text_md

    # text blocks
    if "text" in main_class or "lt-lesson-text" in cls or "lessonTxt01" in cls:
        content_el = block.select_one("[data-param='text']") or block
        # checkbox handling
        for inp in content_el.select("input[type='checkbox']"):
            parent = inp.parent
            text_after = parent.get_text("", strip=True)
            parent.string = "- [ ] " + text_after.replace("\u00a0", " ").strip()
            inp.decompose()

        html = str(content_el)
        md = mdify(html, heading_style="ATX")
        text_md += md + "\n\n"
        return text_md

    # image blocks
    if "image" in main_class or "lessonImg01" in cls or "lt-lesson-image" in cls:
        img = block.select_one("img")
        if img and img.get("src"):
            src = img.get("src")
            text_md += f"![image]({src})\n\n"
        return text_md

    # fallback
    fallback = block.get_text("\n", strip=True)
    if fallback:
        return fallback + "\n\n"
    return ""


def parse_page_and_save(html, out_dir, step_id):
    """Парсит страницу шага (pl-страница) и сохраняет md + картинки.
       Возвращает (lesson_title, md_path, [image_paths])"""
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".lesson-title-value") or soup.select_one("h2.lesson-title-value")
    lesson_title = title_el.get_text(strip=True) if title_el else None

    blocks = soup.select(".lite-block-live-wrapper")
    md_parts = []
    images = []

    for block in blocks:
        md = block_to_md(block)
        imgs = block.select("img")
        for img in imgs:
            src = img.get("src")
            if src:
                # сохраняем картинки в папку images/
                fname = f"step_{step_id}_" + os.path.basename(src.split("?")[0])
                images.append((src, os.path.join(out_dir, "images", fname)))
                # заменяем ссылку в md на локальную
                md = md.replace(src, os.path.join("images", fname))
        if md.strip():
            md_parts.append(md)

    full_md = "\n".join(md_parts).strip()

    ensure_dir(out_dir)
    ensure_dir(os.path.join(out_dir, "images"))

    md_path = os.path.join(out_dir, f"step_{step_id}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(full_md)

    saved_image_paths = []
    for src, path in images:
        try:
            real_path = save_binary(src, path)
            saved_image_paths.append(real_path)
        except Exception as e:
            print(f"[WARN] failed to download {src}: {e}")

    return lesson_title, md_path, saved_image_paths


def find_next_step_url(page_html):
    """
    Ищет кнопку 'Следующий урок' на странице шага и возвращает полный URL следующего шага.
    Если кнопки нет — возвращает None.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    
    # Ищем ссылку с текстом "Следующий урок" в блоке навигации
    nav_block = soup.select_one(".lesson-navigation")
    if not nav_block:
        return None
    
    # Ищем ссылку содержащую "Следующий урок"
    next_link = None
    for a in nav_block.select("a[href*='lesson/view']"):
        text = a.get_text(strip=True).lower()
        if "следующий урок" in text or "следующий" in text:
            next_link = a
            break
    
    if not next_link:
        return None
    
    href = next_link.get("href")
    if not href:
        return None
    
    # Нормализуем URL
    full_url = urljoin(BASE, href)
    return full_url


def get_first_step_url_from_lesson_page(html):
    """
    Из страницы урока (список шагов) получает URL первого шага.
    Игнорирует элемент "Описание".
    Возвращает первый доступный шаг или None.
    """
    soup = BeautifulSoup(html, "html.parser")
    
    for li in soup.select(".lesson-list li"):
        a = li.select_one("a[href*='lesson/view']")
        if not a:
            continue
        
        # Пропускаем "Описание"
        title_el = li.select_one(".link.title")
        title_text = title_el.get_text(" ", strip=True).lower() if title_el else ""
        if "описание" in title_text:
            continue
        
        href = a.get("href")
        if href:
            return urljoin(BASE, href)
    
    return None


def extract_lesson_links_from_page(html):
    """Возвращает список уникальных href полных (teach/control/lesson/view/id/NNN) в порядке появления."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.select("a[href*='/teach/control/lesson/view/id/']"):
        href = a.get("href")
        full = urljoin(BASE, href)
        if full not in found:
            found.append(full)
    # также попробуем искать ссылки вида /pl/... lesson/view?id=NNN
    for a in soup.select("a[href*='lesson/view?id=']"):
        href = a.get("href")
        full = urljoin(BASE, href)
        if full not in found:
            found.append(full)
    return found


def extract_stream_links_from_page(html):
    """Иногда модуль содержит ссылки на stream/view (под-модули). Вернёт их для обхода."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.select("a[href*='/teach/control/stream/view/id/']"):
        href = a.get("href")
        full = urljoin(BASE, href)
        if full not in found:
            found.append(full)
    return found


def extract_step_links_from_lesson_page(html, current_lesson_url=None):
    """Из страницы урока вытаскивает ссылки на шаги, полностью игнорируя "Описание"."""
    soup = BeautifulSoup(html, "html.parser")
    found = []

    for li in soup.select(".lesson-list li"):
        a = li.select_one("a[href*='lesson/view']")
        if not a:
            continue

        title_el = li.select_one(".link.title")
        title_text = title_el.get_text(" ", strip=True).lower() if title_el else ""
        if "описание" in title_text:
            continue

        href = a.get("href")
        if not href:
            continue

        full = urljoin(BASE, href)
        if full == current_lesson_url:
            continue
        if full not in found:
            found.append(full)

    return found


def lesson_id_from_url(u):
    m = re.search(r"(?:lesson/view/id/|lesson/view\?id=)(\d+)", u)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)$", u)
    return None


def pl_lesson_url_from_lesson_url(lesson_url):
    """Преобразует /teach/control/lesson/view/id/NNN -> /pl/teach/control/lesson/view?id=NNN"""
    lid = lesson_id_from_url(lesson_url)
    if lid:
        return f"{BASE}/pl/teach/control/lesson/view?id={lid}"
    return lesson_url


def process_lesson_with_navigation(page, lesson_url, lesson_dir):
    """
    Обрабатывает урок с навигацией по шагам через кнопку 'Следующий урок'.
    
    Логика:
    1. Заходим на страницу урока (список шагов)
    2. Берём первый шаг (игнорируя 'Описание')
    3. Парсим шаг, сохраняем данные
    4. Ищем кнопку 'Следующий урок' на странице шага
    5. Если есть — переходим по ней, повторяем с п.3
    6. Если нет — завершаем обработку урока
    
    Возвращает количество сохранённых шагов.
    """
    print(f"  -> Открываю урок: {lesson_url}")
    
    # Заходим на страницу урока (список шагов)
    try:
        page.goto(lesson_url, timeout=20000)
        time.sleep(1)
    except Exception as e:
        print(f"   ! Ошибка загрузки урока: {e}")
        return 0
    
    lesson_html = page.content()
    
    # Получаем URL первого шага
    first_step_url = get_first_step_url_from_lesson_page(lesson_html)
    if not first_step_url:
        print("   ! Шаги не найдены (описание игнорируется), пропускаю урок.")
        return 0
    
    # Собираем все шаги через навигацию
    step_urls = [first_step_url]
    visited_steps = {first_step_url}
    
    # Навигация по шагам через кнопку "Следующий урок"
    max_steps = MAX_STEP_NAV
    current_step_url = first_step_url
    
    for i in range(max_steps):
        # Переходим на текущий шаг (pl-версия)
        pl_url = pl_lesson_url_from_lesson_url(current_step_url)
        print(f"   -> Шаг {i+1}: {pl_url}")
        
        try:
            page.goto(pl_url, timeout=30000)
            time.sleep(0.5)
        except Exception as e:
            print(f"     ! Ошибка загрузки шага: {e}")
            break
        
        step_html = page.content()
        
        # Проверяем, есть ли контент шага
        soup = BeautifulSoup(step_html, "html.parser")
        if not soup.select_one(".lite-block-live-wrapper"):
            print(f"     ! Не найден контент шага на странице {pl_url}.")
            break
        
        # Сохраняем шаг
        step_id = lesson_id_from_url(pl_url)
        if step_id:
            ensure_dir(lesson_dir)
            ensure_dir(os.path.join(lesson_dir, "images"))
            try:
                title, md_path, images = parse_page_and_save(step_html, lesson_dir, step_id)
                print(f"     saved step {step_id} (title: {title}, images: {len(images)})")
            except Exception as e:
                print(f"     ! Ошибка при сохранении шага {step_id}: {e}")
        
        # Ищем кнопку "Следующий урок"
        next_step_url = find_next_step_url(step_html)
        
        if not next_step_url:
            print("   Кнопка 'Следующий урок' не найдена — завершаем навигацию по шагам.")
            break
        
        # Проверяем, не зациклились ли мы
        if next_step_url in visited_steps:
            print("   Обнаружен цикл навигации — завершаем.")
            break
        
        visited_steps.add(next_step_url)
        step_urls.append(next_step_url)
        current_step_url = next_step_url
    
    return len(step_urls)


def main():
    if not os.path.exists(MODULES_FILE):
        print(f"Создай {MODULES_FILE} с URL/ID модулей (по одному в строке).")
        return

    modules = []
    with open(MODULES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            url = normalize_module_url(s)
            if url:
                modules.append(url)

    if not modules:
        print("Нет модулей в файле.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = None

        # если есть state.json — используем, иначе создаём новый и попросим логин
        if os.path.exists(STATE_FILE):
            print("Использую существующий state:", STATE_FILE)
            context = browser.new_context(storage_state=STATE_FILE)
        else:
            context = browser.new_context()

        page = context.new_page()

        # если нет state -> попросим залогиниться на первой странице (первый модуль)
        if not os.path.exists(STATE_FILE):
            first_module = modules[0]
            print("Открывается браузер для авторизации... Откроется страница первого модуля:", first_module)
            page.goto(first_module)
            input("Авторизуйся в buro20.ru в открывшемся окне, затем вернись в терминал и нажми Enter...")
            # сохраним состояние
            context.storage_state(path=STATE_FILE)
            print("Сохранено состояние в", STATE_FILE)

        # Обработка модулей
        for module_url in modules:
            print("Обрабатываем модуль:", module_url)
            try:
                page.goto(module_url, timeout=30000)
            except Exception as e:
                print("  ! Не смог открыть модуль:", e)
                continue

            time.sleep(1)
            html = page.content()

            # сначала пробуем прямо взять ссылки на уроки
            lesson_links = extract_lesson_links_from_page(html)

            # если прямо нет — попробуем найти вложенные stream (под-модули) и из них взять уроки
            if not lesson_links:
                stream_links = extract_stream_links_from_page(html)
                for s in stream_links:
                    try:
                        page.goto(s, timeout=20000)
                    except:
                        continue
                    time.sleep(0.8)
                    sub_html = page.content()
                    found = extract_lesson_links_from_page(sub_html)
                    for fh in found:
                        if fh not in lesson_links:
                            lesson_links.append(fh)

            if not lesson_links:
                print("  ! Не найдено уроков в модуле — пропускаю.")
                continue

            # нормализуем название модуля для папки (по пути)
            parsed = urlparse(module_url)
            module_slug = os.path.basename(parsed.path) or re.sub(r'\W+', '_', parsed.path)
            module_dir = os.path.join(OUTPUT_DIR, f"module_{module_slug}")
            ensure_dir(module_dir)

            # обрабатываем каждый урок
            for lesson_url in lesson_links:
                lid = lesson_id_from_url(lesson_url) or "unknown"
                
                # создаём подпапку для этого урока внутри модуля
                lesson_lid = lesson_id_from_url(lesson_url) or "unknown"
                lesson_dir = os.path.join(module_dir, f"lesson_{lesson_lid}")
                ensure_dir(lesson_dir)
                ensure_dir(os.path.join(lesson_dir, "images"))
                
                # Используем новую функцию с навигацией по кнопке "Следующий урок"
                total_saved = process_lesson_with_navigation(page, lesson_url, lesson_dir)

                if total_saved == 0:
                    print("   ! Шаги найдены, но не удалось сохранить контент.")
                else:
                    print(f"   Урок обработан: saved steps = {total_saved}")

        # финальное сохранение state (на всякий)
        try:
            context.storage_state(path=STATE_FILE)
        except Exception:
            pass

        browser.close()
    print("Готово.")


if __name__ == "__main__":
    main()