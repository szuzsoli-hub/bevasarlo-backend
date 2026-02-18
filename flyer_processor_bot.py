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

INPUT_FILE = 'assets/flyers.json'
OUTPUT_FILE = 'assets/universal_output.json'

base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, ".env"))

# Google Kulcs Kezelés (Felhő kompatibilis)
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
# 1. MODUL: COOP NÉV JAVÍTÓ (Amit kértél) 🛠️
# ===============================================================================

def get_refined_store_name(store_base, url, title):
    """
    A link és a cím alapján kitalálja a PONTOS hálózatnevet.
    """
    s = store_base.lower()
    u = url.lower()
    t = title.lower() if title else ""

    # --- COOP DETEKTÍV ---
    if "coop" in s:
        if "mecsek" in u or "mecsek" in t: return "Coop Mecsek Füszért"
        if "tisza" in u or "tisza" in t or "szolnok" in u: return "Tisza-Coop"
        if "alfold" in u or "alföld" in t or "kecskemét" in t: return "Alföld Pro-Coop"
        if "hetforras" in u or "hétforrás" in t or "szombathely" in t: return "Hétforrás"
        if "eszak-kelet" in u or "észak" in t or "miskolc" in t or "debrecen" in t: return "Észak-Kelet Pro-Coop"
        if "honi" in u or "honi" in t: return "Honi-Coop"
        if "polus" in u or "pólus" in t: return "Pólus-Coop"
        return store_base

    # --- CBA / PRÍMA DETEKTÍV ---
    if "cba" in s or "príma" in s or "prima" in s:
        if "prima" in u or "príma" in t or "prima" in s:
            return "CBA Príma"
        return "CBA"

    return store_base


# ===============================================================================
# 2. MODUL: A FOTÓS 📸
# ===============================================================================

def capture_pages_with_selenium(target_url, store_name):
    print(f"\n📸 2. LÉPÉS: Fotózás indul ({store_name}): {target_url}")

    chrome_options = Options()
    chrome_options.add_argument("--headless")  # FELHŐ MIATT KÖTELEZŐ!
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
    # ITT A JAVÍTÁS: COOP NÉV PONTOSÍTÁS
    refined_name = get_refined_store_name(flyer_meta['store'], flyer_meta['url'], flyer_meta.get('title', ''))
    
    print(f"\n🧠 AI Feldolgozás: {refined_name}...")
    results = []
    detected_validity = flyer_meta.get('validity', "Keresés...")

    for item in captured_data:
        try:
            full_text = google_ocr(item['image_path'])
            if not re.search(r"\d", full_text): continue

            structured = interpret_text_with_ai(full_text, item['page_num'], refined_name)

            if item['page_num'] == 1 and structured.get("ervenyesseg"):
                raw_val = structured.get("ervenyesseg")
                if len(raw_val) > 5:
                    detected_validity = raw_val
                    print(f"📅 DETEKTÁLT DÁTUM: {detected_validity}")

            for product in structured.get("termekek", []):
                if product.get("kategoria_dontes") != "marad": continue
                if not re.search(r"\d", product.get("ar", "")): continue

                record = {
                    "bolt": refined_name, # JAVÍTOTT NÉV HASZNÁLATA
                    "ujsag": flyer_meta.get('title', f"{refined_name} Akciós Újság"),
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
# FŐVEZÉRLŐ (ASSET OLVASÓ MÓD)
# ===============================================================================

if __name__ == "__main__":
    print("=== BEVÁSÁRLÓ ROBOT: PROCESSOR MÓD ===")

    # 1. Bemenet olvasása
    if not os.path.exists(INPUT_FILE):
        print(f"❌ Nincs bemeneti fájl: {INPUT_FILE}")
        exit()

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        flyers = data.get("flyers", [])
    
    print(f"📋 Feldolgozandó újságok száma: {len(flyers)}")

    # 2. Meglévő adatok betöltése (opcionális, ha hozzáírni akarsz)
    all_products = []
    # Ha szeretnéd megtartani a régieket, itt be lehet tölteni, de most tiszta lappal indulunk a kérésed szerint.

    # 3. Feldolgozás
    for flyer in flyers:
        print(f"\n------------------------------------------------")
        print(f"🚀 Feldolgozás indul: {flyer['store']}")

        pages = capture_pages_with_selenium(flyer['url'], flyer['store'])

        if pages:
            store_results = process_images_with_ai(pages, flyer)
            all_products.extend(store_results)

            for p in pages:
                try:
                    os.remove(p['image_path'])
                except:
                    pass
        else:
            print(f"⚠️ Nem sikerült fotózni: {flyer['store']}")

    # 4. Mentés
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_products, f, ensure_ascii=False, indent=2)

    print(f"\n🏁 KÉSZ! Összesen {len(all_products)} termék mentve: {OUTPUT_FILE}")
