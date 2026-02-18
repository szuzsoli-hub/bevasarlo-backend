import os
import time
import json
import re
import requests
from bs4 import BeautifulSoup
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
from curl_cffi import requests as cffi_requests

# ==============================
# 0. KONFIGURÁCIÓ & ENV
# ==============================

# ITT A MÓDOSÍTÁS: Az assets mappába dolgozunk!
INPUT_FILE = 'assets/flyers.json'
OUTPUT_FILE = 'assets/universal_output.json'

base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, ".env"))

# Google Kulcs Kezelés (Felhő kompatibilis)
if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    # Ha nincs beállítva, feltételezzük, hogy a fájl a gyökérben van (GitHub Actions generálja)
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
# 1. MODUL: A PROFI FELDERÍTŐK (Hagyjuk a kódban, de most nem hívjuk meg) 🕵️‍♂️
# ===============================================================================

def scan_rest_stores():
    # Ezt a részt most nem használjuk, mert a flyers.json-ból dolgozunk
    found_flyers = []
    # ... (A kód marad érintetlen, de inaktív)
    return found_flyers


# ===============================================================================
# 2. MODUL: A FOTÓS 📸
# ===============================================================================

def capture_pages_with_selenium(target_url, store_name):
    print(f"\n📸 2. LÉPÉS: Fotózás indul ({store_name}): {target_url}")

    chrome_options = Options()
    chrome_options.add_argument("--headless") # Felhőben kötelező!
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    captured_data = []

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.get(target_url)
        time.sleep(10)

        # --- 1. METRO: TABULÁTOR ---
        if store_name == "Metro":
            print("⌨️ METRO: Tabulátoros kuki-gyilkos...")
            actions = ActionChains(driver)
            try:
                driver.find_element(By.TAG_NAME, "body").click()
            except:
                pass
            hit = False
            for i in range(40):
                actions.send_keys(Keys.TAB).perform()
                time.sleep(0.1)
                try:
                    active = driver.switch_to.active_element
                    txt = active.text.lower()
                    if "rendben" in txt or "elfogad" in txt or "hozzájárulok" in txt or "allow" in txt:
                        active.send_keys(Keys.ENTER)
                        print(f"✅ METRO Kuki kilőve: {txt}")
                        hit = True
                        time.sleep(3)
                        break
                except:
                    pass
            if not hit:
                actions.send_keys(Keys.ENTER).perform()
                time.sleep(2)

        # --- 2. CBA (MINDEN TÍPUS): KUKI KILLER ---
        elif "CBA" in store_name:
            print(f"... {store_name} Kuki keresése ...")
            try:
                gombok = driver.find_elements(By.TAG_NAME, "button")
                clicked = False
                for gomb in gombok:
                    txt = gomb.text.lower()
                    if "összes" in txt and "elfogad" in txt:
                        gomb.click()
                        print(f"✅ {store_name} Kuki gomb megnyomva.")
                        clicked = True
                        time.sleep(2)
                        break
                if not clicked:
                    driver.execute_script("""
                        var divs = document.querySelectorAll('div');
                        for (var i = 0; i < divs.length; i++) {
                            var style = window.getComputedStyle(divs[i]);
                            if (style.position === 'fixed' && style.top === '0px' && parseInt(style.zIndex) > 10) {
                                divs[i].remove();
                            }
                        }
                    """)
            except:
                pass

        # --- 3. EGYÉB (Spar, Tesco) ---
        else:
            try:
                if store_name == "Spar":
                    try:
                        gombok = driver.find_elements(By.TAG_NAME, "button")
                        for gomb in gombok:
                            if "elfogad" in gomb.text.lower() or "accept" in gomb.text.lower():
                                gomb.click()
                                break
                    except:
                        pass

                driver.execute_script("""
                    var elements = document.querySelectorAll('div, section, footer, header, aside, span, p');
                    for (var i = 0; i < elements.length; i++) {
                        var el = elements[i];
                        var style = window.getComputedStyle(el);
                        if ((style.position === 'fixed' || style.position === 'absolute') && parseInt(style.zIndex) > 50) {
                            if (!el.className.includes('nav') && !el.className.includes('menu')) el.remove();
                        }
                    }
                    document.body.style.overflow = 'auto'; 
                """)
                time.sleep(2)
            except:
                pass

        # FOTÓZÁS
        for i in range(3):
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            driver.save_screenshot(fajl_nev)

            print(f"   -> {store_name} {page_num}. oldal mentve.")

            captured_data.append({
                "image_path": fajl_nev,
                "page_url": driver.current_url,
                "page_num": page_num
            })

            try:
                body = driver.find_element(By.TAG_NAME, 'body')
                body.send_keys(Keys.ARROW_RIGHT)
                time.sleep(4)
            except:
                break

        return captured_data

    except Exception as e:
        print(f"❌ Hiba ({store_name}): {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()


# ===============================================================================
# 3. MODUL: AZ AGY 🧠
# ===============================================================================

def google_ocr(image_path):
    with open(image_path, "rb") as img_file: content = img_file.read()
    image = vision.Image(content=content)
    response = vision_client.document_text_detection(image=image)
    if response.error.message: return ""
    return response.full_text_annotation.text


def interpret_text_with_ai(full_text, page_num, store_name):
    date_instruction = ""
    if page_num == 1:
        date_instruction = "FELADAT 1: KERESD MEG AZ ÉRVÉNYESSÉGI IDŐT (YYYY.MM.DD-YYYY.MM.DD) a címlapon!"

    prompt = f"""
    Ez a(z) {store_name} akciós újság {page_num}. oldala.
    {date_instruction}

    FELADAT 2: Keresd ki a termékeket.
    SZIGORÚ SZABÁLYOK:
    1. 'nev': Csak a termék neve (pl. "Kígyóuborka").
    2. 'ar': Legkedvezőbb ár (pl. "549 Ft").

    3. 'ar_info': Kiszerelés ÉS EGYSÉGÁR (FONTOS!)
       - KÖTELEZŐ MEGKERESNI AZ EGYSÉGÁRAT! (pl. "Ft/kg", "Ft/l", "Ft/db").
       - Formátum: "[Súly/Darab] / [Egységár]"
       - Példa: "1 kg / 1299 Ft/kg" vagy "125 g / 3500 Ft/kg".

    4. KÜLÖNLEGES ESETEK:
       - Ha az ár feltételhez kötött (pl. Clubcard), 'ar_info2': "Részletes feltételek az újságban!".

    JSON FORMAT:
    {{
      "ervenyesseg": "2026.02.12-2026.02.18",
      "termekek": [
        {{
          "nev": "Termék neve",
          "ar": "1299 Ft",
          "ar_info": "1 kg / 1299 Ft/kg",
          "ar_info2": null,
          "kategoria_dontes": "marad"
        }}
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


def process_images_with_ai(captured_data, flyer_meta):
    print(f"\n🧠 AI Feldolgozás: {flyer_meta['store']}...")
    results = []
    # Ha nincs a linkben érvényesség, akkor "Keresés..."
    detected_validity = flyer_meta.get('validity', "Keresés...")

    for item in captured_data:
        try:
            full_text = google_ocr(item['image_path'])
            if not re.search(r"\d", full_text): continue

            structured = interpret_text_with_ai(full_text, item['page_num'], flyer_meta['store'])

            if item['page_num'] == 1 and structured.get("ervenyesseg"):
                raw_val = structured.get("ervenyesseg")
                if len(raw_val) > 5:
                    detected_validity = raw_val
                    print(f"📅 DETEKTÁLT DÁTUM: {detected_validity}")

            for product in structured.get("termekek", []):
                if product.get("kategoria_dontes") != "marad": continue
                if not re.search(r"\d", product.get("ar", "")): continue

                record = {
                    "bolt": flyer_meta['store'],
                    "ujsag": flyer_meta.get('title', f"{flyer_meta['store']} Akciós Újság"),
                    "ervenyesseg": detected_validity,
                    "nev": product.get("nev"),
                    "ar": product.get("ar"),
                    "ar_info": product.get("ar_info", ""),
                    "ar_info2": product.get("ar_info2"),
                    "oldalszam": item['page_num'],
                    "forrasLink": item['page_url']
                }
                results.append(record)
                warn = "⚠️" if record['ar_info2'] else ""
                print(f"      + {record['nev']} | {record['ar']} | {record['ar_info']} {warn}")

        except Exception as e:
            print(f"⚠️ Hiba: {e}")

    return results


# ===============================================================================
# FŐVEZÉRLŐ (FELHŐBARÁT & ASSET OLVASÓ) ☁️📂
# ===============================================================================

if __name__ == "__main__":
    print("=== FLYER PROCESSZOR BOT (v11.0 - Asset Reader) ===")

    # 1. Beolvassuk a már létező flyers.json-t az asset mappából
    if not os.path.exists(INPUT_FILE):
        print(f"❌ HIBA: Nem találom a bemeneti fájlt: {INPUT_FILE}")
        print("   -> Futtasd előbb a Linkvadászt!")
        exit()

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        flyers = data.get("flyers", [])

    print(f"📋 Betöltve {len(flyers)} db újság link az {INPUT_FILE}-ból.")

    all_products = []

    # 2. Végigmegyünk a listán és feldolgozzuk
    for flyer in flyers:
        print(f"\n------------------------------------------------")
        print(f"🚀 Feldolgozás indul: {flyer['store']}")

        pages = capture_pages_with_selenium(flyer['url'], flyer['store'])

        if pages:
            store_results = process_images_with_ai(pages, flyer)
            all_products.extend(store_results)

            # Takarítás
            for p in pages:
                try:
                    if os.path.exists(p['image_path']):
                        os.remove(p['image_path'])
                except:
                    pass
        else:
            print(f"⚠️ Nem sikerült fotózni: {flyer['store']}")

    # 3. Mentés az assets mappába
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_products, f, ensure_ascii=False, indent=2)

    print(f"\n🏁 KÉSZ! Összesen {len(all_products)} termék mentve ide: {OUTPUT_FILE}")
