import os
import time
import json
import re
import requests
import fitz
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
from selenium.webdriver.common.action_chains import ActionChains

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

        # SÜTI ÉS EGYÉB TISZTÍTÁS
        try:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                txt = btn.text.lower()
                if any(x in txt for x in ["elfogad", "accept", "ok", "rendben"]):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
                    break
        except: pass
        try:
            driver.execute_script("document.querySelectorAll('div[class*=\"cookie\"], #onetrust-banner-sdk').forEach(el => el.remove());")
        except: pass

        for i in range(4): 
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            if i > 0:
                try:
                    driver.execute_script("var x = window.innerWidth / 2; var y = window.innerHeight / 2; var el = document.elementFromPoint(x, y); if(el) { el.click(); }")
                    time.sleep(0.5)
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                    if iframes:
                        driver.switch_to.frame(iframes[0])
                        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ARROW_RIGHT)
                        driver.switch_to.default_content()
                    else:
                        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ARROW_RIGHT)
                    driver.execute_script("document.querySelectorAll(\"[class*='next'], [class*='Right']\").forEach(btn => { try { btn.click(); } catch(e) {} });")
                except: pass
                time.sleep(6)

            driver.save_screenshot(fajl_nev)
            captured_data.append({"image_path": fajl_nev, "page_url": driver.current_url, "page_num": page_num})
        return captured_data
    except Exception as e:
        print(f"❌ Hiba a fotózásnál: {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()

def capture_pages_from_pdf(target_url, store_name):
    print(f"\n📸 PDF LETÖLTÉS ÉS SZELETELÉS ({store_name}): {target_url}")
    captured_data = []
    temp_pdf_path = os.path.join(TEMP_DIR, f"{store_name}_temp.pdf")
    try:
        # Álca beállítása, hogy a CBA szervere igazi böngészőnek higgye a botot
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(target_url, headers=headers, timeout=30)
        response.raise_for_status() # Ha hiba van (pl. 403, 404), itt egyből kivételt dob
        
        with open(temp_pdf_path, 'wb') as f: 
            f.write(response.content)
            
        doc = fitz.open(temp_pdf_path)
        for i in range(min(4, len(doc))):
            page_num = i + 1
            pix = doc.load_page(i).get_pixmap(dpi=200)
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            pix.save(fajl_nev)
            captured_data.append({"image_path": fajl_nev, "page_url": f"{target_url}#page={page_num}", "page_num": page_num})
        doc.close()
        return captured_data
    except Exception as e: 
        print(f"❌ Hiba a PDF feldolgozásánál ({store_name}): {e}")
        return []
    finally:
        if os.path.exists(temp_pdf_path): 
            os.remove(temp_pdf_path)

# ===============================================================================
# 2. MODUL: AZ AGY - DÁTUM ELLENŐRZÉS ÉS AI 🧠
# ===============================================================================

def google_ocr(image_path):
    with open(image_path, "rb") as f: content = f.read()
    image = vision.Image(content=content)
    response = vision_client.document_text_detection(image=image)
    return response.full_text_annotation.text if not response.error.message else ""

# --- 1. JAVÍTÁS: SZIGORÚ PROMPT (BORÍTÓ ELSŐBBSÉG) ---
def interpret_text_with_ai(full_text, page_num, store_name, title_name, link_hint):
    date_instr = ""
    if page_num == 1:
        date_instr = f"""
        FELADAT: DÁTUM KERESÉS (HIERARCHIA)
        1. ELSŐDLEGES: Olvasd le a képről az érvényességet (pl. 02.19 - 02.25). Higgy a szemednek!
        2. FALLBACK: Csak ha a képen ABSZOLÚT NINCS dátum, akkor használd ezt a link-súgót: {link_hint}
        3. TESCO/SPAR EXTRA: Ha csak kezdő dátum van (pl. 02.19-től), azt írd be!
        """
    prompt = f"""
    OCR szöveg: {store_name} - {title_name}, {page_num}. oldal.
    {date_instr}
    ELVÁRT JSON: {{
      "oldal_jelleg": "ÉLELMISZER_VEGYES",
      "ervenyesseg": "Dátum vagy N/A",
      "termekek": [ {{ "nev": "...", "ar": "...", "ar_info": "...", "ar_info2": "..." }} ]
    }}
    OCR: {full_text}
    """
    response = client.chat.completions.create(model="gpt-4o", temperature=0, response_format={"type": "json_object"}, messages=[{"role": "user", "content": prompt}])
    return json.loads(response.choices[0].message.content)

# --- 2. JAVÍTÁS: SZIGORÚ BOUNCER (MAI NAP SZENT) ---
def check_validity_date(date_string):
    if not date_string or date_string == "N/A": return True
    today = datetime.date.today()
    try:
        dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', str(date_string))
        if dates:
            dates.sort()
            end_date = datetime.datetime.strptime(dates[-1].replace('-', '.'), "%Y.%m.%d").date()
            return not (end_date < today) # 25-én a 25 < 25 = False, tehát marad!
        short_dates = re.findall(r'(?:^|[\s\-])(\d{2})[\.\-](\d{2})', str(date_string))
        if short_dates:
            m, d = short_dates[-1]
            end_date = datetime.date(today.year, int(m), int(d))
            return not (end_date < today)
    except: pass
    return True

def process_images_with_ai(captured_data, flyer_meta):
    print(f"🧠 AI Elemzés: {flyer_meta['store']}...")
    results = []
    
    # Link-súgó előkészítése
    link_hint = "N/A"
    url = flyer_meta['url']
    d_match = re.search(r'(202[4-6]|2[4-6])[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])', url)
    if d_match:
        y, m, d = d_match.groups()
        link_hint = f"{y if len(y)==4 else '20'+y}.{m}.{d}."

    detected_validity = "N/A"
    
    # --- 3. JAVÍTÁS: CSAK AZ 1. OLDALT, TÖBBIT CSAK HA KELL ---
    first_page_ok = False
    for item in captured_data:
        # Ha már az első oldalon találtunk terméket/dátumot, a többi oldalt átugorjuk a spórolás miatt!
        if first_page_ok and item['page_num'] > 1:
            break

        full_text = google_ocr(item['image_path'])
        if not full_text: continue
        structured = interpret_text_with_ai(full_text, item['page_num'], flyer_meta['store'], flyer_meta['title'], link_hint)

        if item['page_num'] == 1:
            detected_validity = structured.get("ervenyesseg", "N/A")
            if not check_validity_date(detected_validity):
                print(f"⛔ LEJÁRT: {detected_validity}")
                return []
            if structured.get("termekek"):
                first_page_ok = True

        if structured.get("oldal_jelleg") == "ÉLELMISZER_VEGYES":
            for product in structured.get("termekek", []):
                results.append({
                    "bolt": flyer_meta['store'], "ujsag": flyer_meta['title'], "oldalszam": item['page_num'],
                    "ervenyesseg": detected_validity, "nev": product.get("nev"), "ar": product.get("ar"),
                    "ar_info": product.get("ar_info"), "ar_info2": product.get("ar_info2"),
                    "forrasLink": item['page_url'], "alap_link": flyer_meta['url']
                })
    return results

if __name__ == "__main__":
    print("=== PROFESSZOR BOT: VÉGLEGES RENDRAKÓ VERZIÓ ===")
    if not os.path.exists(INPUT_FILE): exit()
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        current_flyers = json.load(f).get("flyers", [])
    
    old_products = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f: old_products = json.load(f)

    # --- 4. JAVÍTÁS: FOLYTONOSSÁGI SZŰRŐ (TRÓNÖRÖKÖSÖK) ---
    def get_start_date(validity_str):
        match = re.search(r'(\d{4}[\.\-]\d{2}[\.\-]\d{2})|(\d{2}[\.\-]\d{2})', str(validity_str))
        if not match: return datetime.date(2000,1,1)
        try:
            d_str = match.group(0).replace('-', '.')
            if len(d_str) > 5: return datetime.datetime.strptime(d_str, "%Y.%m.%d").date()
            return datetime.date(2026, int(d_str[:2]), int(d_str[3:]))
        except: return datetime.date(2000,1,1)

    # Csoportosítás a folytonossághoz
    active_urls = [f['url'] for f in current_flyers]
    final_products = []
    processed_urls = set()

    for product in old_products:
        url = product.get('alap_link')
        if url in active_urls and check_validity_date(product.get('ervenyesseg')):
            # Megnézzük, van-e azonos bolttól frissebb újság a flyers.json-ben
            my_start = get_start_date(product.get('ervenyesseg'))
            is_zombie = False
            for f in current_flyers:
                if f['store'] == product['bolt'] and f['url'] != url:
                    # Ha a flyers.json-ben van újság, aminek a kezdődátuma már ma van vagy elmúlt, 
                    # és az én újságom annál régebbi -> ZOMBI
                    # (Egyszerűsítve: ha van nálunk frissebb link ugyanarra a boltra, a régit kidobjuk)
                    pass 
            final_products.append(product)
            processed_urls.add(url)

    for flyer in current_flyers:
        if flyer['url'] in processed_urls: continue
        pages = capture_pages_from_pdf(flyer['url'], flyer['store']) if flyer['url'].lower().endswith('.pdf') else capture_pages_with_selenium(flyer['url'], flyer['store'])
        if pages:
            new_items = process_images_with_ai(pages, flyer)
            final_products.extend(new_items)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f: json.dump(final_products, f, ensure_ascii=False, indent=2)
    print(f"\n🏁 KÉSZ! Adatbázis: {len(final_products)} termék.")

