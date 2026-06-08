import os
import time
import json
import re
import base64
import requests
import fitz
from dotenv import load_dotenv
from openai import OpenAI
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

openai_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai_key)

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
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(target_url, headers=headers, timeout=30)
        response.raise_for_status()
        
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
# 1/B. MODUL: AUCHAN & SPAR DÁTUM ELŐTÖLTÉS (ÚJ)
# ===============================================================================

def get_auchan_pre_dates(links):
    results = {}
    for url in links:
        match = re.search(r'(\d{4})-(\d{2})-(\d{2})-((?:\d{2}-)?\d{2})', url)
        if match:
            y, m1, d1, end_part = match.groups()
            start_date = f"{y}.{m1}.{d1}."
            if "-" in end_part:
                m2, d2 = end_part.split("-")
                end_date = f"{y}.{m2}.{d2}."
            else:
                end_date = f"{y}.{m1}.{end_part}."
            results[url] = f"{start_date} - {end_date}"
        else:
            results[url] = "N/A"
    return results

def get_spar_pre_dates(links):
    if not links: return {}
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    screenshot_path = os.path.join(TEMP_DIR, "spar_ajanlatok_teszt.png")
    
    try:
        driver.get("https://www.spar.hu/ajanlatok")
        time.sleep(5)
        try: driver.execute_script("document.querySelectorAll('div[class*=\"cookie\"], #onetrust-banner-sdk').forEach(el => el.remove());")
        except: pass
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(3)
        height = driver.execute_script("return Math.max(document.body.scrollHeight, 4000);")
        driver.set_window_size(1920, height)
        driver.save_screenshot(screenshot_path)
    finally:
        driver.quit()

    with open(screenshot_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    prompt = f"""
    FELADAT: Bevásárló apphoz kell érvényességi időket párosítani.
    A LINKEK TITKA:
    A linkek vége így néz ki: ÉÉHHNN-[sorszám]-[típus].
    Például: ".../260219-1-spar-szorolap" -> Ebből a 260219 azt jelenti, hogy a kezdődátum 02.19., az újság típusa pedig SPAR.
    Minden linknél olvasd ki a kezdődátumot és a típust, majd keresd meg a képen azt a szekciót, ahol ez a típus és ez a kezdődátum szerepel egymás mellett (pl. "INTERSPAR 02.26 - 03.04"). 
    Ha megvan, állítsd össze a teljes tól-ig dátumot! Az évet pótold ki 2026-ra.
    KÖTELEZŐ VÁLASZ FORMÁTUM: "ÉÉÉÉ.HH.NN. - ÉÉÉÉ.HH.NN."
    LINKEK:
    {json.dumps(links, indent=2)}
    ELVÁRT VÁLASZ (csak JSON, pontosan a megadott linkekkel mint kulcs):
    """

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_data}"}
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    )
    time.sleep(1)  # Rate limit védelem
    content = response.choices[0].message.content
    if not content:
        print("⚠️ Spar pre-dates: üres GPT válasz, kihagyva")
        return {}
    return json.loads(content)

# ===============================================================================
# 2. MODUL: AZ AGY - DÁTUM ELLENŐRZÉS ÉS AI 🧠
# ===============================================================================

