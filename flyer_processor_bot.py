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
    store_lower = store_name.lower()
    if 'spar' in store_lower or 'issuu' in page_url.lower():
        return None
    m = re.search(r'#page=(\d+)', page_url)
    if m:
        return int(m.group(1))
    m = re.search(r'/\d{6}/(\d+)/?', page_url)
    if m:
        return int(m.group(1))
    m = re.search(r'/page/(\d+)', page_url)
    if m:
        return int(m.group(1))
    m = re.search(r'[?&]page=(\d+)', page_url)
    if m:
        return int(m.group(1))
    m = re.search(r'/tesco-ujsag-[\d-]+/(\d+)', page_url)
    if m:
        return int(m.group(1))
    m = re.search(r'/page/(\d+)-\d+', page_url)
    if m:
        return int(m.group(1))
    return None

# ===============================================================================
# 1/A. MODUL: A FOTOS
# ===============================================================================
def get_page_counter_from_dom(driver):
    try:
        selectors = [
            "[class*='page-counter']", "[class*='pageCounter']", "[class*='page_counter']",
            "[class*='pagination']", "[class*='pager']",
            "[class*='flyer-page']", "[class*='page-number']",
            "[class*='page-indicator']", "[class*='pages']",
            "header [class*='page']", "nav [class*='page']",
        ]
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                txt = el.text.strip()
                if re.search(r'\d+\s*[-/]\s*\d+', txt):
                    print(f"   📄 DOM lapszamlalo: '{txt}' (selector: {sel})")
                    return txt
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
            print(f"   📄 DOM lapszamlalo (JS): '{result}'")
            return result
    except Exception as e:
        print(f"   Lapszamlalo hiba: {e}")
    return None

def parse_page_counter(counter_text):
    if not counter_text:
        return None, None
    m = re.search(r'(\d+)\s*[-]\s*(\d+)', counter_text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'(\d+)\s*[/]\s*\d+', counter_text)
    if m:
        n = int(m.group(1))
        return n, n
    return None, None

# ===============================================================================
# URL ALAPU OLDALANKENTI FOTOZAS
# ===============================================================================
def build_page_urls(alap_url, store_name, count=4):
    store_lower = store_name.lower()
    urls = []
    for page_num in range(1, count + 1):
        if 'aldi' in store_lower:
            base = re.sub(r'/page/\d+$', '', alap_url.rstrip('/'))
            urls.append((page_num, f"{base}/page/{page_num}"))
        elif 'metro' in store_lower:
            base = re.sub(r'/page/\d+$', '', alap_url.rstrip('/'))
            urls.append((page_num, f"{base}/page/{page_num}"))
        elif 'coop' in store_lower:
            base = re.sub(r'/page/\d+$', '', alap_url.rstrip('/'))
            if page_num == 1:
                urls.append((page_num, alap_url))
            else:
                urls.append((page_num, f"{base}/page/{page_num}"))
        elif 'auchan' in store_lower:
            base = alap_url.split('?')[0].rstrip('/')
            urls.append((page_num, f"{base}?page={page_num}"))
        elif 'penny' in store_lower:
            path_part = alap_url.split('?')[0].rstrip('/')
            query_part = ('?' + alap_url.split('?')[1]) if '?' in alap_url else ''
            path_part = re.sub(r'/(\d{1,2})$', '', path_part)
            urls.append((page_num, f"{path_part}/{page_num}/{query_part}"))
        elif 'tesco' in store_lower:
            base = re.sub(r'/\d+$', '', alap_url.rstrip('/'))
            urls.append((page_num, f"{base}/{page_num}"))
        elif 'lidl' in store_lower:
            m = re.search(r'(https://www\.lidl\.hu/l/hu/ujsag/[^/]+)', alap_url)
            lf_match = re.search(r'(\?lf=[^&]+)', alap_url)
            lf = lf_match.group(1) if lf_match else ''
            if m:
                base = m.group(1)
                urls.append((page_num, f"{base}/view/flyer/page/{page_num}{lf}"))
            else:
                urls.append((page_num, alap_url))
        else:
            urls.append((page_num, alap_url))
    return urls

