import requests
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
import re
import datetime
import json
import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# KÜLSŐ MODUL IMPORTÁLÁSA
try:
    from spar_hunter import scan_spar_only
except ImportError:
    print("⚠️ FIGYELEM: Nem találom a spar_hunter.py fájlt! A SPAR szkennelés kimarad.")
    def scan_spar_only(): return []

# --- KONFIGURÁCIÓ ---
OUTPUT_FILE = 'flyers.json'


# ===============================================================================
# 1. RÉSZ: HAGYOMÁNYOS BOLTOK (Protocol & Scanners)
# ===============================================================================

def analyze_link(store_name, title, url):
    t = title.lower()
    u = url.lower()

    # --- 1. PENNY ---
    if store_name == "Penny":
        if "eletmod" in u or "életmód" in t or "recept" in t:
            return "DROP"

    # --- 2. AUCHAN (Most már a non-food szűrővel!) ---
    elif store_name == "Auchan":
        if any(x in u for x in ["bizalom", "qilive", "textil", "jatek", "kert", "auto", "adatvedelem", "tajekoztato", "nonfood", "műszaki", "elektronika"]):
            return "DROP"
        if any(x in t for x in ["nonfood", "műszaki", "elektronika"]):
            return "DROP"

    # --- 3. LIDL ---
    elif store_name == "Lidl":
        if "parkside" in u or "barkacs" in u or "nonfood" in u or "non-food" in u:
            return "DROP"
        if any(x in t for x in ["szabadidő", "utazás", "recept", "barkács"]):
            return "DROP"

    # --- 4. ALDI ---
    elif store_name == "Aldi":
        if any(x in t for x in ["utazás", "középső sor", "kert", "virág"]):
            return "DROP"

    # --- 5. TESCO ---
    elif store_name == "Tesco":
        if any(x in t for x in ["kerti", "játék", "ruha", "f&f", "mobile"]):
            return "DROP"

    # --- 6. SPAR ---
    elif store_name == "Spar":
        if "lifestyle" in t:
            return "DROP"

    # --- 7. METRO (Szigorított Nagyker Szűrő) ---
    elif store_name == "Metro":
        blacklist_words = [
            "kiskeresked", "kávé", "ázsiai", "olasz", "sztár",
            "nagyobb mennyiség", "kantin", "professzionális",
            "street food", "gasztro", "iroda", "vendéglátó",
            "horeca", "gép", "bútor", "konyha", "sirha"
        ]
        if any(b in t for b in blacklist_words):
            return "DROP"
        return "KEEP"

    return "KEEP"


# --- ÚJ: CÍM GENERÁLÓ FÜGGVÉNY (SLUG ALAPJÁN) ---
def get_slug_title(store, current_title, url):
    """
    A kért boltoknál lecseréli a címet a link utolsó, beszédes részére (slug).
    """
    final_title = current_title
    
    # --- AUCHAN: Link vége (pl. .../2026-02-19-04-04-husveti-ajanlataink) ---
    if store == "Auchan":
        slug = url.split('/')[-1] # A link utolsó része a perjel után
        if slug:
            final_title = slug # Címnek adjuk a slug-ot

    # --- ALDI: Link vége (pl. .../online_akcios_ujsag_2026_02_19_kw08_psf9y3ck) ---
    elif store == "Aldi":
        slug = url.split('/')[-1]
        if slug:
            # Regex: megkeresi a '_kw' + számok + '_' + véletlenszerű karakterek részt a string végén, és levágja
            clean_slug = re.sub(r'_kw\d+_[a-zA-Z0-9]+$', '', slug)
            final_title = clean_slug

    # --- SPAR: Link vége a 'spar/' vagy 'interspar/' stb. után ---
    # A SPAR Hunter már eleve az URL-ből generált címet adhat vissza, 
    # de itt a Master Scannerben is ráerősíthetünk, ha a listát bővítjük.
    # (Megjegyzés: A scan_spar() függvény lentebb kezeli a behívást, 
    # de ha a beérkező adat címét módosítani akarjuk, azt ott kell majd.)

    return final_title