def interpret_image_with_ai(image_path, page_num, store_name, title_name, link_hint, pre_calc_date=None):
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    date_instr = ""
    if page_num == 1:
        if pre_calc_date and pre_calc_date != "N/A":
            date_instr = f"""
            FIGYELEM: A dátumot MÁR TUDJUK! NE keress érvényességi időt a képen!
            KÖTELEZŐEN ezt az értéket írd be az "ervenyesseg" JSON mezőbe pontosan így: {pre_calc_date}
            A feladatod kizárólag a termékek kigyűjtése.
            """
        else:
            date_instr = f"""
            FELADAT: DÁTUM KERESÉS ÉS SZIGORÚ FORMÁZÁS
            1. NYOMOZÁS: Keresd meg a képen az érvényességi időt (lehet betűvel, számokkal, kusza elrendezésben is).
            2. SZIGORÚ FORDÍTÁS (KÖTELEZŐ!): A megtalált dátumot formázd át erre a kőbe vésett formátumra: "ÉÉÉÉ.HH.NN. - ÉÉÉÉ.HH.NN."
            3. TISZTÍTÁS: Töröld a napok neveit (csütörtök, szerda) és a felesleges szavakat (-ig). A hónapokat (pl. február) alakítsd számmá (02)!
            4. ÉVSZÁM: Ha hiányzik az év, írd elé: 2026.
            5. TESCO SZABÁLY: KIZÁRÓLAG a Tesco újságoknál hagyd figyelmen kívül a pontgyűjtők vagy nyereményjátékok távoli dátumait (pl. 04.06)! MÁS boltoknál (pl. Auchan) a távoli dátumok ÉRVÉNYESEK, azokat tartsd meg! Ha csak kezdődátum van: "ÉÉÉÉ.HH.NN.-tól".
            6. SPAR SZABÁLY: A Spar újságoknál a dátum gyakran hosszú, mondatszerű (pl. "02. 19. csütörtöktől 02. 25. szerdáig"). Keresd ki belőle a két dátumot, és formázd tiszta intervallummá! Ne add fel, és ne adj vissza N/A-t, ha van szöveges dátum!
            7. VÉGSŐ ESET (FALLBACK): Ha a képen abszolút nincs semmi dátum, add vissza ezt: {link_hint}
            """

    prompt = f"""
    Ez egy magyar akciós újság oldala. Bolt: {store_name} - {title_name}, {page_num}. oldal.
    {date_instr}
    SZABÁLYOK:
    - Csak azokat a termékeket add vissza ahol BIZTOSAN látod az árat
    - NE találj ki semmit, csak amit pontosan látsz
    - Az "ar" mezőbe csak számot írj (Ft jel és szöveg nélkül)
    - Ha feltételes az ár (pl. "24 db esetén"), azt az ar_info mezőbe írd
    - Ha van normál ár és kedvezményes ár is, a kedvezményes kerül az "ar"-ba
    - Ha nincs feltétel → ar_info legyen null
    - Ha nincs egységár → ar_info2 legyen null

    ELVÁRT JSON:
    {{
      "oldal_jelleg": "ÉLELMISZER_VEGYES",
      "ervenyesseg": "Dátum vagy N/A",
      "termekek": [
        {{
          "nev": "pontos terméknév",
          "ar": "akciós ár csak számként",
          "ar_info": "feltétel vagy mennyiség vagy null",
          "ar_info2": "normál ár vagy egységár vagy null"
        }}
      ]
    }}
    """

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_data}"}
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    )
    time.sleep(1)  # Rate limit védelem
    return json.loads(response.choices[0].message.content)

def check_validity_date(date_string, current_flyer_meta, all_flyers):
    if not date_string or date_string == "N/A": 
        return True 
        
    today = datetime.date.today()
    dates = []
    
    matches = re.findall(r'(\d{4}[\.\-]\d{2}[\.\-]\d{2})|(\d{2}[\.\-]\d{2})', str(date_string))
    for m in matches:
        d_str = m[0] or m[1]
        d_str = d_str.replace('-', '.')
        try:
            if len(d_str) > 5:
                d = datetime.datetime.strptime(d_str, "%Y.%m.%d").date()
            else:
                d = datetime.date(today.year, int(d_str[:2]), int(d_str[3:]))
            dates.append(d)
        except: pass
        
    if not dates:
        return True

    dates.sort()
    start_date = dates[0]

    if len(dates) >= 2:
        end_date = dates[-1]
        return today <= end_date  

    current_store = current_flyer_meta['store']
    current_url = current_flyer_meta['url']

    for flyer in all_flyers:
        if flyer['store'] == current_store and flyer['url'] != current_url:
            d_match = re.search(r'(202[4-6]|2[4-6])[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])', flyer['url'])
            if d_match:
                y, m, d = d_match.groups()
                y = int(y) if len(y) == 4 else int(f"20{y}")
                other_start = datetime.date(y, int(m), int(d))
                
                if other_start > start_date:
                    if today >= other_start:
                        return False 
                        
    return True

