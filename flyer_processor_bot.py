import os
import time
import json
import re
import requests
import fitz # PyMuPDF
from dotenv import load_dotenv
from openai import OpenAI
from google.cloud import vision
import datetime

# Selenium importok
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# ==============================
# 0. KONFIGURÁCIÓ & ENV
# ==============================

base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, ".env"))

ASSETS_DIR = os.path.join(base_dir, "assets")
if not os.path.exists(ASSETS_DIR):
    os.makedirs(ASSETS_DIR)

INPUT_FILE = os.path.join(ASSETS_DIR, 'flyers.json')
OUTPUT_FILE = os.path.join(ASSETS_DIR, 'universal_output.json')

if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google_key.json"

openai_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai_key)
vision_client = vision.ImageAnnotatorClient()

TEMP_DIR = os.path.join(base_dir, "temp_kepek")
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

# ===============================================================================
# 1. SEGÉDFÜGGVÉNYEK (BLOKK SZELETELŐ ÉS OCR) ✂️
# ===============================================================================

def split_text_into_price_blocks(full_text):
    """
    Ár alapú blokk szeletelő.
    Tiszta határok az árak között, visszanyúlás nélkül a duplikáció elkerülésére.
    """
    # Kibővített ár regex: 999Ft, 1.299 Ft, 2999FT
    price_pattern = r'\b\d{1,3}(?:[ .]?\d{3})*\s?F[tT]\b(?!/)'
    matches = list(re.finditer(price_pattern, full_text))
    blocks = []

    if not matches:
        return []

    for i, match in enumerate(matches):
        start = match.start()
        # A blokk kezdete az előző tétel vége, vagy ha az nincs, a szöveg eleje
        prev_end = matches[i-1].end() if i > 0 else 0
        safe_start = prev_end

        # A blokk vége a következő ár kezdete
        if i < len(matches) - 1:
            end = matches[i + 1].start()
        else:
            end = len(full_text)

        block_text = full_text[safe_start:end].strip()
        if 20 < len(block_text) < 1000:
            blocks.append(block_text)

    return blocks

def google_ocr(image_path):
    with open(image_path, "rb") as img_file: content = img_file.read()
    image = vision.Image(content=content)
    response = vision_client.document_text_detection(image=image)
    if response.error.message: return ""
    return response.full_text_annotation.text

# ===============================================================================
# 2. MODUL: BLOKK SZINTŰ AI ÉRTELMEZÉS 🧠
# ===============================================================================

def interpret_text_with_ai_block(block_text, store_name, title_name, prices, units, unit_prices, noises):
    prompt = f"""
    Egy termék BLOKK szövegét kaptad a(z) {store_name} "{title_name}" újságjából.
    Ez a blokk pontosan EGY terméket tartalmaz.

    SZABÁLYOK A SZÁMOKHOZ:
    - KIZÁRÓLAG a fenti listákból választhatsz ár és mennyiség adatot. 
    - Az OCR szövegből új számot (ami nincs a listában) NEM használhatsz!
    - Ha a listák üresek, írj null-t.

    BIZTOS MINTÁK:
    - Teljes árak: {prices}
    - Mennyiségek: {units}
    - Egységár jelöltek: {unit_prices}
    - TILTOTT százalék minták: {noises}

    MEZŐK:
    1) 'ar': Csak a 'Teljes árak' listából. Nem lehet egységár.
    2) 'ar_info': Formátum: "[Mennyiség], [Egységár]". Százalék (%) és marketing szöveg TILOS.
    3) 'oldal_terfel': bal/jobb.

    ELVÁRT JSON:
    {{
      "nev": "...",
      "ar": "999 Ft",
      "ar_info": "500 g, 1998 Ft/kg",
      "ar_info2": null,
      "oldal_terfel": "bal",
      "kategoria_dontes": "marad"
    }}

    BLOKK SZÖVEG:
    {block_text}
    """
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(response.choices[0].message.content)

# ===============================================================================
# 3. MODUL: FOLYAMATVEZÉRLŐ (MÉRNÖKI FINOMÍTÁSOKKAL) ⚙️
# ===============================================================================

