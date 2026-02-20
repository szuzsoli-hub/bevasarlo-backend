import os
import time
import json
import re
import requests # <-- ÚJ IMPORT
import fitz # <-- ÚJ IMPORT (PyMuPDF a PDF szeleteléshez)
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

# --- ÚJ: ASSETS MAPPA KEZELÉSE ---
ASSETS_DIR = os.path.join(base_dir, "assets")
if not os.path.exists(ASSETS_DIR):
    os.makedirs(ASSETS_DIR)

# Mindkét fájlt az assets mappán belül kezeljük!
INPUT_FILE = os.path.join(ASSETS_DIR, 'flyers.json')           # A friss linkek
OUTPUT_FILE = os.path.join(ASSETS_DIR, 'universal_output.json') # A kész adatbázis
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
# 1/A. MODUL: A FOTÓS (Capture - HTML/Selenium) 📸
# ===============================================================================

def capture_pages_with_selenium(target_url, store_name):
    print(f"\n📸 FOTÓZÁS INDUL ({store_name}): {target_url}")

    chrome_options = Options()
    chrome_options.add_argument("--headless") # GitHub Actions miatt kötelező!
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    # --- MÓDOSÍTÁS: Szafari álcázás és anti-bot védelem a Spar miatt ---
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")

    captured_data = []

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # Extrém bot elrejtés JavaScripttel
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        
        driver.get(target_url)
        
        # --- MÓDOSÍTÁS: 10 másodperc univerzális betöltési idő (HD képek és éles dátumok miatt) ---
        time.sleep(10) 

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

        # --- MÓDOSÍTÁS: 4 oldalra felemelve a teszt kedvéért ---
        for i in range(4): 
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            
            # --- ÚJ MÓDOSÍTÁS: Lapozás Iframe-en belül a jobbra nyíllal ---
            if i > 0:
                try:
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                    if iframes:
                        # Ha van iframe (pl. Spar flipbook), belépünk és oda küldjük a nyilat
                        driver.switch_to.frame(iframes[0])
                        body = driver.find_element(By.TAG_NAME, 'body')
                        body.send_keys(Keys.ARROW_RIGHT)
                        driver.switch_to.default_content() # Visszalépünk a főoldalra a fotózáshoz
                    else:
                        # Sima oldal esetén marad a normál lapozás
                        body = driver.find_element(By.TAG_NAME, 'body')
                        body.send_keys(Keys.ARROW_RIGHT)
                except Exception as e:
                    print(f"⚠️ Lapozási hiba: {e}")
                
                # --- MÓDOSÍTÁS: 5 másodperc várakozás lapozás után a HD kép betöltéséhez ---
                time.sleep(5)

            # Visszatérés a biztonságos, teljes képernyős fotózáshoz
            driver.save_screenshot(fajl_nev)

            captured_data.append({
                "image_path": fajl_nev,
                "page_url": driver.current_url,
                "page_num": page_num
            })
            print(f"   -> {page_num}. oldal lefotózva. (URL: {driver.current_url})")

        return captured_data

    except Exception as e:
        print(f"❌ Hiba a fotózásnál ({store_name}): {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()


# ===============================================================================
# 1/B. MODUL: A SZELETELŐ (PDF Letöltés és darabolás) ✂️📄
# ===============================================================================

def capture_pages_from_pdf(target_url, store_name):
    print(f"\n📄 PDF LETÖLTÉS ÉS SZELETELÉS INDUL ({store_name}): {target_url}")
    captured_data = []
    temp_pdf_path = os.path.join(TEMP_DIR, f"{store_name}_temp.pdf")

    # --- MÓDOSÍTÁS: Safari álca (headers) a 403-as hiba ellen ---
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    }

    try:
        # 1. Nyers PDF fájl letöltése Safari álcával
        response = requests.get(target_url, headers=headers, timeout=30)
        response.raise_for_status()
        with open(temp_pdf_path, 'wb') as f:
            f.write(response.content)

        # 2. PDF megnyitása és darabolása (PyMuPDF)
        doc = fitz.open(temp_pdf_path)
        max_pages = min(4, len(doc)) # Maximum 4 oldal

        for i in range(max_pages):
            page_num = i + 1
            page = doc.load_page(i)
            # Kép generálása (dpi=200 a tökéletes, tűéles OCR-hez)
            pix = page.get_pixmap(dpi=200)
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            pix.save(fajl_nev)

            # --- Deep Link horgonnyal a pontos oldalhoz ---
            captured_data.append({
                "image_path": fajl_nev,
                "page_url": f"{target_url}#page={page_num}",
                "page_num": page_num
            })
            print(f"   -> {page_num}. oldal tökéletes minőségben kivágva a PDF-ből.")

        doc.close()
        return captured_data

    except Exception as e:
        print(f"❌ Hiba a PDF feldolgozásánál ({store_name}): {e}")
        return []
    finally:
        # Takarítás: A letöltött nyers PDF-et azonnal eldobjuk
        if os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
            except:
                pass


