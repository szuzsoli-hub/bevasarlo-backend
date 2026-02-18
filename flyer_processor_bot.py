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
from selenium.webdriver.common.action_chains import ActionChains

# ==============================
# 0. KONFIGURÁCIÓ & ENV
# ==============================

INPUT_FILE = 'assets/flyers.json'  # A friss linkek (A modulból)
OUTPUT_FILE = 'universal_output.json'  # A kész adatbázis (B modul)

base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, ".env"))

# GitHub Actions környezetben a secretből jön, lokálisan a fájlból/env-ből
if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google_key.json"

openai_key = os.getenv("OPENAI_API_KEY")

if not openai_key:
    print("⚠️ FIGYELEM: Nincs OpenAI kulcs a környezeti változókban!")

client = OpenAI(api_key=openai_key)
vision_client = vision.ImageAnnotatorClient()

TEMP_DIR = os.path.join(base_dir, "temp_kepek")
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)


# ===============================================================================
# 1. MODUL: A FOTÓS (Capture) 📸
# ===============================================================================

def capture_pages_with_selenium(target_url, store_name):
    print(f"\n📸 FOTÓZÁS INDUL ({store_name}): {target_url}")

    chrome_options = Options()
    chrome_options.add_argument("--headless")  # GitHub Actions miatt kötelező!
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    captured_data = []

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.get(target_url)
        time.sleep(8)

        # SÜTI KEZELÉS
        try:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                txt = btn.text.lower()
                if any(x in txt for x in ["elfogad", "accept", "mindent", "ok", "rendben"]):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
                    break
        except:
            pass

        # Zavaró elemek törlése
        try:
            driver.execute_script("""
                document.querySelectorAll('div[class*="cookie"], div[id*="cookie"], #onetrust-banner-sdk').forEach(el => el.remove());
            """)
        except:
            pass

        # FOTÓZÁS (TESZT: CSAK 2 OLDAL!)
        for i in range(2):
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")

            if i > 0:
                body = driver.find_element(By.TAG_NAME, 'body')
                body.send_keys(Keys.ARROW_RIGHT)
                time.sleep(3)

            driver.save_screenshot(fajl_nev)
            captured_data.append({
                "image_path": fajl_nev,
                "page_url": driver.current_url,
                "page_num": page_num
            })
            print(f"   -> {page_num}. oldal lefotózva.")

        return captured_data

    except Exception as e:
        print(f"❌ Hiba a fotózásnál ({store_name}): {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()


# ===============================================================================
# 2. MODUL: AZ AGY - DÁTUM ELLENŐRZÉS (BOUNCER) 🧠
# ===============================================================================

def google_ocr(image_path):
    with open(image_path, "rb") as img_file: content = img_file.read()
    image = vision.Image(content=content)
    response = vision_client.document_text_detection(image=image)
    if response.error.message: return ""
    return response.full_text_annotation.text


def interpret_text_with_ai(full_text, page_num, store_name):
    # Dátum instrukció csak az első oldalon
    date_instr = "FELADAT 1: KERESD MEG AZ ÉRVÉNYESSÉGI IDŐT (YYYY.MM.DD-YYYY.MM.DD) a címlapon!" if page_num == 1 else ""

    prompt = f"""
    Ez a(z) {store_name} akciós újság {page_num}. oldala.
    {date_instr}

    FELADAT 2: Gyűjtsd ki az élelmiszer és vegyi áru termékeket JSON-be.
    SZŰRÉS: Ne vegyél fel marketing dumát, receptet, vagy non-food (ruha, barkács) terméket, csak ha egyértelműen élelmiszer/vegyi áru.

    MEZŐK:
    - 'nev': Termék neve.
    - 'ar': Ár.
    - 'ar_info': Kiszerelés ÉS egységár. HA VAN "/kg" vagy "/l" a képen, azt KÖTELEZŐ ideírni!
    - 'ar_info2': Feltételek (pl. "Csak 2 db esetén"). Ha nincs, legyen null.

    JSON FORMAT:
    {{
      "ervenyesseg": "2026.02.12-2026.02.18", 
      "termekek": [
        {{ "nev": "...", "ar": "...", "ar_info": "...", "ar_info2": null, "kategoria_dontes": "marad" }}
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
    """
    Központi Dátum Ellenőr.
    True = Érvényes
    False = Lejárt (Azonnali törlés)
    """
    if not date_string: return True  # Ha nincs adat, a biztonság kedvéért átengedjük (User check)

    try:
        # Dátum keresés (YYYY.MM.DD vagy YYYY-MM-DD)
        dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', str(date_string))

        if dates:
            # Az utolsó dátum a lejárati idő
            end_date_str = dates[-1].replace('-', '.')
            end_date = datetime.datetime.strptime(end_date_str, "%Y.%m.%d").date()
            today = datetime.date.today()

            if end_date < today:
                return False  # LEJÁRT
            else:
                return True  # MÉG JÓ

    except Exception:
        pass

    return True


def process_images_with_ai(captured_data, flyer_meta):
    print(f"🧠 AI Elemzés: {flyer_meta['store']}...")
    results = []
    detected_validity = flyer_meta.get('validity', "N/A")

    for item in captured_data:
        try:
            full_text = google_ocr(item['image_path'])
            if not full_text:
                os.remove(item['image_path'])
                continue

            structured = interpret_text_with_ai(full_text, item['page_num'], flyer_meta['store'])

            # --- 1. BOUNCER: FRISS ÚJSÁG DÁTUM ELLENŐRZÉS ---
            if item['page_num'] == 1:
                if structured.get("ervenyesseg"):
                    detected_validity = structured.get("ervenyesseg")
                    # Ha az AI szerint a címlapon lévő dátum lejárt -> KUKA
                    if not check_validity_date(detected_validity):
                        print(
                            f"⛔ BOUNCER: Ez az újság lejárt ({detected_validity}), teljes törlés! - {flyer_meta['title']}")
                        os.remove(item['image_path'])
                        return []  # Üres lista = Az egész újság kuka

            for product in structured.get("termekek", []):
                if product.get("kategoria_dontes") == "marad":
                    record = {
                        "bolt": flyer_meta['store'],
                        "ujsag": flyer_meta['title'],
                        "ervenyesseg": detected_validity,
                        "nev": product.get("nev"),
                        "ar": product.get("ar"),
                        "ar_info": product.get("ar_info"),
                        "ar_info2": product.get("ar_info2"),
                        "forrasLink": flyer_meta['url']
                    }
                    results.append(record)
                    print(f"      + {record['nev']} | {record['ar']}")

            os.remove(item['image_path'])

        except Exception as e:
            print(f"⚠️ Hiba az AI feldolgozásnál: {e}")
            if os.path.exists(item['image_path']):
                os.remove(item['image_path'])

    return results


# ===============================================================================
# FŐVEZÉRLŐ (TISZTÍTÁS + BOUNCER + DEDUPLIKÁCIÓ) 🧹⛔💰
# ===============================================================================

if __name__ == "__main__":
    print("=== PROFESSZOR BOT: TOTAL CLEANUP VERZIÓ (v6.0) ===")
    print(f"📅 Mai dátum: {datetime.date.today()}")

    # 1. Friss linkek betöltése (Ez a referencia!)
    if not os.path.exists(INPUT_FILE):
        print("❌ Nincs flyers.json! Futtasd a Linkvadászt előbb.")
        exit()

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        new_flyers_data = json.load(f)
        current_flyers = new_flyers_data.get("flyers", [])

    current_active_urls = [f['url'] for f in current_flyers]
    print(f"📋 Aktív újságok linkjei (Web): {len(current_active_urls)}")

    # 2. Régi adatok betöltése
    old_products = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                old_products = json.load(f)
        except:
            old_products = []

    # 3. KÉT-LÉPCSŐS TISZTÍTÁS (RÉGI ADATOK SZŰRÉSE)
    final_products = []
    kept_count = 0
    dropped_link = 0
    dropped_date = 0

    print("♻️  Régi adatok ellenőrzése...")
    for product in old_products:
        p_link = product.get('forrasLink')
        p_date = product.get('ervenyesseg')

        # A) Link ellenőrzés: Még kint van a boltnál?
        if p_link not in current_active_urls:
            dropped_link += 1
            continue  # Töröljük, mert a bolt levette a linket

        # B) Dátum ellenőrzés: A JSON-ban tárolt dátum lejárt-e mára?
        if not check_validity_date(p_date):
            dropped_date += 1
            continue  # Töröljük, mert lejárt az ideje

        # Ha mindkettőn átment -> MEGTARTJUK
        final_products.append(product)
        kept_count += 1

    print(f"   -> Megtartva: {kept_count}")
    print(f"   -> Törölve (Hibás link): {dropped_link}")
    print(f"   -> Törölve (Lejárt dátum): {dropped_date}")

    # Jegyezzük meg, miket tartottunk meg (URL alapján), hogy ne dolgozzuk fel újra
    processed_urls_in_output = set()
    for p in final_products:
        processed_urls_in_output.add(p['forrasLink'])

    # 4. ÚJ LINKKEK FELDOLGOZÁSA (BOUNCER MÓD)
    for flyer in current_flyers:
        url = flyer['url']

        # DEDUPLIKÁCIÓ: Ha már megvan a tisztított listában -> SKIP
        if url in processed_urls_in_output:
            print(f"⏩ SKIP (Érvényes és kész): {flyer['store']} - {flyer['title']}")
            continue

            # HA ÚJ -> FELDOLGOZÁS INDUL
        print(f"\n🆕 ÚJ ÚJSÁG! Vizsgálat indul: {flyer['store']}")
        pages = capture_pages_with_selenium(url, flyer['store'])

        if pages:
            # Itt fut le a BOUNCER (process_images_with_ai).
            # Ha az AI szerint az 1. oldal dátuma lejárt, üres listát ad vissza.
            new_items = process_images_with_ai(pages, flyer)

            if new_items:
                final_products.extend(new_items)
                print(f"✅ SIKER! {len(new_items)} db termék hozzáadva.")
            else:
                print("🚫 BLOKKOLVA (Lejárt újság).")
        else:
            print("⚠️ Nem sikerült a fotózás.")

    # 5. VÉGSŐ MENTÉS
    # Itt felülírjuk a fájlt a tisztított + új listával
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_products, f, ensure_ascii=False, indent=2)


    print(f"\n🏁 KÉSZ! Végső adatbázis: {len(final_products)} termék.")
