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
# 1/A. MODUL: A FOTÓS (Capture - HTML/Selenium) 📸
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

    captured_data = []

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        
        driver.get(target_url)
        time.sleep(10) 

        # Süti és zavaró elemek
        try:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                txt = btn.text.lower()
                if any(x in txt for x in ["elfogad", "accept", "mindent", "ok", "rendben"]):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
                    break
            driver.execute_script("document.querySelectorAll('div[class*=\"cookie\"], #onetrust-banner-sdk').forEach(el => el.remove());")
        except: pass

        # TESZT: 4 kattintás (Címlap + 3 dupla oldal)
        for i in range(4): 
            page_capture_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_cap_{page_capture_num}.png")
            
            if i > 0:
                try:
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                    target = driver.switch_to.frame(iframes[0]) if iframes else driver.find_element(By.TAG_NAME, 'body')
                    ActionChains(driver).send_keys(Keys.ARROW_RIGHT).perform()
                    if iframes: driver.switch_to.default_content()
                except: pass
                time.sleep(5)

            driver.save_screenshot(fajl_nev)
            captured_data.append({
                "image_path": fajl_nev,
                "page_url": driver.current_url,
                "capture_index": page_capture_num
            })
            print(f"   -> {page_capture_num}. fotó elkészült.")

        return captured_data
    except Exception as e:
        print(f"❌ Hiba: {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()

# ===============================================================================
# 1/B. MODUL: A SZELETELŐ (PDF) ✂️📄
# ===============================================================================

def capture_pages_from_pdf(target_url, store_name):
    print(f"\n📄 PDF SZELETELÉS: {target_url}")
    captured_data = []
    temp_pdf_path = os.path.join(TEMP_DIR, f"{store_name}_temp.pdf")
    headers = {"User-Agent": "Mozilla/5.0 Safari/605.1.15"}

    try:
        response = requests.get(target_url, headers=headers, timeout=30)
        with open(temp_pdf_path, 'wb') as f: f.write(response.content)

        doc = fitz.open(temp_pdf_path)
        for i in range(min(4, len(doc))):
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            doc.load_page(i).get_pixmap(dpi=200).save(fajl_nev)
            captured_data.append({
                "image_path": fajl_nev,
                "page_url": f"{target_url}#page={page_num}",
                "page_num": page_num,
                "is_pdf": True
            })
        doc.close()
        return captured_data
    except Exception as e:
        print(f"❌ PDF Hiba: {e}"); return []
    finally:
        if os.path.exists(temp_pdf_path): os.remove(temp_pdf_path)

# ===============================================================================
# 2. MODUL: AI & LOGIKA 🧠
# ===============================================================================

def interpret_text_with_ai(full_text, capture_index, store_name, title_name):
    # Auchan-specifikus és általános dátum keresés az 1. oldalon
    date_instr = """FELADAT 1: KERESD MEG AZ ÚJSÁG TELJES ÉRVÉNYESSÉGI IDEJÉT! 
    Nézd meg a fejlécet és a láblécet. Formátum: YYYY.MM.DD-YYYY.MM.DD. 
    Nagyon fontos Auchan és egyéb újságoknál is!""" if capture_index == 1 else ""

    prompt = f"""
    Kaptál egy OCR szöveget a(z) {store_name} "{title_name}" újságjának {capture_index}. fotójáról.
    
    FONTOS OLDALSZÁM LOGIKA:
    - Az 1. fotó MINDIG a címlap (szimpla oldal).
    - A többi fotó általában DUPLA oldal (pl. a 2. fotón a 2. és 3. oldal látszik).
    
    {date_instr}

    FELADAT 2: TERMÉKEK KIGYŰJTÉSE
    - 'ar': KÖTELEZŐ a szám után a 'Ft' (pl. "1290 Ft"). Ha több ár van, a nagy, fizetendő árat írd be!
    - 'ar_info': Kiszerelés és egységár (pl. "500 g, 2580 Ft/kg"). Ha valami nem olvasható, hagyd ki az adott részt, de törekedj a pontosságra!
    - 'oldal_terfel': Határozd meg, hogy a termék a fotó BAL vagy JOBB felén van. 
      FIGYELEM: Ha ez az 1. fotó (címlap), MINDEN termék legyen "bal"!

    ELVÁRT JSON:
    {{
      "oldal_jelleg": "ÉLELMISZER_VEGYES",
      "ervenyesseg": "2026.02.12-2026.02.18",
      "termekek": [
        {{ "nev": "...", "ar": "999 Ft", "ar_info": "500 g, 1998 Ft/kg", "oldal_terfel": "bal", "kategoria_dontes": "marad" }}
      ]
    }}

    OCR SZÖVEG:
    {full_text}
    """

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(response.choices[0].message.content)

def check_validity_date(date_string):
    if not date_string or "N/A" in str(date_string): return True
    try:
        dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', str(date_string))
        if dates:
            dates.sort()
            end_date = datetime.datetime.strptime(dates[-1].replace('-', '.'), "%Y.%m.%d").date()
            return end_date >= datetime.date.today()
    except: pass
    return True

def process_images_with_ai(captured_data, flyer_meta):
    results = []
    detected_validity = flyer_meta.get('validity', "N/A")
    
    for item in captured_data:
        full_text = google_ocr(item['image_path'])
        if not full_text: continue

        # PDF esetén fix az oldalszám, Seleniumnál számolunk
        c_idx = item.get('capture_index', item.get('page_num', 1))
        structured = interpret_text_with_ai(full_text, c_idx, flyer_meta['store'], flyer_meta['title'])

        if c_idx == 1:
            detected_validity = structured.get("ervenyesseg", detected_validity)
            if not check_validity_date(detected_validity):
                print(f"⛔ LEJÁRT: {detected_validity}")
                return []

        for product in structured.get("termekek", []):
            if product.get("kategoria_dontes") == "marad":
                terfel = product.get("oldal_terfel", "bal").lower()
                
                # --- OLDALSZÁM ÉS LINK MATEK ---
                if item.get('is_pdf'):
                    v_oldalszam = item['page_num']
                    v_link = item['page_url']
                else:
                    # Selenium Flipbook Spread logika
                    if c_idx == 1:
                        v_oldalszam = 1
                    else:
                        # Ha a 2. fotó a 2-3. oldal, akkor a bal=2, jobb=3
                        v_oldalszam = (c_idx - 1) * 2
                        if terfel == "jobb": v_oldalszam += 1
                    
                    # URL frissítése a pontos oldalszámra (pl. .../2 -> .../3)
                    v_link = re.sub(r'(\d+)(/?)$', str(v_oldalszam), item['page_url'])

                results.append({
                    "bolt": flyer_meta['store'],
                    "ujsag": flyer_meta['title'],
                    "oldalszam": v_oldalszam,
                    "ervenyesseg": detected_validity,
                    "nev": product.get("nev"),
                    "ar": product.get("ar"),
                    "ar_info": product.get("ar_info"),
                    "ar_info2": product.get("ar_info2"),
                    "forrasLink": v_link,
                    "alap_link": flyer_meta['url']
                })

        if os.path.exists(item['image_path']): os.remove(item['image_path'])

    return results

def google_ocr(image_path):
    with open(image_path, "rb") as f: content = f.read()
    response = vision_client.document_text_detection(image=vision.Image(content=content))
    return response.full_text_annotation.text if not response.error.message else ""

# ===============================================================================
# FŐVEZÉRLŐ
# ===============================================================================

if __name__ == "__main__":
    print(f"=== PROFESSZOR BOT v6.5 (Precíz Link & Ár) ===\n📅 {datetime.date.today()}")

    if not os.path.exists(INPUT_FILE): exit("❌ Nincs flyers.json!")
    
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        current_flyers = json.load(f).get("flyers", [])

    old_products = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f: old_products = json.load(f)

    final_products = [p for p in old_products if p.get('alap_link') in [f['url'] for f in current_flyers] and check_validity_date(p.get('ervenyesseg'))]
    processed_urls = {p.get('alap_link') for p in final_products}

    for flyer in current_flyers:
        if flyer['url'] in processed_urls: continue
        
        print(f"\n🆕 ÚJ: {flyer['store']} - {flyer['title']}")
        pages = capture_pages_from_pdf(flyer['url'], flyer['store']) if flyer['url'].lower().endswith('.pdf') else capture_pages_with_selenium(flyer['url'], flyer['store'])
        
        if pages:
            new_items = process_images_with_ai(pages, flyer)
            final_products.extend(new_items)
            print(f"✅ {len(new_items)} termék kész.")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_products, f, ensure_ascii=False, indent=2)
    print(f"\n🏁 KÉSZ! Összesen: {len(final_products)} termék.")