def scan_metro():
    print("\n--- METRO Szkennelés (Szigorított) ---")
    url = "https://cdn.metro-online.com/api/catalog-filter?resolution=600&feeds=metro-nagykereskedelem&collection_id=6365&metatags%5B%5D=channel=website"
    found = []
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if 'items' in data:
            for item in data['items']:
                raw_title = item.get('name', 'Metro Katalógus')
                link = item.get('url', '')
                if not link: continue
                status = analyze_link("Metro", raw_title, link)
                if status == "KEEP":
                    print(f"[{status}] {raw_title} -> {link}")
                    # VALIDITY TÖRÖLVE
                    found.append({"store": "Metro", "title": raw_title, "url": link})
    except Exception as e:
        print(f"❌ Metro Hiba: {e}")
    return found


def scan_spar():
    print("\n--- SPAR Szkennelés (Külső modul: spar_hunter.py) ---")
    try:
        found_raw = scan_spar_only()
        found_processed = []
        
        # ITT VALÓSÍTJUK MEG A SPAR CÍM CSERÉT (URL SLUG ALAPJÁN)
        for item in found_raw:
            url = item['url']
            # Kivágjuk az utolsó részt: pl. '260212-1-spar-szorolap'
            slug = url.rstrip('/').split('/')[-1]
            
            # Frissítjük a címet a slug-ra
            item['title'] = slug
            # VALIDITY TÖRÖLVE (Biztosíték, ha a külső modul beletette volna)
            item.pop('validity', None)

            found_processed.append(item)
            
        print(f"✅ Master Scanner átvette és átnevezte a SPAR adatokat: {len(found_processed)} db")
        return found_processed
    except Exception as e:
        print(f"❌ Hiba a külső SPAR modul futtatásakor: {e}")
        return []