# ===============================================================================
# 2. MODUL: AZ AGY - DÁTUM ELLENŐRZÉS ÉS AI OSZTÁLYOZÁS (BOUNCER) 🧠
# ===============================================================================

def google_ocr(image_path):
    with open(image_path, "rb") as img_file: content = img_file.read()
    image = vision.Image(content=content)
    response = vision_client.document_text_detection(image=image)
    if response.error.message: return ""
    return response.full_text_annotation.text

def interpret_text_with_ai(full_text, page_num, store_name, title_name):
    # Dátum instrukció csak az első oldalon
    date_instr = "FELADAT 1: KERESD MEG AZ AKTUÁLIS ÉRVÉNYESSÉGI IDŐT (YYYY.MM.DD-YYYY.MM.DD) a szövegben! Keresd ki az összes dátumot, amit látsz!" if page_num == 1 else ""

    # --- MÓDOSÍTÁS: AI Térfél felismerő beépítése ---
    prompt = f"""
    Kaptál egy OCR szöveget a(z) {store_name} bolt "{title_name}" újságjának {page_num}. oldaláról.
    FIGYELEM: Ez a kép gyakran egy dupla oldalpárt (pl. 6-7. oldal) ábrázol!
    {date_instr}

    FELADAT 2: KATEGORIZÁLÁS (Azonosítsd az oldal fő profilját!)
    - Ha túlnyomórészt élelmiszer, ital, napi fogyasztási cikk vagy háztartási vegyi áru van rajta -> "ÉLELMISZER_VEGYES"
    - Ha tisztán ruha, barkács, bútor, elektronika, vagy imázs/álláshirdetés konkrét termék nélkül -> "NONFOOD_MARKETING"

    FELADAT 3: TERMÉKEK KIGYŰJTÉSE (Csak ha az oldal ÉLELMISZER_VEGYES!)
    Gyűjtsd ki az élelmiszer és vegyi áru termékeket JSON-be. 
    (Ha az oldal NONFOOD_MARKETING, a 'termekek' lista maradjon üresen: []).

    MEZŐK ÉS FORMÁTUMOK:
    - 'nev': Termék neve.
    - 'ar': Ár. Ez a fizetendő TELJES ár legyen (pl. a csomag ára)! KÖTELEZŐ FORMÁTUM: A szám után mindig írd oda a valutát is! (pl. "999 Ft", "229 Ft/db", vagy "4699 Ft"). SOHA ne az egységárat tedd ide!
    - 'ar_info': Kiszerelés ÉS egységár. TÖREKEDJ ERRE AZ ETALON FORMÁTUMRA: [Mennyiség], [Egységár]. Példák: "500 g, 1398 Ft/kg", vagy "40 db, 117,5 Ft/db", vagy "1.5 l, 499 Ft/l". KIVÉTEL: Ha valamelyik adat hiányzik a képről vagy olvashatatlan, NE dobd el a terméket, csak azt írd be, amit biztosan látsz!
    - 'ar_info2': Feltételek (pl. "Csak 2 db esetén", "Clubcarddal"). Ha nincs, legyen null.
    - 'oldal_terfel': Határozd meg, hogy a termék a kép BAL vagy JOBB térfelén található-e. Ha a kép csak egyetlen oldalt ábrázol, akkor legyen "bal". Értéke csak "bal" vagy "jobb" lehet.

    ELVÁRT JSON FORMAT:
    {{
      "oldal_jelleg": "ÉLELMISZER_VEGYES",
      "ervenyesseg": "2026.02.12-2026.02.18", 
      "termekek": [
        {{ "nev": "...", "ar": "999 Ft", "ar_info": "500 g, 1398 Ft/kg", "ar_info2": null, "oldal_terfel": "jobb", "kategoria_dontes": "marad" }}
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
    if not date_string: return True # Ha nincs adat, a biztonság kedvéért átengedjük (User check)
    
    try:
        # Dátum keresés (YYYY.MM.DD vagy YYYY-MM-DD)
        dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', str(date_string))
        
        if dates:
            dates.sort()
            
            # Az utolsó (legkésőbbi) dátum a lejárati idő
            end_date_str = dates[-1].replace('-', '.')
            end_date = datetime.datetime.strptime(end_date_str, "%Y.%m.%d").date()
            today = datetime.date.today()
            
            if end_date < today:
                return False # LEJÁRT
            else:
                return True # MÉG JÓ
                
    except Exception:
        pass 
        
    return True

def process_images_with_ai(captured_data, flyer_meta):
    print(f"🧠 AI Elemzés: {flyer_meta['store']} - {flyer_meta['title']}...")
    results = []
    detected_validity = flyer_meta.get('validity', "N/A")
    nonfood_count = 0

    try:
        for item in captured_data:
            full_text = google_ocr(item['image_path'])
            if not full_text: 
                continue

            # Átadjuk a bolt és újság nevet a promptnak, hogy az AI-nak ne kelljen kitalálnia
            structured = interpret_text_with_ai(full_text, item['page_num'], flyer_meta['store'], flyer_meta['title'])

            # --- 1. BOUNCER: FRISS ÚJSÁG DÁTUM ELLENŐRZÉS ---
            if item['page_num'] == 1:
                is_valid = True
                
                # A Hibrid Nyomozó (Spar Specifikus)
                if "spar" in flyer_meta['store'].lower():
                    url_date_match = re.search(r'(2[4-6])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', flyer_meta['url'])
                    # A nyers OCR szövegből (full_text) keressük a dátumokat, nem az AI-tól!
                    ocr_detected_dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', full_text)
                    
                    found_exact_match = False
                    
                    if url_date_match and len(ocr_detected_dates) >= 2:
                        # Ha van dátum az URL-ben, kinyerjük (pl. 260219 -> 2026.02.19)
                        y, m, d = url_date_match.groups()
                        expected_start = f"20{y}.{m}.{d}"
                        
                        # Megnézzük a NYERS OCR által talált dátumokat párosával (kezdet-vég)
                        for i in range(0, len(ocr_detected_dates)-1, 2):
                            start_date = ocr_detected_dates[i].replace('-', '.')
                            end_date = ocr_detected_dates[i+1].replace('-', '.')
                            
                            if start_date == expected_start:
                                detected_validity = f"{start_date}-{end_date}"
                                found_exact_match = True
                                is_valid = check_validity_date(detected_validity)
                                print(f"🎯 HIBRID NYOMOZÓ SIKER: Megvan a pontos Spar dátum: {detected_validity}")
                                break
                    
                    # A MENTŐÖV: Ha a Hibrid Nyomozó elbukott, BÍZZUNK A LINKVADÁSZBAN!
                    if not found_exact_match:
                        print("🛡️ SPAR VÉDŐHÁLÓ: Nincs biztos OCR dátum, de átengedjük a Linkvadász frissessége alapján!")
                        detected_validity = flyer_meta.get('validity', "N/A")
                        is_valid = True # Átengedjük!
                
                # Ha NEM Spar, marad a régi ellenőrzés
                else:
                    if structured.get("ervenyesseg"):
                        detected_validity = structured.get("ervenyesseg")
                        is_valid = check_validity_date(detected_validity)

                # Ha a dátum garantáltan lejárt -> KUKA
                if not is_valid:
                     print(f"⛔ BOUNCER: Ez az újság lejárt ({detected_validity}), teljes törlés! - {flyer_meta['title']}")
                     return [] # Megszakítja az AI elemzést

            # --- 2. BOUNCER: NONFOOD / MARKETING SZŰRŐ ---
            jelleg = structured.get("oldal_jelleg", "ÉLELMISZER_VEGYES")
            if jelleg == "NONFOOD_MARKETING":
                print(f"   ⏩ SKIP: A(z) {item['page_num']}. oldal '{jelleg}' besorolást kapott.")
                nonfood_count += 1
                
                # --- MÓDOSÍTÁS: 2 oldal helyett az első 3 oldal után dobja csak ki (Spar Extra miatt) ---
                if item['page_num'] == 3 and nonfood_count == 3:
                    print(f"⛔ BOUNCER: Az első 3 oldal NONFOOD. Egész újság kuka! - {flyer_meta['title']}")
                    return []
                continue # Átugorja a termékek listázását ezen az oldalon

            # --- TERMÉKEK KIMENTÉSE (Precíz Deep Linkkel és kész metaadatokkal) ---
            for product in structured.get("termekek", []):
                if product.get("kategoria_dontes") == "marad":
                    
                    # === ÚJ: OLDAL TÉRFÉL (BAL/JOBB) MATEK ===
                    terfel = product.get("oldal_terfel", "bal").lower()
                    vegleges_link = item['page_url']
                    vegleges_oldalszam = item['page_num']
                    
                    # Ha a jobb oldalon van (ÉS AZ EREDETI FORRÁS NEM PDF), a linket ÉS az oldalszámot is megnöveljük eggyel!
                    if terfel == "jobb" and not flyer_meta['url'].lower().endswith('.pdf'):
                        vegleges_link = re.sub(r'(\d+)(/?)$', lambda m: str(int(m.group(1)) + 1) + m.group(2), item['page_url'])
                        vegleges_oldalszam = item['page_num'] + 1
                    
                    record = {
                        "bolt": flyer_meta['store'],
                        "ujsag": flyer_meta['title'],
                        "oldalszam": vegleges_oldalszam,  # <--- MOST MÁR A KIÍRT SZÁM IS PONTOS LESZ!
                        "ervenyesseg": detected_validity,
                        "nev": product.get("nev"),
                        "ar": product.get("ar"),
                        "ar_info": product.get("ar_info"),
                        "ar_info2": product.get("ar_info2"),
                        "forrasLink": vegleges_link, # A Jogi védelemhez (Most már kicentizve!)
                        "alap_link": flyer_meta['url']  # A deduplikációhoz és jövőbeli csekkoláshoz
                    }
                    results.append(record)
                    print(f"      + {record['nev']} | {record['ar']} | Térfél: {terfel.upper()}")

    except Exception as e:
        print(f"⚠️ Hiba az AI feldolgozásnál: {e}")
    finally:
        # --- BIZTONSÁGI TAKARÍTÁS (SZIVÁRGÁSMENTESÍTÉS) ---
        # Ez mindenképp lefut, ha sikerült, ha hibára futott, ha a Bouncer kidobta az újságot!
        for item in captured_data:
            if os.path.exists(item['image_path']):
                try:
                    os.remove(item['image_path'])
                except Exception:
                    pass
        print(f"🧹 Takarítás: A(z) {flyer_meta['store']} átmeneti képei maradéktalanul törölve lettek.")

    return results


# ===============================================================================
# FŐVEZÉRLŐ (TISZTÍTÁS + BOUNCER + DEDUPLIKÁCIÓ) 🧹⛔💰
# ===============================================================================

if __name__ == "__main__":
    print("=== PROFESSZOR BOT: TOTAL CLEANUP VERZIÓ (v6.2 - PDF Szeletelővel) ===")
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
        # Itt az 'alap_link'-et nézzük, ha már létezik (új formátum), de támogatjuk a régit is ('forrasLink')
        p_base_link = product.get('alap_link', product.get('forrasLink'))
        p_date = product.get('ervenyesseg')
        
        # A) Link ellenőrzés: Még kint van a boltnál?
        if p_base_link not in current_active_urls:
            dropped_link += 1
            continue # Töröljük, mert a bolt levette a linket
            
        # B) Dátum ellenőrzés: A JSON-ban tárolt dátum lejárt-e mára?
        if not check_validity_date(p_date):
            dropped_date += 1
            continue # Töröljük, mert lejárt az ideje
            
        # Ha mindkettőn átment -> MEGTARTJUK
        final_products.append(product)
        kept_count += 1

    print(f"   -> Megtartva: {kept_count}")
    print(f"   -> Törölve (Hibás link): {dropped_link}")
    print(f"   -> Törölve (Lejárt dátum): {dropped_date}")
    
    # Jegyezzük meg, miket tartottunk meg (URL alapján), hogy ne dolgozzuk fel újra
    processed_urls_in_output = set()
    for p in final_products:
        p_base_link = p.get('alap_link', p.get('forrasLink'))
        processed_urls_in_output.add(p_base_link)

    # 4. ÚJ LINKKEK FELDOLGOZÁSA (BOUNCER MÓD)
    for flyer in current_flyers:
        url = flyer['url']
        
        # DEDUPLIKÁCIÓ: Ha már megvan a tisztított listában -> SKIP
        if url in processed_urls_in_output:
            print(f"⏩ SKIP (Érvényes és kész): {flyer['store']} - {flyer['title']}")
            continue 
            
        # HA ÚJ -> FELDOLGOZÁS INDUL
        print(f"\n🆕 ÚJ ÚJSÁG! Vizsgálat indul: {flyer['store']} - {flyer['title']}")

        # --- AZ ÚTVÁLASZTÓ (KAPUŐR) ---
        if url.lower().endswith('.pdf'):
            pages = capture_pages_from_pdf(url, flyer['store'])
        else:
            pages = capture_pages_with_selenium(url, flyer['store'])
        
        if pages:
            # Itt fut le a BOUNCER (process_images_with_ai).
            # Ha az AI szerint lejárt, vagy NONFOOD a katalógus, üres listát ad vissza.
            new_items = process_images_with_ai(pages, flyer)
            
            if new_items:
                final_products.extend(new_items)
                print(f"✅ SIKER! {len(new_items)} db termék hozzáadva.")
            else:
                print("🚫 BLOKKOLVA (Lejárt újság vagy teljesen Non-Food katalógus).")
        else:
            print("⚠️ Nem sikerült a fotózás.")

    # 5. VÉGSŐ MENTÉS
    # Itt felülírjuk a fájlt a tisztított + új listával
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_products, f, ensure_ascii=False, indent=2)

    print(f"\n🏁 KÉSZ! Végső adatbázis: {len(final_products)} termék.")


