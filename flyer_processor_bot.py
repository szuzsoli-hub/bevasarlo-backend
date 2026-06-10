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
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

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
# URL-BŐL OLDALSZÁM KINYERÉS
# ===============================================================================
def extract_page_num_from_url(page_url, store_name):
    """
    URL-ből kinyeri az oldalszámot ahol az URL változik lapozáskor.
    Spar és Issuu esetén None-t ad vissza (Vision fogja leolvasni).
    """
    store_lower = store_name.lower()
    
    # Spar és Issuu: URL nem változik → Vision olvassa
    if 'spar' in store_lower or 'issuu' in page_url.lower():
        return None
    
    # CBA PDF: #page=N
    m = re.search(r'#page=(\d+)', page_url)
    if m:
        return int(m.group(1))
    
    # Penny: .../202623/6/ → 6
    m = re.search(r'/\d{6}/(\d+)/?', page_url)
    if m:
        return int(m.group(1))
    
    # Lidl: /page/4 vagy /page/4? → 4
    m = re.search(r'/page/(\d+)', page_url)
    if m:
        return int(m.group(1))
    
    # Auchan: ?page=4 → 4
    m = re.search(r'[?&]page=(\d+)', page_url)
    if m:
        return int(m.group(1))
    
    # Tesco: .../tesco-ujsag-2026-06-04/4 → 4
    m = re.search(r'/tesco-ujsag-[\d-]+/(\d+)', page_url)
    if m:
        return int(m.group(1))
    
    # Coop/Aldi/Metro: /page/4-5 → 4
    m = re.search(r'/page/(\d+)-\d+', page_url)
    if m:
        return int(m.group(1))
    
    return None

# ===============================================================================
# 1/A. MODUL: A FOTÓS
# ===============================================================================
def get_page_counter_from_dom(driver):
    """DOM-ból kinyeri a lapszámlálót (pl. '2-3 / 57' vagy '4 / 48')"""
    try:
        # Különböző viewereknél különböző selectorok
        selectors = [
            # Auchan, Tesco, Coop stílusú
            "[class*='page-counter']", "[class*='pageCounter']", "[class*='page_counter']",
            "[class*='pagination']", "[class*='pager']",
            # Lidl
            "[class*='flyer-page']", "[class*='page-number']",
            # Penny
            "[class*='page-indicator']", "[class*='pages']",
            # Általános
            "header [class*='page']", "nav [class*='page']",
        ]
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                txt = el.text.strip()
                if re.search(r'\d+\s*[-/]\s*\d+', txt):
                    print(f"   📄 DOM lapszámláló: '{txt}' (selector: {sel})")
                    return txt
        # JavaScript fallback - keresés az egész DOM-ban
        result = driver.execute_script("""
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var t = all[i].innerText || '';
                if (/^\\d+\\s*[-\\/]\\s*\\d+/.test(t.trim()) && t.trim().length < 20) {
                    return t.trim();
                }
            }
            return null;
        """)
        if result:
            print(f"   📄 DOM lapszámláló (JS): '{result}'")
            return result
    except Exception as e:
        print(f"   ⚠️ DOM lapszámláló hiba: {e}")
    return None

