import logging
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import cv2
import os
import requests
from bs4 import BeautifulSoup
import urllib.parse
import pytesseract
from PIL import Image
import sqlite3
from datetime import datetime, timedelta

# --- Настройки ---
API_TOKEN = 'ВАШ_ТОКЕН_ОТ_BOTFATHER'
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
logging.basicConfig(level=logging.INFO)

# --- База данных ---
conn = sqlite3.connect("parts.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        query TEXT,
        filter_type TEXT,
        price_filter TEXT,
        city TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS cache (
        query TEXT PRIMARY KEY,
        filter_type TEXT,
        price_filter TEXT,
        city TEXT,
        results TEXT,
        avito_link TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")
conn.commit()

# --- Клавиатуры ---
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("📸 Фото"), KeyboardButton("✏ Текст"))
main_kb.add(KeyboardButton("📋 История"), KeyboardButton("🌍 Город"))

filters_kb = ReplyKeyboardMarkup(resize_keyboard=True)
filters_kb.row(KeyboardButton("Оригинал"), KeyboardButton("Аналог"), KeyboardButton("Контрактные"))
filters_kb.row(KeyboardButton("До 5000 ₽"), KeyboardButton("5000-10000 ₽"), KeyboardButton("От 10000 ₽"))
filters_kb.add(KeyboardButton("◀ Назад"))

cities_kb = ReplyKeyboardMarkup(resize_keyboard=True)
for city in ["Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань"]:
    cities_kb.add(KeyboardButton(city))
cities_kb.add(KeyboardButton("◀ Назад"))

# --- OCR: Распознавание артикула ---
def find_part_number(image_path):
    try:
        img = cv2.imread(image_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        temp_path = image_path.replace(".jpg", "_cleaned.jpg")
        cv2.imwrite(temp_path, thresh)
        text = pytesseract.image_to_string(Image.open(temp_path), lang='eng+rus')
        os.remove(temp_path)
        words = text.split()
        part_numbers = [w for w in words if w.isalnum() and len(w) >= 5]
        return part_numbers[0] if part_numbers else None
    except Exception as e:
        logging.error(f"OCR error: {e}")
        return None

# --- VIN / госномер (упрощённо) ---
def detect_car_from_text(text):
    regions = {
        "77": "Москва", "99": "Москва", "177": "Москва",
        "78": "СПб", "98": "СПб", "178": "СПб",
        "54": "Новосибирск", "154": "Новосибирск"
    }
    digits = ''.join(filter(str.isdigit, text[-4:]))
    for code, city in regions.items():
        if digits.startswith(code.replace("1", "", 1)):
            return city
    return None

# --- Drom: парсинг с кэшем ---
def get_drom_url(city):
    mapping = {"Москва": "moskva", "Санкт-Петербург": "sankt-peterburg", "Новосибирск": "novosibirsk"}
    return f"https://{mapping.get(city, 'novosibirsk')}.drom.ru"

def parse_drom(query, part_type="Любые", price_filter="Любая цена", city="Новосибирск"):
    cache_key = (query, part_type, price_filter, city)
    cursor.execute("""
        SELECT results, avito_link FROM cache WHERE query = ? AND filter_type = ? AND price_filter = ? AND city = ?
    """, cache_key)
    cached = cursor.fetchone()

    if cached:
        try:
            cached_time = datetime.strptime(cursor.execute("SELECT timestamp FROM cache WHERE query = ?", (query,)).fetchone()[0],
                                        "%Y-%m-%d %H:%M:%S.%f")
            if datetime.now() - cached_time < timedelta(hours=1):
                results = cached[0].split("|||") if cached[0] else []
                avito_link = cached[1]
                return results, avito_link
        except:
            pass

    url = f"{get_drom_url(city)}/auto/parts/?q={urllib.parse.quote(query)}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('div', class_='b-advItem')[:3]
        results = []
        for item in items:
            title = item.find('a', class_='b-advItem__title')
            price = item.find('div', class_='b-advItem__price')
            if title and price:
                link = get_drom_url(city) + title['href']
                results.append(f"🔧 <b>{title.get_text(strip=True)}</b>\n💵 {price.get_text(strip=True)}\n🔗 <a href='{link}'>Смотреть</a>")
    except:
        results = []

    avito_query = urllib.parse.quote(query)
    avito_city = {"Москва": "moskva", "СПб": "sankt-peterburg"}.get(city, "novosibirsk")
    avito_link = f"https://{avito_city}.avito.ru/all/avtozapchasti_i_aksessuary?q={avito_query}"

    result_str = "|||".join(results) if results else ""
    cursor.execute("""
        INSERT OR REPLACE INTO cache (query, filter_type, price_filter, city, results, avito_link, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
    """, (*cache_key, result_str, avito_link))
    conn.commit()

    return results if results else ["🔍 Нет на Drom"], avito_link

# --- Аналоги ---
analog_map = {
    "BOSCH 0445120012": ["DENSO 123456", "STANDART BS-778"],
    "611113112R": ["77010112", "FEBEST R112"],
}

def find_analogs(part_number):
    return analog_map.get(part_number.upper(), [])

# --- Хранилище ---
user_city = {}
user_state = {}

# --- Обработчик фото ---
@dp.message_handler(content_types=types.ContentType.PHOTO)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    photo_path = f"temp_{user_id}.jpg"
    await message.photo[-1].download(photo_path)

    # Попробуем найти артикул
    part_number = find_part_number(photo_path)
    os.remove(photo_path)

    if part_number:
        await message.answer(f"✅ Найден артикул: <code>{part_number}</code>", parse_mode="HTML")
        city = user_city.get(user_id, "Новосибирск")
        await search_and_show(user_id, part_number, city)
    else:
        await message.answer("📸 Фото получено. Ищем артикул... (OCR не распознал текст)")

# --- Обработчик текста ---
@dp.message_handler(lambda m: m.text in ["📸 Фото", "✏ Текст", "🌍 Город", "📋 История", "◀ Назад"])
async def handle_menu(message: types.Message):
    user_id = message.from_user.id
    if message.text == "📸 Фото":
        await message.answer("Отправьте фото", reply_markup=ReplyKeyboardRemove())
    elif message.text == "✏ Текст":
        await message.answer("Введите артикул или название", reply_markup=ReplyKeyboardRemove())
        user_state[user_id] = "awaiting_text"
    elif message.text == "🌍 Город":
        await message.answer("Выберите город:", reply_markup=cities_kb)
    elif message.text == "📋 История":
        cursor.execute("SELECT query, city, timestamp FROM searches WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5", (user_id,))
        history = cursor.fetchall()
        if history:
            text = "🕘 <b>История поиска:</b>\n\n"
            for q, c, t in history:
                text += f"• <code>{q}</code> — {c} ({t[:16]})\n"
        else:
            text = "📭 История пуста."
        await message.answer(text, parse_mode="HTML", reply_markup=main_kb)

@dp.message_handler(lambda m: user_state.get(m.from_user.id) == "awaiting_text")
async def handle_text_input(message: types.Message):
    user_id = message.from_user.id
    user_state.pop(user_id, None)
    query = message.text.strip()

    detected_city = detect_car_from_text(query)
    if detected_city:
        user_city[user_id] = detected_city
        await message.answer(f"🚗 Определён город: <b>{detected_city}</b>", parse_mode="HTML")

    city = user_city.get(user_id, "Новосибирск")
    await search_and_show(user_id, query, city)

async def search_and_show(user_id, query, city):
    await bot.send_chat_action(user_id, "typing")
    drom_results, avito_link = parse_drom(query, city=city)

    analogs = find_analogs(query)
    if analogs:
        analog_text = "\n".join([f"🔁 {a}" for a in analogs[:2]])
        drom_results.append(f"🔁 <b>Аналоги:</b>\n{analog_text}")

    response = "\n\n".join(drom_results)

    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔍 Искать на Avito", url=avito_link))
    await bot.send_message(user_id, response, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

# --- Город ---
@dp.message_handler(lambda m: m.text in ["Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань"])
async def set_city(message: types.Message):
    user_id = message.from_user.id
    user_city[user_id] = message.text
    await message.answer(f"📍 Город: <b>{message.text}</b>", parse_mode="HTML", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "◀ Назад")
async def back(message: types.Message):
    await message.answer("Меню:", reply_markup=main_kb)

# --- /start ---
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    user_city[message.from_user.id] = "Новосибирск"
    await message.answer(
        "🚀 <b>Бот поиска запчастей</b>\n\n"
        "• 📸 Отправьте фото — найдём артикул\n"
        "• ✏ Введите текст — ищем на Drom\n"
        "• 🌍 Выберите город\n"
        "• 🔍 Получите ссылку на Avito\n"
        "• 📋 Смотрите историю\n\n"
        "Все поиски — легально и быстро.",
        parse_mode="HTML",
        reply_markup=main_kb
    )

# --- Веб-сервер для активности ---
from flask import Flask
app = Flask('')

@app.route('/')
def home():
    return "Бот работает 24/7!"

def run():
    app.run(host='0.0.0.0', port=8080)

if __name__ == '__main__':
    import threading
    t = threading.Thread(target=run)
    t.start()
    executor.start_polling(dp, skip_updates=True)