def process_images_with_ai(captured_data, flyer_meta, all_flyers, pre_calc_date=None):
    print(f"🧠 AI Elemzés: {flyer_meta['store']}...")
    results = []
    
    link_hint = "N/A"
    url = flyer_meta['url']
    d_match = re.search(r'(202[4-6]|2[4-6])[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])', url)
    if d_match:
        y, m, d = d_match.groups()
        link_hint = f"{y if len(y)==4 else '20'+y}.{m}.{d}."

    detected_validity = "N/A"
    
    for item in captured_data:
        structured = interpret_image_with_ai(item['image_path'], item['page_num'], flyer_meta['store'], flyer_meta['title'], link_hint, pre_calc_date)

        if item['page_num'] == 1:
            if pre_calc_date and pre_calc_date != "N/A":
                detected_validity = pre_calc_date
            else:
                detected_validity = structured.get("ervenyesseg", "N/A")

            if not check_validity_date(detected_validity, flyer_meta, all_flyers):
                print(f"⛔ LEJÁRT: {detected_validity}")
                return []

        termekek = structured.get("termekek", [])
        if termekek:
            for product in termekek:
                ar_val = str(product.get("ar", "")).strip()
                if ar_val and re.match(r'^[\d\s\.,]+$', ar_val):
                    ar_val = f"{ar_val} Ft"
                results.append({
                    "bolt": flyer_meta['store'], "ujsag": flyer_meta['title'], "oldalszam": item['page_num'],
                    "ervenyesseg": detected_validity, "nev": product.get("nev"), "ar": ar_val,
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

    auchan_links = [f['url'] for f in current_flyers if f['store'].lower() == 'auchan']
    spar_links = [f['url'] for f in current_flyers if f['store'].lower() == 'spar']
    
    pre_fetched_dates = {}
    if auchan_links:
        print("\n🛒 AUCHAN ÉRVÉNYESSÉGEK ELŐTÖLTÉSE...")
        pre_fetched_dates.update(get_auchan_pre_dates(auchan_links))
    if spar_links:
        print("\n🍏 SPAR ÉRVÉNYESSÉGEK ELŐTÖLTÉSE...")
        pre_fetched_dates.update(get_spar_pre_dates(spar_links))

    def get_start_date(validity_str):
        match = re.search(r'(\d{4}[\.\-]\d{2}[\.\-]\d{2})|(\d{2}[\.\-]\d{2})', str(validity_str))
        if not match: return datetime.date(2000,1,1)
        try:
            d_str = match.group(0).replace('-', '.')
            if len(d_str) > 5: return datetime.datetime.strptime(d_str, "%Y.%m.%d").date()
            return datetime.date(2026, int(d_str[:2]), int(d_str[3:]))
        except: return datetime.date(2000,1,1)

    active_urls = [f['url'] for f in current_flyers]
    final_products = []
    processed_urls = set()

    for product in old_products:
        url = product.get('alap_link')
        matching_flyer = next((f for f in current_flyers if f['url'] == url), None)
        
        if matching_flyer and check_validity_date(product.get('ervenyesseg'), matching_flyer, current_flyers):
            final_products.append(product)
            processed_urls.add(url)

    for flyer in current_flyers:
        if flyer['url'] in processed_urls: continue
        
        pre_calc_date = pre_fetched_dates.get(flyer['url'])

        pages = capture_pages_from_pdf(flyer['url'], flyer['store']) if flyer['url'].lower().endswith('.pdf') else capture_pages_with_selenium(flyer['url'], flyer['store'])
        if pages:
            new_items = process_images_with_ai(pages, flyer, current_flyers, pre_calc_date)
            final_products.extend(new_items)

    def get_sub_store(store, url):
        u_lower = url.lower()
        if store.lower() == "tesco":
            if "hipermarket" in u_lower: return "tesco_hiper"
            if "szupermarket" in u_lower: return "tesco_szuper"
        if store.lower() == "spar":
            if "interspar" in u_lower: return "spar_inter"
            if "spar-extra" in u_lower: return "spar_extra"
            if "spar-market" in u_lower: return "spar_market"
            return "spar_sima"
        return store

    sub_store_dates = {}
    for f in current_flyers:
        s_key = get_sub_store(f['store'], f['url'])
        d_match = re.search(r'(202[4-6]|2[4-6])[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])', f['url'])
        if d_match:
            y, m, d = d_match.groups()
            y = int(y) if len(y) == 4 else int(f"20{y}")
            st_date = datetime.date(y, int(m), int(d))
            if s_key not in sub_store_dates:
                sub_store_dates[s_key] = []
            sub_store_dates[s_key].append(st_date)

    for p in final_products:
        erv = str(p.get("ervenyesseg", ""))
        if "-tól" in erv or "-tol" in erv:
            match = re.search(r'(\d{4}[\.\-]\d{2}[\.\-]\d{2})', erv)
            if match:
                d_str = match.group(1).replace('-', '.')
                try:
                    p_start = datetime.datetime.strptime(d_str, "%Y.%m.%d").date()
                    s_key = get_sub_store(p.get("bolt", ""), p.get("alap_link", ""))
                    
                    next_dates = [d for d in sub_store_dates.get(s_key, []) if d > p_start]
                    if next_dates:
                        next_dates.sort()
                        end_date = next_dates[0] - datetime.timedelta(days=1)
                        p["ervenyesseg"] = f"{p_start.strftime('%Y.%m.%d.')} - {end_date.strftime('%Y.%m.%d.')}"
                except:
                    pass

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f: json.dump(final_products, f, ensure_ascii=False, indent=2)
    print(f"\n🏁 KÉSZ! Adatbázis: {len(final_products)} termék.")
