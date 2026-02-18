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

    # --- 2. AUCHAN ---
    elif store_name == "Auchan":
        if any(x in u for x in ["bizalom", "qilive", "textil", "jatek", "kert", "auto", "adatvedelem", "tajekoztato"]):
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
                    found.append({"store": "Metro", "title": raw_title, "url": link, "validity": "Keresés..."})
    except Exception as e:
        print(f"❌ Metro Hiba: {e}")
    return found


def scan_spar():
    print("\n--- SPAR Szkennelés (Javított - Relatív linkek & Dátum) ---")
    url = "https://www.spar.hu/ajanlatok"

    # ERŐS FEJLÉC
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'accept-language': 'hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7',
        'cache-control': 'max-age=0',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }

    found = []
    try:
        response = cffi_requests.get(url, impersonate="chrome124", headers=headers, timeout=20)
        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=True)
        
        seen_urls = set()
        
        # Időkapu: Csak az elmúlt 30 nap (és jövőbeli) újságok kellenek
        today = datetime.date.today()
        cutoff_date = today - datetime.timedelta(days=30)

        for a in links:
            raw_href = a['href']
            
            # --- 1. SZŰRŐ: Érdekes lehet ez a link? ---
            # Keresünk kulcsszavakat: spar, interspar, ajanlatok, szorolap
            is_interesting = False
            if 'spar' in raw_href.lower() and ('ajanlatok' in raw_href.lower() or 'szorolap' in raw_href.lower()):
                is_interesting = True
            
            if not is_interesting:
                continue

            # PDF és egyéb szemetek kizárása
            if "getPdf" in raw_href or ".pdf" in raw_href or "ViewPdf" in raw_href:
                continue

            # --- 2. LINK NORMALIZÁLÁS ---
            # Ha relatív link (pl. /ajanlatok/spar/...), kiegészítjük
            full_url = raw_href
            if raw_href.startswith('/'):
                full_url = f"https://www.spar.hu{raw_href}"
            
            if full_url in seen_urls:
                continue

            # --- 3. DÁTUM KINYERÉSE (YYMMDD formátum) ---
            # Keressük a 6 jegyű számot, ami dátumnak néz ki (pl. 260212)
            date_match = re.search(r'(2[4-6])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', full_url)
            
            validity_str = "Keresés..."
            
            if date_match:
                y_str, m_str, d_str = date_match.groups()
                try:
                    year = 2000 + int(y_str)
                    month = int(m_str)
                    day = int(d_str)
                    
                    flyer_date = datetime.date(year, month, day)
                    
                    # Ha túl régi, eldobjuk
                    if flyer_date < cutoff_date:
                        continue
                    
                    # Számolunk egy érvényességi időt (Start + 6 nap)
                    end_date = flyer_date + datetime.timedelta(days=6)
                    validity_str = f"{flyer_date.strftime('%Y.%m.%d')}-{end_date.strftime('%Y.%m.%d')}"
                    
                except ValueError:
                    continue # Nem valós dátum
            else:
                # Ha nincs dátum a linkben, lehet, hogy gyűjtőoldal -> kihagyjuk
                continue 

            # --- 4. CÍM GENERÁLÁS ---
            title = "SPAR Újság"
            if "interspar" in full_url.lower():
                title = "INTERSPAR"
            elif "spar-market" in full_url.lower():
                title = "SPAR market"
            elif "spar-extra" in full_url.lower():
                title = "SPAR Partner"

            # --- 5. STÁTUSZ ELLENŐRZÉS ---
            status = analyze_link("Spar", title, full_url)
            if status == "KEEP":
                print(f"[{status}] {title} ({validity_str}) -> {full_url}")
                found.append({"store": "Spar", "title": title, "url": full_url, "validity": validity_str})
                seen_urls.add(full_url)

    except Exception as e:
        print(f"❌ Spar Hiba: {e}")
    return found


def scan_auchan():
    print("\n--- AUCHAN Szkennelés (v15.0 - JSON Decode Fix) ---")
    url = "https://www.auchan.hu/katalogusok"
    found = []
    try:
        response = cffi_requests.get(url, impersonate="chrome110", timeout=15)
        # TRÜKK: Kicseréljük a JSON escape karaktereket (\/), hogy a regex megtalálja a rejtett linkeket is!
        raw_text = response.text.replace(r'\/', '/')

        found_raw_links = set()

        # 1. Teljes linkek
        matches_full = re.findall(r'(https?://reklamujsag\.auchan\.hu/online-katalogusok/[^"\'\s<>]+)', raw_text)
        for m in matches_full: found_raw_links.add(m)

        # 2. Relatív linkek (Most már a tisztított szövegben keres!)
        matches_rel = re.findall(r'(/online-katalogusok/[^"\'\s<>]+)', raw_text)
        for m in matches_rel:
            full_url = f"https://reklamujsag.auchan.hu{m}"
            found_raw_links.add(full_url)

        seen_links = set()

        for full_link in found_raw_links:
            full_link = full_link.rstrip('/').rstrip("'").rstrip('"').split('?')[0]  # Extra tisztítás

            if full_link in seen_links: continue

            # Cím generálása
            slug = full_link.split('/')[-1]
            title_match = re.search(r'\d{4}-\d{2}-\d{2}-(.+)', slug)
            if title_match:
                slug_clean = title_match.group(1)
            else:
                slug_clean = slug

            title = slug_clean.replace('-', ' ').title()
            title = f"Auchan {title}"

            status = analyze_link("Auchan", title, full_link)
            if status == "KEEP":
                print(f"[{status}] {title} -> {full_link}")
                found.append({"store": "Auchan", "title": title, "url": full_link, "validity": "Keresés..."})
                seen_links.add(full_link)

        if not seen_links:
            print("! Az Auchan struktúra megváltozott.")

    except Exception as e:
        print(f"❌ Auchan Hiba: {e}")
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
                title = "Penny Akciós Újság"
                if "eletmod" in clean_link: title = "Penny Életmód"
                base_url = clean_link.split('?')[0]
                if base_url not in processed_ids and ".jpg" not in base_url:
                    processed_ids.add(base_url)
                    status = analyze_link("Penny", title, clean_link)
                    if status == "KEEP":
                        print(f"[{status}] {title} -> {clean_link}")
                        found.append({"store": "Penny", "title": title, "url": clean_link, "validity": "Keresés..."})
    except Exception as e:
        print(f"❌ Penny Hiba: {e}")
    return found