def check_validity_date(date_string):
    if not date_string or date_string == "N/A": return True
    try:
        dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', str(date_string))
        if dates:
            dates.sort()
            end_date_str = dates[-1].replace('-', '.')
            end_date = datetime.datetime.strptime(end_date_str, "%Y.%m.%d").date()
            return end_date >= datetime.date.today()
    except: pass
    return True

def process_images_with_ai(captured_data, flyer_meta):
    print(f"🧠 IPARI BLOKK AI Elemzés: {flyer_meta['store']}...")
    results = []
    seen_items = set() 
    final_detected_validity = flyer_meta.get('validity', "N/A")

    try:
        for item in captured_data:
            full_text = google_ocr(item['image_path'])
            if not full_text: continue

            # --- 1. DÁTUM: SZIGORÚ NULL-KEZELÉS (Csak az 1. oldalról) ---
            if item['page_num'] == 1:
                date_prompt = f"""
                Keresd ki a fő érvényességi időt.
                Formátum: YYYY.MM.DD-YYYY.MM.DD
                Ha nem található, válasz: {{ "ervenyesseg": null }}
                Szöveg: {full_text}
                """
                d_resp = client.chat.completions.create(
                    model="gpt-4o", temperature=0, response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": date_prompt}]
                )
                date_json = json.loads(d_resp.choices[0].message.content)
                if date_json.get("ervenyesseg"):
                    final_detected_validity = date_json["ervenyesseg"]

                if not check_validity_date(final_detected_validity):
                    print(f"⛔ LEJÁRT ÚJSÁG: {final_detected_validity}")
                    return []

            # --- 2. OLDAL JELLEG ELLENŐRZÉS ---
            class_resp = client.chat.completions.create(
                model="gpt-4o", temperature=0, response_format={"type": "json_object"},
                messages=[{
                    "role": "user",
                    "content": f'Válaszolj ebben a formában: {{ "jelleg": "ÉLELMISZER_VEGYES" }} \n\n Szöveg: {full_text[:800]}'
                }]
            )
            if json.loads(class_resp.choices[0].message.content).get("jelleg") == "NONFOOD_MARKETING":
                continue

            # --- 3. BLOKK SZELETELÉS ÉS FELDOLGOZÁS ---
            blocks = split_text_into_price_blocks(full_text)
            for block in blocks:
                # Mérnöki regexek a ChatGPT review alapján:
                prices = list(set(re.findall(r'\b\d{1,3}(?:[ .]?\d{3})*\s?F[tT]\b(?!/)', block)))
                # Kezeli az OCR törést ( Ft / kg )
                unit_prices = list(set(re.findall(r'\b\d+(?:[.,]\d+)?\s?F[tT]\s?/\s?(?:kg|g|l|ml|db)\b', block, re.I)))
                # Kizárja a marketing szövegeket (lookahead)
                units = list(set(re.findall(r'\b\d+(?:[.,]\d+)?\s?(?:kg|g|l|ml|db)\b(?!\s*[a-zA-Záéíóöőúüű])', block, re.I)))
                noises = list(set(re.findall(r'\b\d+\s?%\b', block)))

                if not prices: continue

                structured = interpret_text_with_ai_block(
                    block, flyer_meta['store'], flyer_meta['title'],
                    prices, units, unit_prices, noises
                )

                if structured.get("kategoria_dontes") == "marad" and structured.get("ar"):
                    # --- 4. DEDUPLIKÁCIÓ ÉS VALIDÁCIÓ ---
                    item_key = (structured.get("nev"), structured.get("ar"))
                    if item_key in seen_items: continue
                    
                    # Ár-sanity check (150k limit gyanús élelmiszernél)
                    try:
                        num_p = int(re.sub(r'\D', '', structured.get("ar")))
                        if num_p > 150000: continue 
                    except: pass

                    seen_items.add(item_key)

                    # --- 5. OLDALMATEK (BAL/JOBB) ---
                    terfel = str(structured.get("oldal_terfel", "bal")).lower()
                    v_link = item['page_url']
                    v_oldal = item['page_num']
                    
                    if terfel == "jobb" and not flyer_meta['url'].lower().endswith('.pdf'):
                        v_link = re.sub(r'(\d+)(/?)$', lambda m: str(int(m.group(1)) + 1) + m.group(2), item['page_url'])
                        v_oldal += 1

                    results.append({
                        "bolt": flyer_meta['store'],
                        "ujsag": flyer_meta['title'],
                        "oldalszam": v_oldal,
                        "ervenyesseg": final_detected_validity,
                        "nev": structured.get("nev"),
                        "ar": structured.get("ar"),
                        "ar_info": structured.get("ar_info"),
                        "ar_info2": structured.get("ar_info2"),
                        "forrasLink": v_link,
                        "alap_link": flyer_meta['url']
                    })
                    print(f"      + {structured.get('nev')} | {structured.get('ar')}")

    except Exception as e:
        print(f"⚠️ AI hiba: {e}")
    finally:
        for item in captured_data:
            if os.path.exists(item['image_path']): os.remove(item['image_path'])
    return results

