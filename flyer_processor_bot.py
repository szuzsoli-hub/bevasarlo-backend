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

        # SÜTI KEZELÉS
        try:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                txt = btn.text.lower()
                if any(x in txt for x in ["elfogad", "accept", "mindent", "ok", "rendben"]):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
                    break
        except: pass
        
        try:
            driver.execute_script("document.querySelectorAll('div[class*=\"cookie\"], div[id*=\"cookie\"], #onetrust-banner-sdk').forEach(el => el.remove());")
        except: pass

        for i in range(4): 
            page_num = i + 1
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            
            if i > 0:
                try:
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                    if iframes:
                        driver.switch_to.frame(iframes[0])
                        body = driver.find_element(By.TAG_NAME, 'body')
                        body.send_keys(Keys.ARROW_RIGHT)
                        driver.switch_to.default_content()
                    else:
                        body = driver.find_element(By.TAG_NAME, 'body')
                        body.send_keys(Keys.ARROW_RIGHT)
                except Exception as e:
                    print(f"⚠️ Lapozási hiba: {e}")
                
                time.sleep(6)

            driver.save_screenshot(fajl_nev)
            current_live_url = driver.current_url

            captured_data.append({
                "image_path": fajl_nev,
                "page_url": current_live_url,
                "page_num": page_num
            })
            print(f"   -> {page_num}. oldal lefotózva. (URL: {current_live_url})")

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
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"}

    try:
        response = requests.get(target_url, headers=headers, timeout=30)
        response.raise_for_status()
        with open(temp_pdf_path, 'wb') as f:
            f.write(response.content)

        doc = fitz.open(temp_pdf_path)
        max_pages = min(4, len(doc))

        for i in range(max_pages):
            page_num = i + 1
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=200)
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            pix.save(fajl_nev)

            captured_data.append({
                "image_path": fajl_nev,
                "page_url": f"{target_url}#page={page_num}",
                "page_num": page_num
            })
            print(f"   -> {page_num}. oldal kivágva a PDF-ből.")

        doc.close()
        return captured_data
    except Exception as e:
        print(f"❌ Hiba a PDF feldolgozásánál: {e}")
        return []
    finally:
        if os.path.exists(temp_pdf_path): os.remove(temp_pdf_path)

# ===============================================================================
# 2. MODUL: AZ AGY - DÁTUM ELLENŐRZÉS ÉS AI OSZTÁLYOZÁS 🧠
# ===============================================================================

def google_ocr(image_path):
    with open(image_path, "rb") as img_file: content = img_file.read()
    image = vision.Image(content=content)
    response = vision_client.document_text_detection(image=image)
    if response.error.message: return ""
    return response.full_text_annotation.text