def parse_page_counter(counter_text):
    """
    Kinyeri a bal és jobb oldal számát a lapszámlálóból.
    '2-3 / 57' → (2, 3)
    '4 / 48' → (4, 4)  ← csak 1 oldal
    'pages 2-3 of 43' → (2, 3)
    """
    if not counter_text:
        return None, None
    # Keressük a X-Y mintát (két szám kötőjellel)
    m = re.search(r'(\d+)\s*[-–]\s*(\d+)', counter_text)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Csak egy szám: X / Y
    m = re.search(r'(\d+)\s*[/]\s*\d+', counter_text)
    if m:
        n = int(m.group(1))
        return n, n
    return None, None

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

        prev_screenshot_hash = None

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

                # === HASH ELLENŐRZÉS: lapozott-e ténylegesen? ===
                screenshot_bytes = driver.get_screenshot_as_png()
                current_hash = hash(screenshot_bytes)
                if prev_screenshot_hash is not None and current_hash == prev_screenshot_hash:
                    print(f"   ⚠️ FIGYELEM: {store_name} {page_num}. oldal = UGYANAZ mint az előző! Nem lapozott! Újra próbál...")
                    # Újra próbál lapozni
                    try:
                        driver.execute_script("document.querySelectorAll(\"[class*='next'], [class*='Right'], [class*='arrow']\").forEach(btn => { try { btn.click(); } catch(e) {} });")
                    except: pass
                    time.sleep(4)
                    screenshot_bytes = driver.get_screenshot_as_png()
                    current_hash = hash(screenshot_bytes)
                    if current_hash == prev_screenshot_hash:
                        print(f"   ❌ {store_name} {page_num}. oldal: Lapozás sikertelen, ugyanaz az oldal!")
                    else:
                        print(f"   ✅ {store_name} {page_num}. oldal: Másodpróbára sikerült a lapozás!")
                else:
                    print(f"   ✅ {store_name} {page_num}. oldal: Lapozás sikeres (hash változott)")
                prev_screenshot_hash = current_hash
                # Mentés a már elkészített screenshot-ból
                with open(fajl_nev, 'wb') as f:
                    f.write(screenshot_bytes)
            else:
                driver.save_screenshot(fajl_nev)
                with open(fajl_nev, 'rb') as f:
                    prev_screenshot_hash = hash(f.read())

            # === DOM LAPSZÁMLÁLÓ KIOLVASÁSA ===
            counter_text = get_page_counter_from_dom(driver)
            left_page, right_page = parse_page_counter(counter_text)
            current_url = driver.current_url
            print(f"   📍 URL: {current_url[-60:]}")
            print(f"   📄 Lapszámláló: '{counter_text}' → bal={left_page}, jobb={right_page}")

            captured_data.append({
                "image_path": fajl_nev,
                "page_url": current_url,
                "page_num": page_num,
                "left_page": left_page,
                "right_page": right_page,
            })
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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
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
            captured_data.append({
                "image_path": fajl_nev,
                "page_url": f"{target_url}#page={page_num}",
                "page_num": page_num
            })
        doc.close()
        return captured_data
    except Exception as e:
        print(f"❌ Hiba a PDF feldolgozásánál ({store_name}): {e}")
        return []
    finally:
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