# ===============================================================================
# FŐ MODULOK (FOTÓZÁS ÉS PDF SZELETELÉS - EREDETI) 📸
# ===============================================================================

def capture_pages_with_selenium(target_url, store_name):
    print(f"\n📸 FOTÓZÁS INDUL ({store_name}): {target_url}")
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        driver.get(target_url)
        time.sleep(10) 

        # Süti kezelés és egyéb zavaró elemek
        try:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                txt = btn.text.lower()
                if any(x in txt for x in ["elfogad", "accept", "ok", "rendben"]):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
                    break
        except: pass

        captured_data = []
        for i in range(4): 
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            if i > 0:
                try:
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                    if iframes:
                        driver.switch_to.frame(iframes[0])
                        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ARROW_RIGHT)
                        driver.switch_to.default_content()
                    else:
                        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ARROW_RIGHT)
                except: pass
                time.sleep(5)
            driver.save_screenshot(fajl_nev)
            captured_data.append({"image_path": fajl_nev, "page_url": driver.current_url, "page_num": page_num})
        return captured_data
    finally:
        if 'driver' in locals(): driver.quit()

def capture_pages_from_pdf(target_url, store_name):
    print(f"\n📄 PDF SZELETELÉS INDUL ({store_name}): {target_url}")
    temp_pdf = os.path.join(TEMP_DIR, f"{store_name}_temp.pdf")
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"}
    try:
        r = requests.get(target_url, headers=headers, timeout=30)
        with open(temp_pdf, 'wb') as f: f.write(r.content)
        doc = fitz.open(temp_pdf)
        captured_data = []
        for i in range(min(4, len(doc))):
            page_num = i + 1
            pix = doc.load_page(i).get_pixmap(dpi=200)
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            pix.save(fajl_nev)
            captured_data.append({"image_path": fajl_nev, "page_url": f"{target_url}#page={page_num}", "page_num": page_num})
        doc.close()
        return captured_data
    finally:
        if os.path.exists(temp_pdf): os.remove(temp_pdf)

# ===============================================================================
# FŐVEZÉRLŐ
# ===============================================================================

if __name__ == "__main__":
    print("=== PROFESSZOR BOT: INDUSTRIAL BLOCK VERSION (v7.3) ===")
    if not os.path.exists(INPUT_FILE): exit()
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        current_flyers = json.load(f).get("flyers", [])
    
    old_products = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f: old_products = json.load(f)

    final_products = []
    processed_urls = set()
    active_urls = [f['url'] for f in current_flyers]

    # Tisztítás
    for p in old_products:
        link = p.get('alap_link', p.get('forrasLink'))
        if link in active_urls and check_validity_date(p.get('ervenyesseg')):
            final_products.append(p)
            processed_urls.add(link)

    # Feldolgozás
    for flyer in current_flyers:
        if flyer['url'] in processed_urls: continue
        pages = capture_pages_from_pdf(flyer['url'], flyer['store']) if flyer['url'].lower().endswith('.pdf') else capture_pages_with_selenium(flyer['url'], flyer['store'])
        if pages:
            new_items = process_images_with_ai(pages, flyer)
            if new_items: final_products.extend(new_items)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_products, f, ensure_ascii=False, indent=2)
    print(f"\n🏁 KÉSZ! Adatbázis: {len(final_products)} termék.")