def interpret_text_with_ai(full_text, page_num, store_name, title_name, link_hint):
    date_instr = ""
    if page_num == 1:
        date_instr = f"""
        FELADAT 1: DÁTUM KERESÉS
        Súgás a linkből: {link_hint}
        Keresd az érvényességi időt! Kulcsszavak: Érvényes, Ajánlatunk, Időtartam, csütörtöktől, szerdáig, vasárnapig, heti, hét.
        Ha találsz konkrét dátumot (pl. 02.19 - 25.), másold ki pontosan! Ha csak heti számot látsz (pl. 8. hét), azt is írd be!
        """

    prompt = f"""
    Kaptál egy OCR szöveget a(z) {store_name} bolt "{title_name}" újságjának {page_num}. oldaláról.
    {date_instr}

    FELADAT 2: KATEGORIZÁLÁS ("ÉLELMISZER_VEGYES" vagy "NONFOOD_MARKETING")
    FELADAT 3: TERMÉKEK KIGYŰJTÉSE (Csak ha ÉLELMISZER_VEGYES)

    ELVÁRT JSON FORMÁTUM KÖTELEZŐEN:
    {{
      "oldal_jelleg": "ÉLELMISZER_VEGYES",
      "ervenyesseg": "Ha a súgás nem 'N/A', akkor PONTOSAN ezt a súgást használd: {link_hint}. VISZONT ha a súgás 'N/A', akkor ez a FALLBACK: KÖTELEZŐEN az OCR szövegből olvasd le a pontos érvényességi időt (keresd az 'Érvényes', 'Időtartam' szavakat)!",
      "termekek": [
        {{
          "nev": "Termék neve",
          "ar": "Ár valutával",
          "ar_info": "Kiszerelés és egységár",
          "ar_info2": "Feltételek vagy null"
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

def check_validity_date(date_string):
    if not date_string or "hét" in str(date_string).lower(): return True
    try:
        dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', str(date_string))
        if dates:
            dates.sort()
            end_date_str = dates[-1].replace('-', '.')
            end_date = datetime.datetime.strptime(end_date_str, "%Y.%m.%d").date()
            # Csak akkor dobjuk ki, ha biztosan elmúlt a végdátum
            if end_date < datetime.date.today(): return False
    except: pass
    return True

def process_images_with_ai(captured_data, flyer_meta):
    print(f"🧠 AI Elemzés: {flyer_meta['store']}...")
    results = []
    
    # 1. LINK-FIRST LOGIKA: Adatkinyerés az URL-ből súgásként
    link_hint = flyer_meta.get('validity', "N/A")
    url = flyer_meta['url']
    
    # 1. Próba: Auchan hónapváltós (pl. 2026-02-26-03-04)
    auchan_cross_month = re.search(r'(202[4-6])[-_](\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})', url)
    
    # 2. Próba: Auchan azonos hónap (pl. 2026-02-19-25)
    # A (?!\d) a végén biztosítja, hogy ne keverje össze a hónapváltóssal!
    auchan_same_month = re.search(r'(202[4-6])[-_](\d{2})[-_](\d{2})[-_](\d{2})(?!\d)', url)
    
    # 3. Próba: Normál vagy SPAR-féle rövid dátum (pl. 2026-02-19 vagy 260219)
    single_date = re.search(r'(202[4-6]|2[4-6])[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])', url)
    
    if auchan_cross_month:
        y, m1, d1, m2, d2 = auchan_cross_month.groups()
        link_hint = f"{y}.{m1}.{d1}. - {m2}.{d2}."
    elif auchan_same_month:
        y, m, d1, d2 = auchan_same_month.groups()
        link_hint = f"{y}.{m}.{d1}. - {m}.{d2}."
    elif single_date:
        y_str, m_str, d_str = single_date.groups()
        year = y_str if len(y_str) == 4 else f"20{y_str}"
        link_hint = f"{year}.{m_str}.{d_str}."
    elif "heti" in url or "het" in url:
        week_match = re.search(r'(\d{1,2})[-_]het', url)
        if week_match:
            link_hint = f"{week_match.group(1)}. hét"

    detected_validity = link_hint
    nonfood_count = 0

    try:
        for item in captured_data:
            full_text = google_ocr(item['image_path'])
            if not full_text: continue

            structured = interpret_text_with_ai(full_text, item['page_num'], flyer_meta['store'], flyer_meta['title'], link_hint)

            if item['page_num'] == 1:
                if structured.get("ervenyesseg") and structured.get("ervenyesseg") != "N/A":
                    detected_validity = structured.get("ervenyesseg")
                
                # Szelíd Bouncer: Ha nem tudjuk biztosan hogy lejárt, megtartjuk!
                if not check_validity_date(detected_validity):
                    print(f"⛔ LEJÁRT: {detected_validity}")
                    return []

            jelleg = structured.get("oldal_jelleg", "ÉLELMISZER_VEGYES")
            if jelleg == "NONFOOD_MARKETING":
                nonfood_count += 1
                if item['page_num'] == 3 and nonfood_count == 3: return []
                continue

            for product in structured.get("termekek", []):
                # Kivettük a kategoria_dontes feltételt, mindent mentünk, amit az AI terméknek talált!
                record = {
                    "bolt": flyer_meta['store'],
                    "ujsag": flyer_meta['title'],
                    "oldalszam": item['page_num'],
                    "ervenyesseg": detected_validity,
                    "nev": product.get("nev"),
                    "ar": product.get("ar"),
                    "ar_info": product.get("ar_info"),
                    "ar_info2": product.get("ar_info2"),
                    "forrasLink": item['page_url'],
                    "alap_link": flyer_meta['url']
                }
                results.append(record)
                print(f"      + {record['nev']} | {record['ar']}")

    except Exception as e:
        print(f"⚠️ Hiba az AI feldolgozásnál: {e}")
    finally:
        for item in captured_data:
            if os.path.exists(item['image_path']): os.remove(item['image_path'])
        print(f"🧹 Takarítás kész.")

    return results

if __name__ == "__main__":
    print("=== PROFESSZOR BOT: ROBUST DÁTUMKEZELŐ VERZIÓ ===")
    
    if not os.path.exists(INPUT_FILE):
        print("❌ Nincs flyers.json!")
        exit()
    
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        new_flyers_data = json.load(f)
        current_flyers = new_flyers_data.get("flyers", [])
        
    current_active_urls = [f['url'] for f in current_flyers]
    old_products = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                old_products = json.load(f)
        except: pass

    final_products = []
    processed_urls_in_output = set()
    
    for product in old_products:
        p_base_link = product.get('alap_link', product.get('forrasLink'))
        if p_base_link in current_active_urls and check_validity_date(product.get('ervenyesseg')):
            final_products.append(product)
            processed_urls_in_output.add(p_base_link)

    for flyer in current_flyers:
        url = flyer['url']
        if url in processed_urls_in_output: continue 
        
        print(f"\n🆕 ÚJ ÚJSÁG: {flyer['store']} - {flyer['title']}")
        if url.lower().endswith('.pdf'):
            pages = capture_pages_from_pdf(url, flyer['store'])
        else:
            pages = capture_pages_with_selenium(url, flyer['store'])
        
        if pages:
            new_items = process_images_with_ai(pages, flyer)
            if new_items: final_products.extend(new_items)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_products, f, ensure_ascii=False, indent=2)

    print(f"\n🏁 KÉSZ! Adatbázis: {len(final_products)} termék.")