# ===============================================================================
# 1/B. DÁTUM ELŐTÖLTÉS
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
    A linkek vége: ÉÉHHNN-[sorszám]-[típus].
    Például: ".../260219-1-spar-szorolap" → kezdődátum 02.19., típus SPAR.
    Keresd meg a képen a megfelelő szekciót és állítsd össze a tól-ig dátumot! Év: 2026.
    KÖTELEZŐ VÁLASZ FORMÁTUM: "ÉÉÉÉ.HH.NN. - ÉÉÉÉ.HH.NN."
    LINKEK:
    {json.dumps(links, indent=2)}
    ELVÁRT VÁLASZ (csak JSON):
    """
    response = client.chat.completions.create(
        model="gpt-4o", temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            {"type": "text", "text": prompt}
        ]}]
    )
    time.sleep(1)
    content = response.choices[0].message.content
    if not content:
        return {}
    return json.loads(content)

# ===============================================================================
# 2. MODUL: AI ELEMZÉS
# ===============================================================================
def interpret_image_with_ai(image_path, page_num, store_name, title_name, link_hint, pre_calc_date=None, need_vision_pagenum=False, double_page_info=None):
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    
    date_instr = ""
    if page_num == 1:
        if pre_calc_date and pre_calc_date != "N/A":
            date_instr = f"""
            FIGYELEM: A dátumot MÁR TUDJUK! NE keress érvényességi időt a képen!
            KÖTELEZŐEN ezt az értéket írd be az "ervenyesseg" JSON mezőbe pontosan így: {pre_calc_date}
            """
        else:
            date_instr = f"""
            FELADAT: DÁTUM KERESÉS ÉS SZIGORÚ FORMÁZÁS
            1. Keresd meg az érvényességi időt.
            2. Formátum: "ÉÉÉÉ.HH.NN. - ÉÉÉÉ.HH.NN."
            3. Töröld a napneveket, hónapokat alakítsd számmá.
            4. Hiányzó év: 2026.
            5. TESCO: hagyd figyelmen kívül a pontgyűjtők távoli dátumait.
            6. SPAR: a mondatszerű dátumból is fejtsd ki a két dátumot.
            7. FALLBACK: {link_hint}
            """

    # Oldalszám instrukció
    if need_vision_pagenum:
        pagenum_instr = f"""
    OLDALSZÁM KIOLVASÁS (KÖTELEZŐ!):
    - Keresd meg a képen a lapszámlálót (pl. "4 / 48" vagy "12 / 61" - általában felül középen)
    - Az "oldalszam" mezőbe a PERJEL ELŐTTI számot írd (pl. "4 / 48" → 4, "12 / 61" → 12)
    - CSAK a perjel előtti számot írd, semmi mást!
    - Ha nem látható lapszámláló → írd: {page_num}
    """
    elif double_page_info:
        # Dupla oldalas viewer: megmondjuk az AI-nak a bal/jobb oldal számát
        pagenum_instr = f"""
    OLDALSZÁM - FONTOS! A képen KÉT újságoldal látható egyszerre:
    - BAL OLDAL = {double_page_info.split(',')[0].split('=')[1].strip()}. oldal
    - JOBB OLDAL = {double_page_info.split(',')[1].split('=')[1].strip()}. oldal
    - Minden terméknél döntsd el hogy BAL vagy JOBB oldalon van-e, és az "oldalszam" mezőbe a megfelelő számot írd!
    - Ha a termék a bal felén van → bal oldal száma, ha jobb felén → jobb oldal száma
    """
    else:
        pagenum_instr = f"""
    OLDALSZÁM: Az oldalszámot már tudjuk, az "oldalszam" mezőbe írd: {page_num}
    """

    prompt = f"""
    Ez egy magyar akciós újság oldala. Bolt: {store_name} - {title_name}.
    {date_instr}
    SZABÁLYOK:
    - Csak azokat a termékeket add vissza ahol BIZTOSAN látod az árat
    - NE találj ki semmit
    - Az "ar" mezőbe csak számot írj (Ft jel nélkül)
    - Feltételes ár → ar_info mezőbe
    - Kedvezményes ár kerül az "ar"-ba
    - Nincs feltétel → ar_info: null
    - Nincs egységár → ar_info2: null
    {pagenum_instr}
    ELVÁRT JSON:
    {{
      "oldal_jelleg": "ÉLELMISZER_VEGYES",
      "ervenyesseg": "Dátum vagy N/A",
      "oldalszam": {page_num},
      "termekek": [
        {{
          "nev": "pontos terméknév",
          "ar": "akciós ár csak számként",
          "ar_info": "feltétel vagy null",
          "ar_info2": "normál ár vagy egységár vagy null"
        }}
      ]
    }}
    """
    response = client.chat.completions.create(
        model="gpt-4o", temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            {"type": "text", "text": prompt}
        ]}]
    )
    time.sleep(1)
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
        return today <= dates[-1]
    current_store = current_flyer_meta['store']
    current_url = current_flyer_meta['url']
    for flyer in all_flyers:
        if flyer['store'] == current_store and flyer['url'] != current_url:
            d_match = re.search(r'(202[4-6]|2[4-6])[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])', flyer['url'])
            if d_match:
                y, m, d = d_match.groups()
                y = int(y) if len(y) == 4 else int(f"20{y}")
                other_start = datetime.date(y, int(m), int(d))
                if other_start > start_date and today >= other_start:
                    return False
    return True

def build_forras_link(alap_link, page_num, store_name):
    """
    Boltonként felépíti a pontos forrasLink URL-t az oldalszám alapján.
    Dupla oldalas viewereknél (Auchan, Lidl, Penny, Tesco) ez adja a helyes linket.
    """
    store_lower = store_name.lower()
    try:
        if 'auchan' in store_lower:
            # Auchan: alap_link + ?page=N
            base = alap_link.split('?')[0].rstrip('/')
            return f"{base}?page={page_num}"
        elif 'lidl' in store_lower:
            # Lidl: .../ar/0?lf=HHZ → .../view/flyer/page/N?lf=HHZ
            m = re.search(r'(https://www\.lidl\.hu/l/hu/ujsag/[^/]+)', alap_link)
            if m:
                base = m.group(1)
                lf_match = re.search(r'(\?lf=[^&]+)', alap_link)
                lf = lf_match.group(1) if lf_match else ''
                return f"{base}/view/flyer/page/{page_num}{lf}"
            return alap_link
        elif 'penny' in store_lower:
            # Penny: .../202624/ + N/
            base = re.sub(r'/\d+/?$', '/', alap_link.rstrip('/') + '/')
            return f"{base}{page_num}/"
        elif 'tesco' in store_lower:
            # Tesco: alap_link/N (az alap_link már tartalmazza az /1-et, azt cseréljük)
            base = re.sub(r'/\d+$', '', alap_link.rstrip('/'))
            return f"{base}/{page_num}"
        elif 'coop' in store_lower.lower():
            # Coop: alap_link/page/N
            base = alap_link.rstrip('/')
            return f"{base}/page/{page_num}"
    except Exception as e:
        print(f"   ⚠️ forrasLink építési hiba ({store_name}, oldal {page_num}): {e}")
    return alap_link

def process_images_with_ai(captured_data, flyer_meta, all_flyers, pre_calc_date=None):
    print(f"🧠 AI Elemzés: {flyer_meta['store']} - {flyer_meta['title']}...")
    results = []
    store_name = flyer_meta['store']
    store_lower = store_name.lower()

    # Dupla oldalas viewerek ahol DOM lapszámlálóból kell az oldalszámot kezelni
    is_double_page_viewer = any(x in store_lower for x in ['auchan', 'lidl', 'penny', 'tesco', 'coop'])

    # Spar és Issuu esetén Vision olvassa az oldalszámot
    use_vision_pagenum = 'spar' in store_lower or 'issuu' in flyer_meta['url'].lower()
    
    link_hint = "N/A"
    url = flyer_meta['url']
    d_match = re.search(r'(202[4-6]|2[4-6])[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])', url)
    if d_match:
        y, m, d = d_match.groups()
        link_hint = f"{y if len(y)==4 else '20'+y}.{m}.{d}."
    
    detected_validity = "N/A"
    
    for item in captured_data:
        left_page = item.get('left_page')
        right_page = item.get('right_page')

        # Oldalszám meghatározása
        if use_vision_pagenum:
            url_pagenum = None
        else:
            url_pagenum = extract_page_num_from_url(item['page_url'], store_name)

        # Ha DOM lapszámláló elérhető, azt használjuk
        if left_page is not None:
            effective_pagenum = left_page
            print(f"   📄 DOM lapszámláló alapú oldalszám: bal={left_page}, jobb={right_page}")
        elif url_pagenum is not None:
            effective_pagenum = url_pagenum
            print(f"   📄 URL alapú oldalszám: {url_pagenum}")
        else:
            effective_pagenum = item['page_num']
            print(f"   📄 Sorszám alapú oldalszám: {effective_pagenum}")

        # Ha dupla oldalas viewer, megmondjuk az AI-nak a bal/jobb oldal számát
        if is_double_page_viewer and left_page is not None and right_page is not None and left_page != right_page:
            double_page_info = f"left_page={left_page}, right_page={right_page}"
        else:
            double_page_info = None

        structured = interpret_image_with_ai(
            item['image_path'],
            effective_pagenum,
            store_name,
            flyer_meta['title'],
            link_hint,
            pre_calc_date,
            need_vision_pagenum=use_vision_pagenum,
            double_page_info=double_page_info
        )
        
        if item['page_num'] == 1:
            if pre_calc_date and pre_calc_date != "N/A":
                detected_validity = pre_calc_date
            else:
                detected_validity = structured.get("ervenyesseg", "N/A")
            if not check_validity_date(detected_validity, flyer_meta, all_flyers):
                print(f"⛔ LEJÁRT: {detected_validity}")
                return []
        
        # Végső oldalszám: URL-ből, DOM-ból vagy Vision-tól
        if use_vision_pagenum:
            final_pagenum = structured.get("oldalszam", item['page_num'])
            try:
                final_pagenum = int(final_pagenum)
            except:
                final_pagenum = item['page_num']
        else:
            final_pagenum = effective_pagenum

        termekek = structured.get("termekek", [])
        print(f"   🛒 Talált termékek: {len(termekek)} db (oldal: {final_pagenum})")

        if termekek:
            for product in termekek:
                ar_val = str(product.get("ar", "")).strip()
                if ar_val and re.match(r'^[\d\s\.,]+$', ar_val):
                    ar_val = f"{ar_val} Ft"

                # Termék oldalszáma: ha dupla oldal, az AI megmondja melyik oldalon van
                product_page = product.get("oldalszam", final_pagenum)
                try:
                    product_page = int(product_page)
                except:
                    product_page = final_pagenum

                # forrasLink: oldalszám alapján építjük
                if is_double_page_viewer:
                    forras = build_forras_link(flyer_meta['url'], product_page, store_name)
                else:
                    forras = item['page_url']

                print(f"      → {product.get('nev', '?')[:30]} | {ar_val} | oldal={product_page} | link={forras[-40:]}")

                results.append({
                    "bolt": store_name,
                    "ujsag": flyer_meta['title'],
                    "oldalszam": product_page,
                    "ervenyesseg": detected_validity,
                    "nev": product.get("nev"),
                    "ar": ar_val,
                    "ar_info": product.get("ar_info"),
                    "ar_info2": product.get("ar_info2"),
                    "forrasLink": forras,
                    "alap_link": flyer_meta['url']
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
    final_products = []
    processed_urls = set()
    for product in old_products:
        url = product.get('alap_link')
        matching_flyer = next((f for f in current_flyers if f['url'] == url), None)
        if matching_flyer and check_validity_date(product.get('ervenyesseg'), matching_flyer, current_flyers):
            final_products.append(product)
            processed_urls.add(url)

    # ============================================================
    # IDŐFIGYELÉS + BOLTONKÉNTI MENTÉS
    # ============================================================
    START_TIME = datetime.datetime.now()
    TIME_LIMIT_MINUTES = 330  # 5.5 óra → biztonságos leállás 6 óra előtt

    def ido_van_meg():
        eltelt = (datetime.datetime.now() - START_TIME).total_seconds() / 60
        return eltelt < TIME_LIMIT_MINUTES

    def mentes(products):
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

    # Boltok csoportosítása (csak a még nem feldolgozottak)
    store_groups = {}
    for flyer in current_flyers:
        if flyer['url'] in processed_urls:
            continue
        store = flyer['store']
        if store not in store_groups:
            store_groups[store] = []
        store_groups[store].append(flyer)

    for store_name, flyers in store_groups.items():
        if not ido_van_meg():
            print(f"⏰ Időlimit elérve! {store_name} és a többi bolt marad a következő futásra.")
            break
        print(f"\n🏪 BOLT FELDOLGOZÁSA: {store_name} ({len(flyers)} újság)")
        for flyer in flyers:
            if not ido_van_meg():
                print(f"⏰ Időlimit elérve! {flyer['title']} marad a következő futásra.")
                break
            pre_calc_date = pre_fetched_dates.get(flyer['url'])
            pages = capture_pages_from_pdf(flyer['url'], flyer['store']) if flyer['url'].lower().endswith('.pdf') else capture_pages_with_selenium(flyer['url'], flyer['store'])
            if pages:
                new_items = process_images_with_ai(pages, flyer, current_flyers, pre_calc_date)
                final_products.extend(new_items)
        # Bolt összes újságja kész → mentés
        print(f"💾 Mentés {store_name} után...")
        mentes(final_products)
    # ============================================================

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
                except: pass
    mentes(final_products)
    print(f"\n🏁 KÉSZ! Adatbázis: {len(final_products)} termék.")
