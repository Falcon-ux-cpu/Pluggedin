import os
import re
import json
import shutil
import smtplib
import tempfile
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

# --- Настройки ---
BASE_URL = "https://pluggedin.ru"
FEED_URL = "https://pluggedin.ru/news"  # Страница со списком свежих статей
SENT_FILE = "sent_articles.json"

# Данные авторизации (берем из Secrets GitHub)
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_USER)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def load_sent_articles():
    if os.path.exists(SENT_FILE):
        try:
            with open(SENT_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Ошибка чтения {SENT_FILE}: {e}")
    return set()


def save_sent_articles(sent_set):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_set), f, ensure_ascii=False, indent=2)


def get_latest_article_urls():
    """Собирает ссылки на статьи с главной страницы/раздела /open."""
    try:
        response = requests.get(FEED_URL, headers=HEADERS, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Ошибка получения списка статей: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    urls = []
    
    # Поиск ссылок вида /open/название-статьи-id
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"/open/[^/]+-\d+", href):
            full_url = urljoin(BASE_URL, href)
            if full_url not in urls:
                urls.append(full_url)
                
    return urls


def parse_and_send_article(article_url, sent_set):
    print(f"\n--- Обработка статьи: {article_url} ---")
    try:
        res = requests.get(article_url, headers=HEADERS, timeout=15)
        res.raise_for_status()
    except Exception as e:
        print(f"Не удалось загрузить страницу статьи: {e}")
        return False

    soup = BeautifulSoup(res.text, "html.parser")

    # 1. Извлечение заголовка
    h1_tag = soup.find("h1", {"itemprop": "headline"})
    title = h1_tag.get_text(strip=True) if h1_tag else "Без названия"

    # 2. Извлечение даты
    date_tag = soup.find("span", class_="dateArticle")
    article_date = date_tag.get_text(strip=True) if date_tag else ""

    # 3. Извлечение главного (титульного) изображения
    top_img_tag = soup.find("img", {"itemprop": "image"})
    top_img_url = None
    if top_img_tag and top_img_tag.get("src"):
        top_img_url = urljoin(BASE_URL, top_img_tag["src"])

    # 4. Извлечение тела статьи
    body_div = soup.find("div", class_="open-article-text")
    if not body_div:
        print("Тело статьи (open-article-text) не найдено.")
        return False

    # Создаем временную папку для локальной загрузки изображений
    temp_dir = tempfile.mkdtemp(prefix="pluggedin_imgs_")

    try:
        # Подготовка структуры письма
        msg = MIMEMultipart("related")
        
        # Обязательное условие: тема письма содержит слово Pluggedin
        msg["Subject"] = f"[Pluggedin] {title}"
        msg["From"] = GMAIL_USER
        msg["To"] = RECIPIENT_EMAIL

        img_counter = 0
        inline_images = []

        # Функция для скачивания и добавления вложения CID
        def process_image(img_url, img_tag):
            nonlocal img_counter
            try:
                img_res = requests.get(img_url, headers=HEADERS, timeout=10)
                if img_res.status_code == 200:
                    img_counter += 1
                    ext = img_url.split(".")[-1].split("?")[0].lower()
                    if ext not in ["jpg", "jpeg", "png", "gif", "webp"]:
                        ext = "jpg"

                    filename = f"image_{img_counter}.{ext}"
                    filepath = os.path.join(temp_dir, filename)

                    with open(filepath, "wb") as f:
                        f.write(img_res.content)

                    # CID для связи изображения внутри HTML
                    cid = f"img_{img_counter}@pluggedin"
                    img_tag["src"] = f"cid:{cid}"

                    # Сброс/оптимизация ширины и стилей
                    if img_tag.has_attr("style"):
                        del img_tag["style"]
                    if img_tag.has_attr("width"):
                        del img_tag["width"]
                    if img_tag.has_attr("height"):
                        del img_tag["height"]
                    
                    img_tag["style"] = "max-width: 100% !important; height: auto !important; display: block; margin: 12px auto;"

                    inline_images.append((filepath, cid, ext))
            except Exception as err:
                print(f"Ошибка загрузки изображения {img_url}: {err}")

        # Обработка титульного изображения (если есть)
        top_img_html = ""
        if top_img_url:
            dummy_img = soup.new_tag("img", src=top_img_url, alt=title)
            process_image(top_img_url, dummy_img)
            top_img_html = str(dummy_img)

        # Обработка всех изображений в теле статьи
        for img in body_div.find_all("img"):
            src = img.get("src")
            if src:
                full_img_url = urljoin(BASE_URL, src)
                process_image(full_img_url, img)

        # Оптимизация стилей всех тегов внутри тела
        for tag in body_div.find_all(True):
            # Гарантируем корректность контейнеров
            if tag.name == "p":
                tag["style"] = "line-height: 1.6; font-size: 16px; margin-bottom: 14px; color: #222222;"
            elif tag.name in ["h2", "h3"]:
                tag["style"] = "margin-top: 24px; margin-bottom: 12px; font-weight: bold; color: #111111;"

        # Сборка финального HTML письма
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; margin: 0; }}
                .container {{ max-width: 680px; background: #ffffff; padding: 25px; margin: 0 auto; border-radius: 8px; }}
                .footer {{ margin-top: 30px; font-size: 12px; color: #888888; text-align: center; border-top: 1px solid #eee; padding-top: 15px; }}
                a {{ color: #0066cc; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1 style="font-weight: bold; color: #000000; font-size: 26px; line-height: 1.3; margin-top: 10px;">{title}</h1>
                <div style="color: #777777; font-size: 13px; margin-bottom: 20px;">{article_date}</div>
                {top_img_html}
                <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;">
                {str(body_div)}
                <div class="footer">
                    Источник: <a href="{article_url}">{article_url}</a><br>
                    Рассылка Pluggedin
                </div>
            </div>
        </body>
        </html>
        """

        msg_alternative = MIMEMultipart("alternative")
        msg.attach(msg_alternative)

        # Текстовая версия
        text_body = f"{title}\n{article_date}\n\nСсылка: {article_url}"
        msg_alternative.attach(MIMEText(text_body, "plain", "utf-8"))
        msg_alternative.attach(MIMEText(html_content, "html", "utf-8"))

        # Прикрепление сохраненных картинок к письму
        for filepath, cid, ext in inline_images:
            with open(filepath, "rb") as f:
                subtype = "jpeg" if ext in ["jpg", "jpeg"] else ext
                img_data = MIMEImage(f.read(), _subtype=subtype)
                img_data.add_header("Content-ID", f"<{cid}>")
                img_data.add_header("Content-Disposition", "inline", filename=os.path.basename(filepath))
                msg.attach(img_data)

        # Отправка через Gmail SMTP
        print("Отправка письма...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())

        print(f"Успешно отправлено: {title}")
        sent_set.add(article_url)
        return True

    finally:
        # Гарантированное удаление временных файлов после отправки
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    sent_set = load_sent_articles()
    urls = get_latest_article_urls()

    if not urls:
        print("Новых статей на странице не обнаружено.")
        return

    new_articles_count = 0
    # Проходим по статьям (начиная со старых к новым)
    for url in reversed(urls):
        if url not in sent_set:
            success = parse_and_send_article(url, sent_set)
            if success:
                new_articles_count += 1
                save_sent_articles(sent_set)

    print(f"\nЗавершено. Обработано новых статей: {new_articles_count}")


if __name__ == "__main__":
    main()