def capture_pages_by_url(alap_url, store_name, count=4):
    print(f"\nURL ALAPU FOTOZAS INDUL ({store_name}): {alap_url}")
    page_urls = build_page_urls(alap_url, store_name, count)
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1280,900")
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
        for page_num, page_url in page_urls:
            print(f"   Oldal {page_num}: {page_url[-70:]}")
            fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
            try:
                driver.get(page_url)
                time.sleep(8)
                if page_num == 1:
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
                driver.save_screenshot(fajl_nev)
                captured_data.append({
                    "image_path": fajl_nev,
                    "page_url": page_url,
                    "page_num": page_num,
                    "left_page": None,
                    "right_page": None,
                })
                print(f"   Oldal {page_num} fotozva")
            except Exception as e:
                print(f"   Oldal {page_num} hiba: {e}")
        return captured_data
    except Exception as e:
        print(f"Hiba az URL alapu fotozsanal: {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()

def capture_pages_with_selenium(target_url, store_name):
    print(f"\nFOTOZAS INDUL ({store_name}): {target_url}")
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

                screenshot_bytes = driver.get_screenshot_as_png()
                current_hash = hash(screenshot_bytes)
                if prev_screenshot_hash is not None and current_hash == prev_screenshot_hash:
                    print(f"   FIGYELEM: {store_name} {page_num}. oldal = UGYANAZ! Ujra probal...")
                    try:
                        driver.execute_script("document.querySelectorAll(\"[class*='next'], [class*='Right'], [class*='arrow']\").forEach(btn => { try { btn.click(); } catch(e) {} });")
                    except: pass
                    time.sleep(4)
                    screenshot_bytes = driver.get_screenshot_as_png()
                    current_hash = hash(screenshot_bytes)
                    if current_hash == prev_screenshot_hash:
                        print(f"   {store_name} {page_num}. oldal: Lapozas sikertelen!")
                    else:
                        print(f"   {store_name} {page_num}. oldal: Masodprobara sikerult!")
                else:
                    print(f"   {store_name} {page_num}. oldal: Lapozas sikeres")
                prev_screenshot_hash = current_hash
                with open(fajl_nev, 'wb') as f:
                    f.write(screenshot_bytes)
            else:
                driver.save_screenshot(fajl_nev)
                with open(fajl_nev, 'rb') as f:
                    prev_screenshot_hash = hash(f.read())

            counter_text = get_page_counter_from_dom(driver)
            left_page, right_page = parse_page_counter(counter_text)
            current_url = driver.current_url
            print(f"   URL: {current_url[-60:]}")
            print(f"   Lapszamlalo: '{counter_text}' bal={left_page}, jobb={right_page}")

            captured_data.append({
                "image_path": fajl_nev,
                "page_url": current_url,
                "page_num": page_num,
                "left_page": left_page,
                "right_page": right_page,
            })
        return captured_data
    except Exception as e:
        print(f"Hiba a fotozsnal: {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()


def capture_pages_spar(target_url, store_name, count=4):
    print(f"\nSPAR FOTOZAS INDUL: {target_url}")

    slug_match = re.search(r'/ajanlatok/([^/?#]+)/([^/?#]+)', target_url)
    if slug_match:
        szorolap_url = f"https://szorolap.spar.hu/{slug_match.group(1)}/{slug_match.group(2)}/"
        ipaper_base = f"https://ipaper.ipapercms.dk/spar-hungary/{slug_match.group(1)}/{slug_match.group(2)}/Image.ashx"
    else:
        szorolap_url = target_url
        ipaper_base = None

    # 1. PROBA: iPaper Image API
    if ipaper_base:
        print(f"   iPaper API proba: {ipaper_base}")
        api_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': szorolap_url,
        }
        api_captured = []
        api_ok = True
        for page_num in range(1, count + 1):
            api_url = f"{ipaper_base}?PageNumber={page_num}&ImageType=Large"
            try:
                r = requests.get(api_url, headers=api_headers, timeout=15)
                if r.status_code == 200 and 'image' in r.headers.get('content-type', ''):
                    fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
                    with open(fajl_nev, 'wb') as f:
                        f.write(r.content)
                    forras = f"{szorolap_url}{page_num}/"
                    api_captured.append({
                        "image_path": fajl_nev,
                        "page_url": forras,
                        "page_num": page_num,
                        "left_page": page_num,
                        "right_page": page_num,
                    })
                    print(f"   API oldal {page_num} letoltve")
                else:
                    print(f"   API oldal {page_num}: HTTP {r.status_code}")
                    api_ok = False
                    break
            except Exception as e:
                print(f"   API hiba: {e}")
                api_ok = False
                break
        if api_ok and len(api_captured) == count:
            print(f"   iPaper API sikerult! {len(api_captured)} oldal letoltve.")
            return api_captured

    print(f"   iPaper API nem sikerult, Selenium fallback")

    # 2. FALLBACK: Selenium
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

        print(f"   Selenium: {szorolap_url}")
        driver.get(szorolap_url)
        time.sleep(10)

        try:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                if any(x in btn.text.lower() for x in ["elfogad", "accept", "ok", "rendben"]):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
                    break
        except: pass
        try:
            driver.execute_script("document.querySelectorAll('div[class*=\"cookie\"], #onetrust-banner-sdk').forEach(el => el.remove());")
        except: pass

        prev_hash = None
        i = 0

        while len(captured_data) < count:
            counter_text = get_page_counter_from_dom(driver)
            left_page, right_page = parse_page_counter(counter_text)
            print(f"   Lapszamlalo: '{counter_text}' bal={left_page}, jobb={right_page}")

            screenshot_bytes = driver.get_screenshot_as_png()
            current_hash = hash(screenshot_bytes)

            if prev_hash is not None and current_hash == prev_hash:
                print(f"   Ugyanaz az oldal! Ujraprobalkozas...")
                _spar_lapoz(driver)
                time.sleep(5)
                screenshot_bytes = driver.get_screenshot_as_png()
                current_hash = hash(screenshot_bytes)
                if current_hash == prev_hash:
                    print(f"   Lapozas sikertelen, leallunk.")
                    break
                else:
                    print(f"   Masodprobara sikerult!")
                    counter_text = get_page_counter_from_dom(driver)
                    left_page, right_page = parse_page_counter(counter_text)

            prev_hash = current_hash

            if left_page is not None and right_page is not None and left_page != right_page:
                fajl_bal = os.path.join(TEMP_DIR, f"{store_name}_oldal_{left_page}.png")
                fajl_jobb = os.path.join(TEMP_DIR, f"{store_name}_oldal_{right_page}.png")
                _crop_screenshot(screenshot_bytes, fajl_bal, side='left')
                _crop_screenshot(screenshot_bytes, fajl_jobb, side='right')
                for pg, fajl in [(left_page, fajl_bal), (right_page, fajl_jobb)]:
                    if len(captured_data) < count:
                        forras = f"{szorolap_url}{pg}/"
                        captured_data.append({
                            "image_path": fajl,
                            "page_url": forras,
                            "page_num": pg,
                            "left_page": pg,
                            "right_page": pg,
                        })
                        print(f"   Dupla crop: {pg}. oldal")
            else:
                pg = left_page if left_page is not None else (i + 1)
                fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{pg}.png")
                with open(fajl_nev, 'wb') as f:
                    f.write(screenshot_bytes)
                forras = f"{szorolap_url}{pg}/"
                captured_data.append({
                    "image_path": fajl_nev,
                    "page_url": forras,
                    "page_num": pg,
                    "left_page": pg,
                    "right_page": pg,
                })
                print(f"   Egyoldal: {pg}. oldal")

            if len(captured_data) < count:
                _spar_lapoz(driver)
                time.sleep(6)
            i += 1
            if i > count + 3:
                break

        return captured_data

    except Exception as e:
        print(f"Spar Selenium hiba: {e}")
        return []
    finally:
        if 'driver' in locals():
            driver.quit()


def _spar_lapoz(driver):
    # 1. iPaper JS API
    try:
        result = driver.execute_script("""
            if (window.iPaperAPI && typeof window.iPaperAPI.goToNextPage === 'function') {
                window.iPaperAPI.goToNextPage();
                return 'api_goToNextPage';
            }
            if (window.iPaperAPI && typeof window.iPaperAPI.next === 'function') {
                window.iPaperAPI.next();
                return 'api_next';
            }
            return 'api_not_found';
        """)
        if result in ('api_goToNextPage', 'api_next'):
            print(f"   Lapozas: iPaper JS API ({result})")
            return True
        print(f"   iPaper JS API nem elerheto ({result})")
    except Exception as e:
        print(f"   iPaper JS API hiba: {e}")

    # 2. CSS selector
    try:
        nyil_selectorok = [
            "[class*='next']", "[class*='Next']",
            "[class*='right']", "[class*='Right']",
            "[class*='arrow']", "[class*='Arrow']",
            "button[aria-label*='next']", "button[aria-label*='Next']",
        ]
        for sel in nyil_selectorok:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    print(f"   Lapozas: CSS selector ({sel})")
                    return True
    except Exception as e:
        print(f"   CSS selector hiba: {e}")

    # 3. Koordinata kattintas
    try:
        w = driver.execute_script("return window.innerWidth")
        h = driver.execute_script("return window.innerHeight")
        x = int(w * 0.92)
        y = int(h * 0.5)
        driver.execute_script(f"document.elementFromPoint({x},{y})?.click()")
        print(f"   Lapozas: koordinata kattintas ({x},{y})")
        return True
    except Exception as e:
        print(f"   Koordinata lapozas hiba: {e}")

    return False


def _crop_screenshot(screenshot_bytes, output_path, side='left'):
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(screenshot_bytes))
        w, h = img.size
        if side == 'left':
            cropped = img.crop((0, 0, w // 2, h))
        else:
            cropped = img.crop((w // 2, 0, w, h))
        cropped.save(output_path)
    except Exception as e:
        print(f"   Crop hiba ({side}): {e}, teljes kep mentve")
        with open(output_path, 'wb') as f:
            f.write(screenshot_bytes)


def capture_pages_from_pdf(target_url, store_name):
    print(f"\nPDF LETOLTES ES SZELETELES ({store_name}): {target_url}")
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
        print(f"Hiba a PDF feldolgozasanal ({store_name}): {e}")
        return []
    finally:
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

# ===============================================================================
# 1/B. DATUM ELOTOLTES
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
    Például: ".../260219-1-spar-szorolap" -> kezdődátum 02.19., típus SPAR.
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
# HTML/URL ALAPU DATUM KINYERES
# ===============================================================================
def get_validity_from_html(url, store):
    store_lower = store.lower()

    if 'cba' in store_lower or 'prima' in store_lower:
        m = re.search(r'_(\d{2})(\d{2})-(\d{2})(\d{2})\.pdf', url, re.IGNORECASE)
        if m:
            year_match = re.search(r'/(\d{4})/', url)
            year = year_match.group(1) if year_match else str(datetime.date.today().year)
            m1, d1, m2, d2 = m.group(1), m.group(2), m.group(3), m.group(4)
            result = f"{year}.{m1}.{d1}. - {year}.{m2}.{d2}."
            print(f"   CBA/Prima URL regex: {result}")
            return result
        return None

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"   HTML fetch hiba ({store}): {e}")
        return None

    if 'aldi' in store_lower:
        m = re.search(r'<title>[^<]*?(\d{4}\.\d{2}\.\d{2})\.-(\d{4}\.\d{2}\.\d{2})', html)
        if m:
            result = f"{m.group(1)}. - {m.group(2)}."
            print(f"   Aldi title: {result}")
            return result
        return None

    if 'penny' in store_lower:
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']', html, re.IGNORECASE)
        if m:
            import html as html_lib
            desc = html_lib.unescape(m.group(1))
            honap_map = {
                'januar': '01', 'februar': '02', 'marcius': '03', 'aprilis': '04',
                'majus': '05', 'junius': '06', 'julius': '07', 'augusztus': '08',
                'szeptember': '09', 'oktober': '10', 'november': '11', 'december': '12',
                'január': '01', 'február': '02', 'március': '03', 'április': '04',
                'május': '05', 'június': '06', 'július': '07',
                'október': '10',
            }
            dm = re.search(r'(\w+)\s+(\d+)\.\s+és\s+(\w+)\s+(\d+)\.', desc, re.IGNORECASE)
            if dm:
                h1 = honap_map.get(dm.group(1).lower())
                d1 = dm.group(2).zfill(2)
                h2 = honap_map.get(dm.group(3).lower())
                d2 = dm.group(4).zfill(2)
                year = str(datetime.date.today().year)
                if h1 and h2:
                    result = f"{year}.{h1}.{d1}. - {year}.{h2}.{d2}."
                    print(f"   Penny meta description: {result}")
                    return result
        return None

    if 'metro' in store_lower:
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']', html, re.IGNORECASE)
        if m:
            desc = m.group(1)
            honap_map = {
                'JANUAR': '01', 'FEBRUAR': '02', 'MARCIUS': '03', 'APRILIS': '04',
                'MAJUS': '05', 'JUNIUS': '06', 'JULIUS': '07', 'AUGUSZTUS': '08',
                'SZEPTEMBER': '09', 'OKTOBER': '10', 'NOVEMBER': '11', 'DECEMBER': '12',
                'JANUÁR': '01', 'FEBRUÁR': '02', 'MÁRCIUS': '03', 'ÁPRILIS': '04',
                'MÁJUS': '05', 'JÚNIUS': '06', 'JÚLIUS': '07', 'OKTÓBER': '10',
            }
            dm = re.search(r'(\d{4})\.\s+(\w+)\s+(\d+)-(\d+)\.', desc)
            if dm:
                year = dm.group(1)
                honap = honap_map.get(dm.group(2).upper())
                d1 = dm.group(3).zfill(2)
                d2 = dm.group(4).zfill(2)
                if honap:
                    result = f"{year}.{honap}.{d1}. - {year}.{honap}.{d2}."
                    print(f"   Metro meta description: {result}")
                    return result
        return None

    if 'coop' in store_lower:
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']', html, re.IGNORECASE)
        if m:
            desc = m.group(1)
            dm = re.search(r'(\d{4})\.(\d{2})\.(\d{2})\s*-\s*(\d{2})\.(\d{2})', desc)
            if dm:
                year = dm.group(1)
                result = f"{year}.{dm.group(2)}.{dm.group(3)}. - {year}.{dm.group(4)}.{dm.group(5)}."
                print(f"   Coop meta description: {result}")
                return result
            dm2 = re.search(r'(\d{4}\.\d{2}\.\d{2})\s*[-]\s*(\d{4}\.\d{2}\.\d{2})', desc)
            if dm2:
                result = f"{dm2.group(1)}. - {dm2.group(2)}."
                print(f"   Coop meta description (fallback): {result}")
                return result
            year_now = str(datetime.date.today().year)
            dm3 = re.search(rf'({year_now})\.\s*(\d{{2}})\.\s*(\d{{2}})\.\s*\w+\s+(\d{{2}})\.\s*(\d{{2}})\.', desc)
            if dm3:
                year = dm3.group(1)
                result = f"{year}.{dm3.group(2)}.{dm3.group(3)}. - {year}.{dm3.group(4)}.{dm3.group(5)}."
                print(f"   Coop meta description (Alfold): {result}")
                return result
        return None

    return None

