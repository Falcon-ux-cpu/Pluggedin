import os
import re
import json
import shutil
import tempfile
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

# --- Настройки ---
BASE_URL = "https://pluggedin.ru"
FEED_URL = "https://pluggedin.ru/open"
SENT_FILE = "sent_articles.json"

# SMTP Настройки из переменных окружения (GitHub Secrets)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")  # App Password от Gmail
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")


def load_sent_articles():
    if os.path.exists(SENT_FILE):
        try:
            with open(SENT_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Ошибка чтения {SENT_FILE}: {e}")
            return set()
    return set()


def save_sent_articles(sent_set):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_set), f, ensure_ascii=False, indent=2)


def get_latest_article_urls():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(FEED_URL, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    links = []

    # Ищем ссылки на статьи формата /open/...
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/open/" in href and href != "/open":
            full_url = urljoin(BASE_URL, href)
            if full_url not in links:
                links.append(full_url)

    return links


def parse_article(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    # 1. Заголовок
    title_elem = soup.find("h1", {"itemprop": "headline"})
    title = title_elem.get_text(strip=True) if title_elem else "Без названия"

    # 2. Дата
    date_elem = soup.find("span", class_="dateArticle")
    date_str = date_elem.get_text(strip=True) if date_elem else ""

    # 3. Главная обложка
    top_img_elem = soup.find("img", {"itemprop": "image"})
    top_img_url = None
    if top_img_elem and top_img_elem.get("src"):
        top_img_url = urljoin(BASE_URL, top_img_elem["src"])

    # 4. Тело статьи
    body_elem = soup.find("div", class_="open-article-text")
    if not body_elem:
        print(f"Не удалось найти тело статьи по адресу: {url}")
        return None

    return {
        "url": url,
        "title": title,
        "date": date_str,
        "top_img_url": top_img_url,
        "body_soup": body_elem
    }


def send_article_email(article_data):
    msg = MIMEMultipart("related")
    msg["Subject"] = f"[PluggedIn] {article_data['title']}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    # Временная папка для скачивания изображений
    temp_dir = tempfile.mkdtemp()
    images_to_attach = []  # список кортежей: (path, cid, filename)
    img_counter = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    try:
        # --- Обработка титульной картинки ---
        top_img_html = ""
        if article_data["top_img_url"]:
            try:
                res = requests.get(article_data["top_img_url"], headers=headers, stream=True)
                if res.status_code == 200:
                    img_counter += 1
                    ext = os.path.splitext(article_data["top_img_url"])[1].split('?')[0] or '.jpg'
                    filename = f"image_{img_counter}{ext}"
                    filepath = os.path.join(temp_dir, filename)

                    with open(filepath, "wb") as f:
                        for chunk in res.iter_content(8192):
                            f.write(chunk)

                    cid = f"img_{img_counter}"
                    images_to_attach.append((filepath, cid, filename))

                    # Формируем HTML для титульной картинки с оптимизацией
                    top_img_html = f'''
                    <div style="text-align: center; margin-bottom: 20px;">
                        <img src="cid:{cid}" alt="{article_data['title']}" style="max-width: 100% !important; height: auto !important; display: block; margin: 0 auto; border-radius: 8px;" />
                    </div>
                    '''
            except Exception as e:
                print(f"Ошибка при скачивании главного изображения: {e}")

        # --- Обработка изображений в теле статьи ---
        body_soup = article_data["body_soup"]

        for img in body_soup.find_all("img"):
            src = img.get("src")
            if not src:
                continue

            abs_src = urljoin(BASE_URL, src)

            try:
                res = requests.get(abs_src, headers=headers, stream=True)
                if res.status_code == 200:
                    img_counter += 1
                    ext = os.path.splitext(abs_src)[1].split('?')[0] or '.jpg'
                    filename = f"image_{img_counter}{ext}"
                    filepath = os.path.join(temp_dir, filename)

                    with open(filepath, "wb") as f:
                        for chunk in res.iter_content(8192):
                            f.write(chunk)

                    cid = f"img_{img_counter}"
                    images_to_attach.append((filepath, cid, filename))

                    # Заменяем src на cid
                    img["src"] = f"cid:{cid}"

                    # Оптимизируем стили для адаптивности и предотвращения выпирания
                    img["style"] = "max-width: 100% !important; height: auto !important; display: block; margin: 10px auto;"
            except Exception as e:
                print(f"Ошибка скачивания картинки {abs_src}: {e}")

        # Сборка финального HTML письма
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    color: #333333;
                    max-width: 700px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                .header {{
                    margin-bottom: 20px;
                    border-bottom: 2px solid #eeeeee;
                    padding-bottom: 10px;
                }}
                .title {{
                    font-size: 26px;
                    font-weight: bold;
                    color: #000000;
                    margin: 0 0 10px 0;
                }}
                .date {{
                    font-size: 14px;
                    color: #777777;
                }}
                .content img {{
                    max-width: 100% !important;
                    height: auto !important;
                }}
                .footer {{
                    margin-top: 30px;
                    padding-top: 15px;
                    border-top: 1px solid #eeeeee;
                    font-size: 12px;
                    color: #888888;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1 class="title">{article_data['title']}</h1>
                <div class="date">{article_data['date']}</div>
            </div>
            
            {top_img_html}
            
            <div class="content">
                {str(body_soup)}
            </div>
            
            <div class="footer">
                <p>Оригинал статьи: <a href="{article_data['url']}">{article_data['url']}</a></p>
            </div>
        </body>
        </html>
        """

        msg_alternative = MIMEMultipart("alternative")
        msg.attach(msg_alternative)

        # Текстовая версия
        text_part = MIMEText(f"{article_data['title']}\n\n{article_data['url']}", "plain", "utf-8")
        msg_alternative.attach(text_part)

        # HTML версия
        html_part = MIMEText(html_content, "html", "utf-8")
        msg_alternative.attach(html_part)

        # Прикрепляем изображения как CID
        for filepath, cid, filename in images_to_attach:
            with open(filepath, "rb") as f:
                mime_img = MIMEImage(f.read())
                mime_img.add_header("Content-ID", f"<{cid}>")
                mime_img.add_header("Content-Disposition", "inline", filename=filename)
                msg.attach(mime_img)

        # Отправка по SMTP
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)

        print(f"Статья успешно отправлена: {article_data['title']}")
        return True

    except Exception as e:
        print(f"Ошибка при сборке/отправке письма: {e}")
        return False

    finally:
        # Автоматическая очистка временной папки и скачанных файлов
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


def main():
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        raise ValueError("Отсутствуют необходимые переменные окружения для почты!")

    sent_articles = load_sent_articles()
    urls = get_latest_article_urls()

    print(f"Найдено статей на главной: {len(urls)}")

    new_articles_count = 0
    # Проходим по статьям в обратном порядке (от старых к новым)
    for url in reversed(urls):
        if url in sent_articles:
            continue

        print(f"Обработка новой статьи: {url}")
        article_data = parse_article(url)

        if article_data:
            success = send_article_email(article_data)
            if success:
                sent_articles.add(url)
                new_articles_count += 1

    if new_articles_count > 0:
        save_sent_articles(sent_articles)
        print(f"Сохранено новых статей: {new_articles_count}")
    else:
        print("Новых статей для отправки не найдено.")


if __name__ == "__main__":
    main()