def scan_lidl():
    print("\n--- LIDL Szkennelés (Visszaállítva Requests-re) ---")
    url = "https://www.lidl.hu/c/szorolap/s10013623"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'}
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
            title = raw_title.get_text(strip=True) if raw_title else "Lidl Újság"
            if link not in seen:
                status = analyze_link("Lidl", title, link)
                if status == "KEEP":
                    print(f"[{status}] {title} -> {link}")
                    found.append({"store": "Lidl", "title": title, "url": link, "validity": "Keresés..."})
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
                title = "Tesco Hipermarket" if "hipermarket" in href else "Tesco Szupermarket"
                if full_url not in seen:
                    status = analyze_link("Tesco", title, full_url)
                    if status == "KEEP":
                        print(f"[{status}] {title} -> {full_url}")
                        found.append({"store": "Tesco", "title": title, "url": full_url, "validity": "Keresés..."})
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
                        print(f"[{status}] {title} -> {href}")
                        found.append({"store": "Aldi", "title": title, "url": href, "validity": "Keresés..."})
                    seen.add(href)
    except:
        pass
    return found


def scan_cba_combined():
    print("\n--- CBA / Príma Szkennelés ---")
    found = []
    url_prima = "https://prima5.hu/index.php/prima/akciok-katalogusok"
    try:
        response = cffi_requests.get(url_prima, impersonate="chrome110")
        if response.status_code == 200:
            print("[KEEP] CBA Príma 5 (Szeged) -> https://prima5.hu/index.php/prima/akciok-katalogusok")
            found.append({"store": "CBA Príma", "title": "CBA Príma 5 (Szeged)",
                          "url": "https://prima5.hu/index.php/prima/akciok-katalogusok", "validity": "Keresés..."})
    except:
        pass
    url_cba = "https://cba.hu/aktualis-ajanlataink/"
    try:
        response = cffi_requests.get(url_cba, impersonate="chrome110")
        soup = BeautifulSoup(response.text, 'html.parser')
        found_main = False
        for a in soup.find_all('a', href=True):
            href = a['href']
            if "ajanlat" in href or "akcio" in href or "catalog" in href:
                if len(href) > 20 and ("pdf" in href or "issuu" in href or "flipbook" in href):
                    print(f"[KEEP] CBA Országos -> {href}")
                    found.append({"store": "CBA", "title": "CBA Akciós Újság", "url": href, "validity": "Keresés..."})
                    found_main = True
        if not found_main:
            print("[KEEP] CBA Országos Gyűjtőoldal -> https://cba.hu/aktualis-ajanlataink/")
            found.append({"store": "CBA", "title": "CBA Akciós Újság", "url": url_cba, "validity": "Keresés..."})
    except:
        pass
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
    all_flyers.extend(scan_penny())
    all_flyers.extend(scan_lidl())
    all_flyers.extend(scan_metro())
    all_flyers.extend(scan_tesco())
    all_flyers.extend(scan_auchan())
    all_flyers.extend(scan_spar())
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

    # --- 3. COOP EREDMÉNYEK HOZZÁADÁSA A KÖZÖS LISTÁHOZ (JAVÍTOTT NÉVADÁS) ---
    
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
        elif "hetforras" in url_lower or "hétforrás" in url_lower or "szombathely" in url_lower:
            store_display_name = "Hétforrás"
        elif "eszak-kelet" in url_lower or "debrecen" in url_lower or "miskolc" in url_lower:
            store_display_name = "Észak-Kelet Pro-Coop"
        elif "honi" in url_lower:
            store_display_name = "Honi-Coop"
        elif "polus" in url_lower or "pólus" in url_lower:
            store_display_name = "Pólus-Coop"

        # TISZTÍTÓ SZABÁLY (Zrt, Kft gyilkos)
        for bad_suffix in ["Zrt.", "Zrt", "Kft.", "Kft", "Kereskedelmi"]:
            store_display_name = store_display_name.replace(bad_suffix, "").strip()

        if links.get("aktualis_link"):
            all_flyers.append({
                "store": store_display_name,
                "title": "Aktuális",
                "url": links["aktualis_link"],
                "validity": "Keresés..."
            })
            print(f"[COOP] {store_display_name} (Aktuális) hozzáadva.")

        if links.get("jovoheti_link"):
            all_flyers.append({
                "store": store_display_name,
                "title": "Jövő heti",
                "url": links["jovoheti_link"],
                "validity": "Keresés..."
            })
            print(f"[COOP] {store_display_name} (Jövő heti) hozzáadva.")

    # --- 4. MENTÉS ---
    final_json = {
        "last_updated": str(datetime.datetime.now()),
        "flyers": all_flyers
    }

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=4)

    print(f"\n💾 SIKER! Összesen {len(all_flyers)} db újság linkje (Hagyományos + Coop) mentve ide: {OUTPUT_FILE}")
    print("Most ellenőrizd a JSON-t, és ha jó, indítsd a Feldolgozó Robotot!")


if __name__ == "__main__":
    main()