# ===============================================================================
# 2. MODUL: AI ELEMZES
# ===============================================================================
def interpret_image_with_ai(image_path, page_num, store_name, title_name, link_hint, pre_calc_date=None, need_vision_pagenum=False, double_page_info=None):
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    date_instr = ""
    if page_num == 1:
        if pre_calc_date and pre_calc_date != "N/A":
            date_instr = f'DATUM: Mar tudjuk, NE keresd a kepen! Az "ervenyesseg" mezobe pontosan ezt ird: {pre_calc_date}'
        else:
            date_instr = f"""DATUM KERESES: Keresd meg az ervenyessegi idot a kepen.
Formatum: "EEEE.HH.NN. - EEEE.HH.NN." — honapokat szamma, napneveket torolj, hianyzo ev: 2026.
TESCO: a pontgyujtok tavoli datumait hagyd figyelmen kivul.
SPAR: mondatszeru datumbol is fejtsd ki a ket datumot.
FALLBACK ha nem latod: {link_hint}"""

    if need_vision_pagenum:
        pagenum_instr = f'OLDALSZAM: Keresd meg a lapszamlalot (pl. "4 / 48" — altalaban felul kozepen). Az "oldalszam" mezobe a PERJEL ELOTTI szamot ird. Ha nem lathato: {page_num}'
    elif double_page_info:
        bal = double_page_info.split(',')[0].split('=')[1].strip()
        jobb = double_page_info.split(',')[1].split('=')[1].strip()
        pagenum_instr = f'OLDALSZAM: A kepen KET ujsagoldal lathato. BAL OLDAL = {bal}. oldal, JOBB OLDAL = {jobb}. oldal. Minden termeknel dontsd el melyik oldalon van!'
    else:
        pagenum_instr = f'OLDALSZAM: Az "oldalszam" mezobe ird: {page_num}'

    prompt = f"""Ez egy magyar akcioS ujsag oldala. Bolt: {store_name} — {title_name}.
{date_instr}

FELADATOD: Add vissza az osszes akcioS termeKet amit ezen az oldalon latsz.
A felhasznalo vasarlasi dontest hoz — minden lathato informacio fontos!

TERMEKENKENTI SZABALYOK:
- "nev": marka + pontos termeknev (pl. "S-Budget csirkemellfile", "Lay's chips Max")
- "kiszereles": gramm, kg, liter, db, csomag stb. ahogy az ujsagban latod (pl. "500g", "1,5l", "10db") — ha nem lathato: null
- "ar": az akcioS ar MINDIG Ft-tal! Ha latod a szamot de nincs Ft jelolve, add hozza! (pl. "1199 Ft")
- "ar_egyseg": egysegar ha lathato (pl. "2398 Ft/kg", "199 Ft/l", "49 Ft/db") — osszehasonlitashoz kritikus! Ha nem lathato: null
- "ar_info": MINDEN egyeb feltetel es info amit latsz, pontosan ahogy az ujsagban szerepel:
    * darabszam feltetel pl: "2 db vasarlasaKor, 1 db ara: 1499 Ft"
    * idoszaki ervenYesseg pl: "csak 06.11-06.14. kozott"
    * kartYafeltetel pl: "MySpar kartyaval"
    * normal ar pl: "normal ar: 1599 Ft"
    * ezek kombinacioja is lehetseges
    * Ha nincs ilyen info: null

{pagenum_instr}

ELVART JSON:
{{
  "ervenyesseg": "Datum vagy N/A",
  "oldalszam": {page_num},
  "termekek": [
    {{
      "nev": "marka + termekNev",
      "kiszereles": "pl. 500g vagy null",
      "ar": "akcioS ar Ft-tal",
      "ar_egyseg": "pl. 2398 Ft/kg vagy null",
      "ar_info": "minden egyeb info vagy null"
    }}
  ]
}}"""

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
    store_lower = store_name.lower()
    try:
        if 'auchan' in store_lower:
            base = alap_link.split('?')[0].rstrip('/')
            return f"{base}?page={page_num}"
        elif 'lidl' in store_lower:
            m = re.search(r'(https://www\.lidl\.hu/l/hu/ujsag/[^/]+)', alap_link)
            if m:
                base = m.group(1)
                lf_match = re.search(r'(\?lf=[^&]+)', alap_link)
                lf = lf_match.group(1) if lf_match else ''
                return f"{base}/view/flyer/page/{page_num}{lf}"
            return alap_link
        elif 'penny' in store_lower:
            base = re.sub(r'/\d+/?$', '/', alap_link.rstrip('/') + '/')
            return f"{base}{page_num}/"
        elif 'tesco' in store_lower:
            base = re.sub(r'/\d+$', '', alap_link.rstrip('/'))
            return f"{base}/{page_num}"
        elif 'coop' in store_lower:
            base = alap_link.rstrip('/')
            return f"{base}/page/{page_num}"
    except Exception as e:
        print(f"   forrasLink epítesi hiba ({store_name}, oldal {page_num}): {e}")
    return alap_link