def scan_auchan():
    print("\n--- AUCHAN Szkennelés (Selenium-alapú 'Jövő heti' támogatással) ---")
    url = "https://www.auchan.hu/katalogusok"
    found = []
    
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    wait = WebDriverWait(driver, 15)

    try:
        driver.get(url)
        time.sleep(3)

        try:
            cookie_btn = wait.until(EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler")))
            cookie_btn.click()
        except: pass

        source_aktualis = driver.page_source

        print("🔎 'Jövő heti katalógusok' fül aktiválása...")
        try:
            next_btns = driver.find_elements(By.XPATH, "//*[contains(text(), 'Jövő heti katalógusok')]")
            clicked = False
            for btn in next_btns:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(1)
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                    print("✅ Jövő heti fül aktív.")
                    break
                except: continue
            
            if clicked:
                time.sleep(4) 
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                time.sleep(2)
                source_jovoheti = driver.page_source
            else:
                source_jovoheti = ""
        except:
            source_jovoheti = ""

        full_text = (source_aktualis + source_jovoheti).replace(r'\/', '/')
        found_raw_links = set()
        found_raw_links.update(re.findall(r'(https?://reklamujsag\.auchan\.hu/online-katalogusok/[^"\'\s<>]+)', full_text))
        for m in re.findall(r'(/online-katalogusok/[^"\'\s<>]+)', full_text):
            found_raw_links.add(f"https://reklamujsag.auchan.hu{m}")

        seen_links = set()
        for full_link in found_raw_links:
            full_link = full_link.rstrip('/').rstrip("'").rstrip('"').split('?')[0]
            if full_link in seen_links: continue

            # --- EREDETI CÍM GENERÁLÁS (CSAK LOGOLÁSHOZ) ---
            slug = full_link.split('/')[-1]
            title_match = re.search(r'\d{4}-\d{2}-\d{2}-(.+)', slug)
            clean_title = title_match.group(1).replace('-', ' ').title() if title_match else slug.replace('-', ' ').title()
            title = f"Auchan {clean_title}"

            status = analyze_link("Auchan", title, full_link)
            if status == "KEEP":
                # --- ÚJ: CÍM CSERÉJE A LINK VÉGÉRE (SLUG) ---
                better_title = get_slug_title("Auchan", title, full_link)
                
                print(f"[{status}] {better_title} -> {full_link}")
                # VALIDITY TÖRÖLVE
                found.append({"store": "Auchan", "title": better_title, "url": full_link})
                seen_links.add(full_link)

    except Exception as e:
        print(f"❌ Auchan Hiba: {e}")
    finally:
        driver.quit()
    return found


def scan_penny():
    print("\n--- PENNY Szkennelés ---")
    url = "https://www.penny.hu/reklamujsag"
    headers = {'User-Agent': 'Mozilla/5.0'}
    found = []
    processed_ids = set()
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        script = soup.find('script', id='__NUXT_DATA__')
        if script:
            matches = re.findall(r'https:[^"\'\s]*rewe\.co\.at[^"\'\s]*', script.string)
            for raw_link in matches:
                clean_link = raw_link.replace('\\u002F', '/').replace('\\/', '/')
                if '"' in clean_link: clean_link = clean_link.split('"')[0]
                
                base_url = clean_link.split('?')[0]
                if base_url not in processed_ids and ".jpg" not in base_url:
                    processed_ids.add(base_url)
                    
                    # --- ÚJ: PENNY URL ALAPÚ CÍMGENERÁLÁS ---
                    title = "Penny Akciós Újság"
                    if "eletmod" in clean_link:
                        title = "Penny Életmód"
                    else:
                        # Kinyerjük a 202608 formátumot
                        match = re.search(r'/(\d{4})(\d{2})/', clean_link)
                        if match:
                            year, week = match.groups()
                            title = f"Penny Akciós Újság {int(week)}. heti ({year}{week})"
                    
                    status = analyze_link("Penny", title, clean_link)
                    if status == "KEEP":
                        print(f"[{status}] {title} -> {clean_link}")
                        # VALIDITY TÖRÖLVE
                        found.append({"store": "Penny", "title": title, "url": clean_link})
    except Exception as e:
        print(f"❌ Penny Hiba: {e}")
    return found


def scan_lidl():
    print("\n--- LIDL Szkennelés (Visszaállítva Requests-re) ---")
    url = "https://www.lidl.hu/c/szorolap/s10013623"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'}
    seen = set()
    found = []
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        for flyer in soup.find_all('a', class_='flyer'):
            link = flyer.get('href')
            if not link: continue
            if not link.startswith('http'): link = f"https://www.lidl.hu{link}"
            
            raw_title = flyer.find(class_='flyer__title')
            
            # --- ÚJ: LIDL URL ALAPÚ CÍMGENERÁLÁS ---
            match = re.search(r'/ujsag/([^/]+)', link)
            if match:
                slug = match.group(1)
                # Eltávolítjuk a végéről a '-2026' vagy hasonló évszámot ha van
                slug = re.sub(r'-\d{4}$', '', slug)
                title = f"Lidl {slug}"
            else:
                title = raw_title.get_text(strip=True) if raw_title else "Lidl Újság"
            
            if link not in seen:
                status = analyze_link("Lidl", title, link)
                if status == "KEEP":
                    print(f"[{status}] {title} -> {link}")
                    # VALIDITY TÖRÖLVE
                    found.append({"store": "Lidl", "title": title, "url": link})
                seen.add(link)
    except Exception as e:
        print(f"❌ Lidl Hiba: {e}")
    return found


def scan_tesco():
    print("\n--- TESCO Szkennelés ---")
    url = "https://www.tesco.hu/akciok/katalogusok/"
    seen = set()
    found = []
    try:
        response = cffi_requests.get(url, impersonate="chrome110", headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'tesco-ujsag' in href and ('hipermarket' in href or 'szupermarket' in href):
                full_url = href if href.startswith('http') else f"https://www.tesco.hu{href}"
                
                # --- ÚJ: TESCO URL ALAPÚ CÍMGENERÁLÁS ---
                match = re.search(r'/katalogusok/([^/]+/[^/]+)', full_url)
                if match:
                    title = f"Tesco {match.group(1)}"
                else:
                    title = "Tesco Hipermarket" if "hipermarket" in href else "Tesco Szupermarket"
                
                if full_url not in seen:
                    status = analyze_link("Tesco", title, full_url)
                    if status == "KEEP":
                        print(f"[{status}] {title} -> {full_url}")
                        # VALIDITY TÖRÖLVE
                        found.append({"store": "Tesco", "title": title, "url": full_url})
                    seen.add(full_url)
    except Exception as e:
        print(f"❌ Tesco Hiba: {e}")
    return found


def scan_aldi():
    print("\n--- ALDI Szkennelés ---")
    url = "https://www.aldi.hu/hu/ajanlatok/online-akcios-ujsag.html"
    found = []
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.text, 'html.parser')
        seen = set()
        for a in soup.find_all('a', href=True):
            if 'szorolap.aldi.hu' in a['href']:
                href = a['href']
                title = a.get('title', 'Aldi Újság')
                if href not in seen:
                    status = analyze_link("Aldi", title, href)
                    if status == "KEEP":
                        # --- ÚJ: CÍM CSERÉJE A LINK VÉGÉRE (SLUG) ---
                        better_title = get_slug_title("Aldi", title, href)
                        
                        print(f"[{status}] {better_title} -> {href}")
                        # VALIDITY TÖRÖLVE
                        found.append({"store": "Aldi", "title": better_title, "url": href})
                    seen.add(href)
    except:
        pass
    return found


# ===============================================================================
# --- ÚJ: CBA & PRÍMA PDF VADÁSZ MODUL (HÁLÓZATI LEHALLGATÓVAL) ---
# ===============================================================================

def _hunt_cba_prima_pdfs(url, store_name):
    """Belső segédfüggvény: Letölti a nyers hálózati PDF linkeket a CBA/Príma weblapjáról."""
    print(f"\n🚀 {store_name} Hálózati PDF Vadászat Indul: {url}")
    
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--window-size=1920,1080")
    # A --disable-gpu és az egyedi User-Agent TÖRÖLVE, mert megölik a 3D FlipBook motorját!
    options.add_argument("--no-sandbox") # Ez kell a GitHub Actions miatt
    options.add_argument("--disable-dev-shm-usage") # Ez is kell a GitHub Actions miatt
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    pdf_links = set()
    
    try:
        driver.get(url)
        print("⏳ 1. Alap weboldal betöltése (5 mp)...")
        time.sleep(5)
        
        try:
            gombok = driver.find_elements(By.TAG_NAME, "button")
            for btn in gombok:
                txt = btn.text.lower()
                if "összes" in txt or "elfogad" in txt or "mindent" in txt:
                    driver.execute_script("arguments[0].click();", btn)
                    print("🍪 Süti ablak eltávolítva.")
                    time.sleep(2)
                    break
        except Exception as e:
            print(f"⚠️ Süti hiba (nem gond): {e}")

        print("📜 2. Újságok keresése és 'felébresztése'...")
        flipbooks = driver.find_elements(By.CSS_SELECTOR, "._3d-flip-book")
        
        if flipbooks:
            print(f"🎯 Talált flipbook modulok száma: {len(flipbooks)} db")
            for i, fb in enumerate(flipbooks):
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", fb)
                    time.sleep(1)
                    driver.execute_script("arguments[0].click();", fb)
                    print(f"   👆 {i+1}. újság megkattintva. Várakozás a hálózati forgalomra...")
                    time.sleep(4)
                except Exception as e:
                    print(f"   ⚠️ Nem sikerült a(z) {i+1}. újságot felébreszteni: {e}")
        else:
            print("❌ Nem találtam 3D Flipbook elemet az oldalon!")
        
        print("⏳ 3. Utolsó várakozás a letöltések befejezésére (10 mp)...")
        time.sleep(10)

        print("📡 4. Hálózati napló (Network log) elemzése...")
        logs = driver.get_log("performance")
        for entry in logs:
            log_message = entry.get("message")
            if log_message:
                try:
                    log_data = json.loads(log_message)
                    message = log_data.get("message", {})
                    method = message.get("method", "")
                    
                    if method in ["Network.requestWillBeSent", "Network.responseReceived"]:
                        params = message.get("params", {})
                        req_url = params.get("request", {}).get("url", "")
                        res_url = params.get("response", {}).get("url", "")
                        
                        for u in [req_url, res_url]:
                            if u and ".pdf" in u.lower():
                                pdf_links.add(u)
                except:
                    pass
                    
        if pdf_links:
             print(f"🎉 SIKER! {len(pdf_links)} db PDF linket találtunk a hálózaton!")
        else:
             print("❌ Üres kézzel tértünk vissza, nincs PDF a naplóban.")
                    
    except Exception as e:
        print(f"❌ Végzetes hiba a(z) {store_name} PDF vadászatnál: {e}")
    finally:
        driver.quit()
        
    return pdf_links


def scan_cba_combined():
    print("\n--- CBA / Príma Szkennelés (PDF Vadász + Automata Dátumszűrő) ---")
    found = []
    today = datetime.date.today()
    
    targets = [
        ("CBA", "https://cba.hu/aktualis-ajanlataink/"),
        ("CBA Príma", "https://prima.hu/aktualis-ajanlataink/")
    ]
    
    for store_name, url in targets:
        pdfs = _hunt_cba_prima_pdfs(url, store_name)
        
        for pdf_url in sorted(pdfs):
            title = pdf_url.split('/')[-1] 
            is_expired = False
            
            date_match = re.search(r'-(\d{2})(\d{2})\.pdf', pdf_url, re.IGNORECASE)
            if date_match:
                month = int(date_match.group(1))
                day = int(date_match.group(2))
                year = today.year
                
                year_match = re.search(r'/(\d{4})/\d{2}/', pdf_url)
                if year_match:
                    year = int(year_match.group(1))
                    
                try:
                    end_date = datetime.date(year, month, day)
                    if end_date < today:
                        is_expired = True
                except:
                    pass 
            
            if is_expired:
                print(f"[DROP] Lejárt PDF eldobva: {title}")
            else:
                print(f"[KEEP] {store_name} ({title}) -> {pdf_url}")
                found.append({
                    "store": store_name,
                    "title": title,
                    "url": pdf_url
                })
                
    return found

# =============================================================================
# 2. RÉSZ: COOP MISSZIÓ (Selenium)
# =============================================================================

def fresh_start(driver, wait):
    """Újratölti az oldalt, hogy tiszta lappal induljon (Aktuális fül)."""
    print("\n🔄 Oldal újratöltése (Clean State)...")
    driver.get("https://www.coop.hu/ajanlatkereso/")
    time.sleep(3)

    # Süti kezelése minden egyes újratöltésnél
    try:
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Összes süti')]"))).click()
        print("🍪 Sütik törölve.")
    except:
        # Ha nincs gomb, biztosra megyünk a JS törléssel
        driver.execute_script("document.querySelectorAll('.cookie-bar, #cookie-consent').forEach(el => el.remove());")

    # Megnyitjuk a boltválasztót
    wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Válasszon Coop üzletet')]"))).click()
    time.sleep(1)


# 1. SZOLNOK (Dupla kör: Szolnok ABC + Híd ABC)
def scan_szolnok(driver, wait, results):
    print("📍 SZOLNOK BEVETÉS INDUL...")
    fresh_start(driver, wait)

    # 1. BOLT: 170.SZ.SZOLNOK (Csak Aktuális)
    ActionChains(driver).send_keys(Keys.TAB).perform()
    time.sleep(0.5)
    driver.switch_to.active_element.send_keys("5000" + Keys.ENTER)
    time.sleep(5)

    target_1 = "170.SZ.SZOLNOK"
    print(f"🔎 [1/2] Bolt: {target_1}")
    bolt1 = wait.until(EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{target_1}')]")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bolt1)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", bolt1)
    time.sleep(6)

    driver.execute_script("window.scrollBy(0, 700);")
    time.sleep(3)

    print("🎯 Szolnok ABC: Aktuális...")
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["szolnok_abc"]["aktualis_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()

    # --- ÚJRAINDÍTÁS A MÁSODIK BOLT ELŐTT ---
    fresh_start(driver, wait)

    # 2. BOLT: HÍD ABC (Aktuális + Jövő heti)
    ActionChains(driver).send_keys(Keys.TAB).perform()
    time.sleep(0.5)
    driver.switch_to.active_element.send_keys("5000" + Keys.ENTER)
    time.sleep(5)

    target_2 = "HÍD ABC"
    print(f"🔎 [2/2] Bolt: {target_2}")
    bolt2 = wait.until(EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{target_2}')]")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bolt2)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", bolt2)
    time.sleep(6)

    driver.execute_script("window.scrollBy(0, 700);")
    time.sleep(3)

    print("🎯 Híd ABC: Aktuális...")
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["hid_abc"]["aktualis_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)

    print("🎯 Híd ABC: Jövő heti...")
    driver.execute_script("document.elementFromPoint(825, 294).click();")  # Gomb
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 600).click();")  # Újság
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["hid_abc"]["jovoheti_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


# 2. KECSKEMÉT
def scan_kecskemet(driver, wait, results):
    print("📍 KECSKEMÉT BEVETÉS INDUL...")
    fresh_start(driver, wait)

    ActionChains(driver).send_keys(Keys.TAB).perform()
    time.sleep(0.5)
    driver.switch_to.active_element.send_keys("6000" + Keys.ENTER)
    time.sleep(5)

    print("🔎 Bolt: SZÉCHENYIVÁROSI...")
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'SZÉCHENYIVÁROSI')]")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bolt)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)

    driver.execute_script("window.scrollBy(0, 700);")
    time.sleep(3)

    print("🎯 Aktuális...")
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["kecskemet"]["aktualis_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)

    print("🎯 Jövő heti...")
    driver.execute_script("document.elementFromPoint(825, 294).click();")
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 600).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["kecskemet"]["jovoheti_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


# 3. DEBRECEN
def scan_debrecen(driver, wait, results):
    print("📍 DEBRECEN BEVETÉS INDUL...")
    fresh_start(driver, wait)

    ActionChains(driver).send_keys(Keys.TAB).perform()
    time.sleep(0.5)
    driver.switch_to.active_element.send_keys("4032" + Keys.ENTER)
    time.sleep(5)

    target = "51. SZ. ÉLELMISZERBOLT"
    print(f"🔎 Bolt: {target}")
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{target}')]")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bolt)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)

    driver.execute_script("window.scrollBy(0, 700);")
    time.sleep(3)

    print("🎯 Aktuális...")
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["debrecen"]["aktualis_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)

    print("🎯 Jövő heti...")
    driver.execute_script("document.elementFromPoint(825, 294).click();")
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 600).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["debrecen"]["jovoheti_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


# 4. PÉCS
def scan_pecs(driver, wait, results):
    print("📍 PÉCS BEVETÉS INDUL...")
    fresh_start(driver, wait)

    ActionChains(driver).send_keys(Keys.TAB).perform()
    time.sleep(0.5)
    driver.switch_to.active_element.send_keys("7623" + Keys.ENTER)
    time.sleep(5)

    target = "240 COOP ABC PÉCS"
    print(f"🔎 Bolt: {target}")
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{target}')]")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bolt)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)

    driver.execute_script("window.scrollBy(0, 700);")
    time.sleep(3)

    print("🎯 Aktuális...")
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["pecs"]["aktualis_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)

    print("🎯 Jövő heti...")
    driver.execute_script("document.elementFromPoint(825, 294).click();")
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 600).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["pecs"]["jovoheti_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


# 5. SZOMBATHELY (HERMÁN ABC - JAVÍTOTT TENYERELÉSSEL)
def scan_szombathely(driver, wait, results):
    print("📍 SZOMBATHELY BEVETÉS INDUL...")
    fresh_start(driver, wait)

    ActionChains(driver).send_keys(Keys.TAB).perform()
    time.sleep(0.5)
    driver.switch_to.active_element.send_keys("9700" + Keys.ENTER)
    time.sleep(5)

    target = "HERMÁN ABC"
    print(f"🔎 Bolt: {target}")
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{target}')]")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bolt)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)

    driver.execute_script("window.scrollBy(0, 700);")
    time.sleep(3)

    # --- JAVÍTVA: Y=126 helyett Y=500 (Újság közepe) ---
    print("🎯 Aktuális (X:493, Y:500 - TENYERELÉS)...")
    driver.execute_script("document.elementFromPoint(493, 500).click();")
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["szombathely"]["aktualis_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)

    # --- JAVÍTVA: Gomb: Y=126, Újság: Y=500 ---
    print("🎯 Jövő heti (Gomb: X:823 Y:126 | Újság: X:823 Y:500)...")
    driver.execute_script("document.elementFromPoint(823, 126).click();")  # FÜL KIVÁLASZTÁSA
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(823, 500).click();")  # TENYERELÉS AZ ÚJSÁGRA
    time.sleep(6)

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for f in iframes:
        src = f.get_attribute("src")
        if src and "katalogus" in src:
            results["szombathely"]["jovoheti_link"] = src
            break

    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


# ===============================================================================
# FŐVEZÉRLŐ (EGYESÍTETT)
# ===============================================================================

def main():
    print("=== MASTER SCANNER: FLAYER SCANNER + COOP ALL-IN-ONE ===")

    all_flyers = []

    # --- 1. HAGYOMÁNYOS BOLTOK ---
    # !!! FONTOS: SPAR KERÜLT ELŐRE !!!
    # Most a külső modult hívja meg
    all_flyers.extend(scan_spar())
    
    all_flyers.extend(scan_penny())
    all_flyers.extend(scan_lidl())
    all_flyers.extend(scan_metro())
    all_flyers.extend(scan_tesco())
    all_flyers.extend(scan_auchan())
    all_flyers.extend(scan_aldi())
    all_flyers.extend(scan_cba_combined())

    # --- 2. COOP MISSZIÓ (Selenium) ---
    print("\n🚀 COOP MISSZIÓ INDUL (HÁTTÉRBEN/HEADLESS)...")

    coop_results = {
        "szolnok_abc": {"aktualis_link": None},
        "hid_abc": {"aktualis_link": None, "jovoheti_link": None},
        "kecskemet": {"aktualis_link": None, "jovoheti_link": None},
        "debrecen": {"aktualis_link": None, "jovoheti_link": None},
        "pecs": {"aktualis_link": None, "jovoheti_link": None},
        "szombathely": {"aktualis_link": None, "jovoheti_link": None}
    }

    opts = Options()
    opts.add_argument("--window-size=1920,1080")

    # --- FEJLESZTÉS: HÁTTÉRBEN FUTTATÁS (HEADLESS) AKTIVÁLVA ---
    opts.add_argument("--headless")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    wait = WebDriverWait(driver, 15)

    try:
        scan_szolnok(driver, wait, coop_results)
        scan_kecskemet(driver, wait, coop_results)
        scan_debrecen(driver, wait, coop_results)
        scan_pecs(driver, wait, coop_results)
        scan_szombathely(driver, wait, coop_results)
    except Exception as e:
        print(f"❌ Coop Hiba: {e}")
    finally:
        driver.quit()

    # --- 3. COOP EREDMÉNYEK HOZZÁADÁSA A KÖZÖS LISTÁHOZ ---
    
    for key, links in coop_results.items():
        # URL alapú névmeghatározás a kért térkép szerint
        url_to_check = links.get("aktualis_link") or links.get("jovoheti_link") or ""
        url_lower = url_to_check.lower()
        
        store_display_name = f"Coop {key}" # Alapértelmezett

        # TÉRKÉP ALKALMAZÁSA
        if "mecsek" in url_lower:
            store_display_name = "Coop Mecsek Füszért"
        elif "tisza" in url_lower or "szolnok" in url_lower:
            store_display_name = "Tisza-Coop"
        elif "alfold" in url_lower or "alföld" in url_lower or "kecskemet" in url_lower:
            store_display_name = "Alföld Pro-Coop"
        # --- JAVÍTÁS: ITT A KÉRT MÓDOSÍTÁS ---
        elif "hetforras" in url_lower or "hétforrás" in url_lower or "szombathely" in url_lower:
            store_display_name = "Coop Hétforrás"
        elif "eszak-kelet" in url_lower or "debrecen" in url_lower or "miskolc" in url_lower:
            store_display_name = "Észak-Kelet Pro-Coop"
        elif "honi" in url_lower:
            store_display_name = "Honi-Coop"
        elif "polus" in url_lower or "pólus" in url_lower:
            store_display_name = "Pólus-Coop"

        # TISZTÍTÓ SZABÁLY (Zrt, Kft gyilkos)
        for bad_suffix in ["Zrt.", "Zrt", "Kft.", "Kft", "Kereskedelmi"]:
            store_display_name = store_display_name.replace(bad_suffix, "").strip()

        # --- ÚJ: CÍM CSERÉJE COOP-NÁL IS (URL SLUG ALAPJÁN) ---
        if links.get("aktualis_link"):
            url = links["aktualis_link"]
            # Kivágjuk a link utolsó részét (pl. coop-tisza-szorolap-2026-februar-3-het-szuper-plusz)
            # Figyelem: ha a link '/' jellel végződik, az utolsó elem üres lehet, ezért rstrip kell
            slug = url.rstrip('/').split('/')[-1]
            if slug:
                final_title = slug
            else:
                final_title = "Aktuális"

            all_flyers.append({
                "store": store_display_name,
                "title": final_title,
                "url": url
                # VALIDITY TÖRÖLVE
            })
            print(f"[COOP] {store_display_name} ({final_title}) hozzáadva.")

        if links.get("jovoheti_link"):
            url = links["jovoheti_link"]
            slug = url.rstrip('/').split('/')[-1]
            if slug:
                final_title = slug
            else:
                final_title = "Jövő heti"

            all_flyers.append({
                "store": store_display_name,
                "title": final_title,
                "url": url
                # VALIDITY TÖRÖLVE
            })
            print(f"[COOP] {store_display_name} ({final_title}) hozzáadva.")

    # --- 4. MENTÉS ---
    final_json = {
        "last_updated": str(datetime.datetime.now()),
        "flyers": all_flyers
    }

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=4)

    print(f"\n💾 SIKER! Összesen {len(all_flyers)} db újság linkje (Hagyományos + Coop + SPAR) mentve ide: {OUTPUT_FILE}")
    print("Most ellenőrizd a JSON-t, és ha jó, indítsd a Feldolgozó Robotot!")


if __name__ == "__main__":
    main()


