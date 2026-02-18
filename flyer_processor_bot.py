import os
import time
import json
import re
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

INPUT_FILE = 'assets/flyers.json'
OUTPUT_FILE = 'assets/universal_output.json' 

base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, ".env"))

if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google_key.json"

openai_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai_key)
vision_client = vision.ImageAnnotatorClient()

TEMP_DIR = os.path.join(base_dir, "temp_kepek")
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

# ===============================================================================
# 1. MODUL: A NÉV-TISZTÍTÓ GÉP (Zrt. Gyilkos & Coop Térkép) ✂️
# ===============================================================================

def clean_text_final(text):
    """Minden céges sallangot eltávolít."""
    if not text: return ""
    # Törli: Zrt, Kft, Bt, Kereskedelmi stb. (ponttal vagy anélkül)
    forbidden = [r'Zrt\.?', r'Kft\.?', r'Bt\.?', r'Kereskedelmi', r'Vállalat']
    cleaned = text
    for word in forbidden:
        cleaned = re.sub(word, '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def get_refined_store_name(store_base, url):
    """Link alapján kényszeríti a pontos hálózatnevet."""
    u = url.lower()
    # COOP SZABÁLYOK
    if "mecsek" in u: return "Coop Mecsek Füszért"
    if "tisza" in u or "szolnok" in u: return "Tisza-Coop"
    if "alfold" in u or "alföld" in u or "kecskemet" in u: return "Alföld Pro-Coop"
    if "hetforras" in u or "szombathely" in u: return "Hétforrás"
    if "eszak-kelet" in u or "debrecen" in u or "miskolc" in u: return "Észak-Kelet Pro-Coop"
    if "polus" in u: return "Pólus-Coop"
    if "honi" in u: return "Honi-Coop"
    
    # CBA SZABÁLYOK
    if "prima" in u or "príma" in u: return "CBA Príma"
    if "cba" in u: return "CBA"
    
    return clean_text_final(store_base)

# ===============================================================================
# 2. MODUL: A FOTÓS (CBA-val tesztelt beállítás) 📸
# ===============================================================================

def capture_pages_with_selenium(target_url, store_name):
    print(f"\n📸 FOTÓZÁS: {store_name} -> {target_url}")
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    captured_data = []
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.get(target_url)
        time.sleep(10) # Bőven hagyunk időt

        # 2 OLDAL
        for i in range(2):
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"p{page_num}_{int(time.time())}.png")
            if i > 0:
                driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ARROW_RIGHT)
                time.sleep(4)
            driver.save_screenshot(fajl_nev)
            captured_data.append({"image_path": fajl_nev, "page_num": page_num})
        return captured_data
    except Exception as e:
        print(f"❌ Fotó hiba: {e}"); return []
    finally:
        if 'driver' in locals(): driver.quit()

# ===============================================================================
# 3. MODUL: AZ AGY - VIZUÁLIS PÁROSÍTÁS 🧠
# ===============================================================================

def interpret_text_with_ai(full_text, page_num, store_name, url_title):
    # CSAK az első oldalon keresünk nevet és dátumot
    visual_logic = ""
    if page_num == 1:
        visual_logic = f"""
        FELADAT 1 (HORGONY LOGIKA): 
        1. Keresd meg a képen az ÉRVÉNYESSÉGI DÁTUMOT (YYYY.MM.DD-YYYY.MM.DD)!
        2. Keresd meg az ÚJSÁG NEVÉT PONTOSAN A DÁTUM MELLETT, ALATT VAGY FELETT! 
           (Pl. 'Penny Akciós újság', 'Tisza-Coop Szuper Plusz').
        3. A nagy marketing szlogeneket (pl. 'Szuper árak', 'Bomba ajánlat') HAGYD FIGYELMEN KÍVÜL!
        """

    prompt = f"""
    Ez a(z) {store_name} újság {page_num}. oldala.
    {visual_logic}
    
    FELADAT 2: Gyűjtsd ki a termékeket JSON-be.
    SZABÁLY: 'ar_info'-ba KÖTELEZŐ a kiszerelés ÉS az egységár (számold ki, ha nincs ott)!
    
    JSON FORMAT:
    {{
      "ujsag_neve": "...", 
      "datum": "...", 
      "termekek": [
        {{ "nev": "...", "ar": "...", "ar_info": "...", "ar_info2": null, "kategoria_dontes": "marad" }}
      ]
    }}
    OCR: {full_text}
    """
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(response.choices[0].message.content)

def check_validity_date(date_string):
    if not date_string or len(str(date_string)) < 5: return True
    try:
        dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', str(date_string))
        if dates:
            end_date = datetime.datetime.strptime(dates[-1].replace('-', '.'), "%Y.%m.%d").date()
            if end_date < datetime.date.today(): return False
    except: pass
    return True

def process_flyer(flyer):
    # NÉV KÉNYSZERÍTÉS LINK ALAPJÁN
    refined_store = get_refined_store_name(flyer['store'], flyer['url'])
    print(f"🤖 Feldolgozás: {refined_store}")
    
    pages = capture_pages_with_selenium(flyer['url'], flyer['store'])
    if not pages: return []

    final_results = []
    current_title = flyer['title']
    current_date = flyer.get('validity', '')

    for p in pages:
        with open(p['image_path'], "rb") as f: content = f.read()
        ocr_text = vision_client.document_text_detection(image=vision.Image(content=content)).full_text_annotation.text
        
        if ocr_text:
            data = interpret_text_with_ai(ocr_text, p['page_num'], refined_store, flyer['title'])
            
            if p['page_num'] == 1:
                # Kép győz a link felett névben és dátumban is!
                if data.get("ujsag_neve") and len(data["ujsag_neve"]) > 3:
                    current_title = data["ujsag_neve"]
                if data.get("datum") and len(data["datum"]) > 5:
                    current_date = data["datum"]
                
                # MENTŐÖV: Ha a képről leolvasott dátum már lejárt, itt állunk meg!
                if not check_validity_date(current_date):
                    print(f"⛔ Lejárt: {current_date}"); return []

            for prod in data.get("termekek", []):
                if prod.get("kategoria_dontes") == "marad":
                    final_results.append({
                        "bolt": refined_store,
                        "ujsag": clean_text_final(current_title),
                        "ervenyesseg": current_date,
                        "nev": prod.get("nev"),
                        "ar": prod.get("ar"),
                        "ar_info": prod.get("ar_info"),
                        "ar_info2": prod.get("ar_info2"),
                        "forrasLink": flyer['url']
                    })
        if os.path.exists(p['image_path']): os.remove(p['image_path'])
    return final_results

# ===============================================================================
# FŐVEZÉRLŐ
# ===============================================================================

if __name__ == "__main__":
    if not os.path.exists(INPUT_FILE): exit()
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        flyers = json.load(f).get("flyers", [])

    old_data = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f: old_data = json.load(f)

    # Tisztítás (Csak ami még a linkekben benne van és érvényes)
    active_urls = [f['url'] for f in flyers]
    final_list = [p for p in old_data if p['forrasLink'] in active_urls and check_validity_date(p['ervenyesseg'])]
    
    processed_urls = {p['forrasLink'] for p in final_list}

    for f in flyers:
        # NINCS ELŐRE KIDOBÁS! Minden új linket megnyitunk ellenőrizni.
        if f['url'] not in processed_urls:
            new_items = process_flyer(f)
            if new_items: final_list.extend(new_items)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    print("🏁 Kész.")