def _format_validity(raw_date):
    """Érvényesség szöveg formázása egységesen."""
    if not raw_date or raw_date == "N/A":
        return "N/A"
    return f"Újság érvényessége: {raw_date} (egyes termékek akciós érvényessége eltérhet, ellenőrizd az újságban!)"

def process_images_with_ai(captured_data, flyer_meta, all_flyers, pre_calc_date=None):
    print(f"AI Elemzes: {flyer_meta['store']} - {flyer_meta['title']}...")
    results = []
    store_name = flyer_meta['store']
    store_lower = store_name.lower()

    is_double_page_viewer = False
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

        if use_vision_pagenum:
            url_pagenum = None
        else:
            url_pagenum = extract_page_num_from_url(item['page_url'], store_name)

        if left_page is not None:
            effective_pagenum = left_page
        elif url_pagenum is not None:
            effective_pagenum = url_pagenum
        else:
            effective_pagenum = item['page_num']

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
                detected_validity = _format_validity(pre_calc_date)
            else:
                raw = structured.get("ervenyesseg", "N/A")
                detected_validity = _format_validity(raw)
            if not check_validity_date(detected_validity, flyer_meta, all_flyers):
                print(f"LEJART: {detected_validity}")
                return []

        if use_vision_pagenum:
            final_pagenum = structured.get("oldalszam", item['page_num'])
            try:
                final_pagenum = int(final_pagenum)
            except:
                final_pagenum = item['page_num']
        else:
            final_pagenum = effective_pagenum

        termekek = structured.get("termekek", [])
        print(f"   Talalt termekek: {len(termekek)} db (oldal: {final_pagenum})")

        if termekek:
            for product in termekek:
                ar_val = str(product.get("ar", "")).strip()
                if ar_val and 'Ft' not in ar_val and re.search(r'\d', ar_val):
                    ar_val = f"{ar_val} Ft"

                product_page = product.get("oldalszam", final_pagenum)
                try:
                    product_page = int(product_page)
                except:
                    product_page = final_pagenum

                if is_double_page_viewer:
                    forras = build_forras_link(flyer_meta['url'], product_page, store_name)
                else:
                    forras = item['page_url']

                print(f"      -> {product.get('nev', '?')[:30]} | {ar_val} | oldal={product_page}")

                results.append({
                    "bolt": store_name,
                    "ujsag": flyer_meta['title'],
                    "oldalszam": product_page,
                    "ervenyesseg": detected_validity,
                    "nev": product.get("nev"),
                    "kiszereles": product.get("kiszereles"),
                    "ar": ar_val,
                    "ar_egyseg": product.get("ar_egyseg"),
                    "ar_info": product.get("ar_info"),
                    "forrasLink": forras,
                    "alap_link": flyer_meta['url']
                })
    return results

if __name__ == "__main__":
    print("=== PROFESSZOR BOT: VEGLEGES RENDRAKÓ VERZIO ===")
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
        print("\nAUCHAN ERVENYESSEGEK ELOTOLTESE...")
        pre_fetched_dates.update(get_auchan_pre_dates(auchan_links))
    if spar_links:
        print("\nSPAR ERVENYESSEGEK ELOTOLTESE...")
        pre_fetched_dates.update(get_spar_pre_dates(spar_links))

    HTML_DATE_STORES = ['aldi', 'penny', 'metro', 'coop', 'cba', 'prima']
    print("\nHTML/URL ALAPU ERVENYESSEGEK ELOTOLTESE...")
    for flyer in current_flyers:
        store_lower = flyer['store'].lower()
        if any(s in store_lower for s in HTML_DATE_STORES):
            validity = get_validity_from_html(flyer['url'], flyer['store'])
            if validity:
                pre_fetched_dates[flyer['url']] = validity
                print(f"   OK {flyer['store']}: {validity}")
            else:
                print(f"   HIBA {flyer['store']} ({flyer['title']}): nem sikerult kinyerni")

    final_products = []
    processed_urls = set()
    for product in old_products:
        url = product.get('alap_link')
        matching_flyer = next((f for f in current_flyers if f['url'] == url), None)
        if matching_flyer and check_validity_date(product.get('ervenyesseg'), matching_flyer, current_flyers):
            final_products.append(product)
            processed_urls.add(url)

    START_TIME = datetime.datetime.now()
    TIME_LIMIT_MINUTES = 330

    def ido_van_meg():
        eltelt = (datetime.datetime.now() - START_TIME).total_seconds() / 60
        return eltelt < TIME_LIMIT_MINUTES

    def mentes(products):
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

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
            print(f"Idolimit! {store_name} marad a kovetkezo futasra.")
            break
        print(f"\nBOLT: {store_name} ({len(flyers)} ujsag)")
        for flyer in flyers:
            if not ido_van_meg():
                print(f"Idolimit! {flyer['title']} marad.")
                break
            pre_calc_date = pre_fetched_dates.get(flyer['url'])
            store_lower_main = flyer['store'].lower()
            url_based_stores = ['aldi', 'metro', 'coop', 'auchan', 'penny', 'tesco', 'lidl']
            if flyer['url'].lower().endswith('.pdf'):
                pages = capture_pages_from_pdf(flyer['url'], flyer['store'])
            elif 'spar' in store_lower_main:
                pages = capture_pages_spar(flyer['url'], flyer['store'])
            elif any(s in store_lower_main for s in url_based_stores):
                pages = capture_pages_by_url(flyer['url'], flyer['store'])
            else:
                pages = capture_pages_with_selenium(flyer['url'], flyer['store'])
            if pages:
                new_items = process_images_with_ai(pages, flyer, current_flyers, pre_calc_date)
                final_products.extend(new_items)
        print(f"Mentes {store_name} utan...")
        mentes(final_products)

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
        if "-tol" in erv or "-tol" in erv:
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
                        p["ervenyesseg"] = _format_validity(f"{p_start.strftime('%Y.%m.%d.')} - {end_date.strftime('%Y.%m.%d.')}")
                except: pass

    mentes(final_products)
    print(f"\nKESZ! Adatbazis: {len(final_products)} termek.")
