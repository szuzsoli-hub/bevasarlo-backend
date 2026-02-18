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
        today = datetime.date.today()
        cutoff_date = today - datetime.timedelta(days=30)

        for a in links:
            raw_href = a['href']
            is_interesting = False
            if 'spar' in raw_href.lower() and ('ajanlatok' in raw_href.lower() or 'szorolap' in raw_href.lower()):
                is_interesting = True
            
            if not is_interesting or any(x in raw_href for x in ["getPdf", ".pdf", "ViewPdf"]):
                continue

            full_url = raw_href if not raw_href.startswith('/') else f"https://www.spar.hu{raw_href}"
            
            if full_url in seen_urls:
                continue

            date_match = re.search(r'(2[4-6])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', full_url)
            validity_str = "Keresés..."
            
            if date_match:
                y_str, m_str, d_str = date_match.groups()
                try:
                    flyer_date = datetime.date(2000 + int(y_str), int(m_str), int(d_str))
                    if flyer_date < cutoff_date:
                        continue
                    end_date = flyer_date + datetime.timedelta(days=6)
                    validity_str = f"{flyer_date.strftime('%Y.%m.%d')}-{end_date.strftime('%Y.%m.%d')}"
                except ValueError:
                    continue
            else:
                continue 

            title = "SPAR Újság"
            if "interspar" in full_url.lower(): title = "INTERSPAR"
            elif "spar-market" in full_url.lower(): title = "SPAR market"
            elif "spar-extra" in full_url.lower(): title = "SPAR Partner"

            status = analyze_link("Spar", title, full_url)
            if status == "KEEP":
                print(f"[{status}] {title} ({validity_str}) -> {full_url}")
                found.append({"store": "Spar", "title": title, "url": full_url, "validity": validity_str})
                seen_urls.add(full_url)

    except Exception as e:
        print(f"❌ Spar Hiba: {e}")
    return found


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

        # 1. SÜTIK KEZELÉSE
        try:
            cookie_btn = wait.until(EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler")))
            cookie_btn.click()
        except:
            pass

        # --- A) AKTUÁLIS HÉT FORRÁSA ---
        source_aktualis = driver.page_source

        # --- B) ÁTVÁLTÁS A JÖVŐ HETI FÜLRE ---
        print("🔎 'Jövő heti katalógusok' fül aktiválása...")
        try:
            # Fallback logika: megkeressük a gombot szöveg alapján
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
                except:
                    continue
            
            if clicked:
                time.sleep(4) # Várjuk a dinamikus betöltést
                # Görgetés lefelé a lazy loading miatt
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                time.sleep(2)
                source_jovoheti = driver.page_source
            else:
                source_jovoheti = ""
        except:
            source_jovoheti = ""

        # --- C) FELDOLGOZÁS ---
        full_text = (source_aktualis + source_jovoheti).replace(r'\/', '/')
        found_raw_links = set()
        
        # Regex keresés (teljes és relatív)
        found_raw_links.update(re.findall(r'(https?://reklamujsag\.auchan\.hu/online-katalogusok/[^"\'\s<>]+)', full_text))
        for m in re.findall(r'(/online-katalogusok/[^"\'\s<>]+)', full_text):
            found_raw_links.add(f"https://reklamujsag.auchan.hu{m}")

        seen_links = set()
        for full_link in found_raw_links:
            full_link = full_link.rstrip('/').rstrip("'").rstrip('"').split('?')[0]
            if full_link in seen_links: continue

            slug = full_link.split('/')[-1]
            title_match = re.search(r'\d{4}-\d{2}-\d{2}-(.+)', slug)
            clean_title = title_match.group(1).replace('-', ' ').title() if title_match else slug.replace('-', ' ').title()
            title = f"Auchan {clean_title}"

            status = analyze_link("Auchan", title, full_link)
            if status == "KEEP":
                print(f"[{status}] {title} -> {full_link}")
                found.append({"store": "Auchan", "title": title, "url": full_link, "validity": "Keresés..."})
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
            found.append({"store": "CBA Príma", "title": "CBA Príma 5 (Szeged)", "url": url_prima, "validity": "Keresés..."})
    except: pass
    url_cba = "https://cba.hu/aktualis-ajanlataink/"
    try:
        response = cffi_requests.get(url_cba, impersonate="chrome110")
        soup = BeautifulSoup(response.text, 'html.parser')
        found_main = False
        for a in soup.find_all('a', href=True):
            href = a['href']
            if "ajanlat" in href or "akcio" in href or "catalog" in href:
                if len(href) > 20 and ("pdf" in href or "issuu" in href or "flipbook" in href):
                    found.append({"store": "CBA", "title": "CBA Akciós Újság", "url": href, "validity": "Keresés..."})
                    found_main = True
        if not found_main:
            found.append({"store": "CBA", "title": "CBA Akciós Újság", "url": url_cba, "validity": "Keresés..."})
    except: pass
    return found


# =============================================================================
# 2. RÉSZ: COOP MISSZIÓ (Selenium)
# =============================================================================

def fresh_start(driver, wait):
    driver.get("https://www.coop.hu/ajanlatkereso/")
    time.sleep(3)
    try:
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Összes süti')]"))).click()
    except:
        driver.execute_script("document.querySelectorAll('.cookie-bar, #cookie-consent').forEach(el => el.remove());")
    wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Válasszon Coop üzletet')]"))).click()
    time.sleep(1)


def scan_szolnok(driver, wait, results):
    print("📍 SZOLNOK BEVETÉS INDUL...")
    fresh_start(driver, wait)
    ActionChains(driver).send_keys(Keys.TAB).perform()
    driver.switch_to.active_element.send_keys("5000" + Keys.ENTER)
    time.sleep(5)
    bolt1 = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), '170.SZ.SZOLNOK')]")))
    driver.execute_script("arguments[0].click();", bolt1)
    time.sleep(6)
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["szolnok_abc"]["aktualis_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


def scan_kecskemet(driver, wait, results):
    print("📍 KECSKEMÉT BEVETÉS INDUL...")
    fresh_start(driver, wait)
    ActionChains(driver).send_keys(Keys.TAB).perform()
    driver.switch_to.active_element.send_keys("6000" + Keys.ENTER)
    time.sleep(5)
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'SZÉCHENYIVÁROSI')]")))
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["kecskemet"]["aktualis_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 294).click();")
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 600).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["kecskemet"]["jovoheti_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


def scan_debrecen(driver, wait, results):
    print("📍 DEBRECEN BEVETÉS INDUL...")
    fresh_start(driver, wait)
    ActionChains(driver).send_keys(Keys.TAB).perform()
    driver.switch_to.active_element.send_keys("4032" + Keys.ENTER)
    time.sleep(5)
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), '51. SZ. ÉLELMISZERBOLT')]")))
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["debrecen"]["aktualis_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 294).click();")
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 600).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["debrecen"]["jovoheti_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


def scan_pecs(driver, wait, results):
    print("📍 PÉCS BEVETÉS INDUL...")
    fresh_start(driver, wait)
    ActionChains(driver).send_keys(Keys.TAB).perform()
    driver.switch_to.active_element.send_keys("7623" + Keys.ENTER)
    time.sleep(5)
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), '240 COOP ABC PÉCS')]")))
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)
    driver.execute_script("document.elementFromPoint(400, 500).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["pecs"]["aktualis_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 294).click();")
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(825, 600).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["pecs"]["jovoheti_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


def scan_szombathely(driver, wait, results):
    print("📍 SZOMBATHELY BEVETÉS INDUL...")
    fresh_start(driver, wait)
    ActionChains(driver).send_keys(Keys.TAB).perform()
    driver.switch_to.active_element.send_keys("9700" + Keys.ENTER)
    time.sleep(5)
    bolt = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'HERMÁN ABC')]")))
    driver.execute_script("arguments[0].click();", bolt)
    time.sleep(6)
    driver.execute_script("document.elementFromPoint(493, 500).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["szombathely"]["aktualis_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(823, 126).click();")
    time.sleep(4)
    driver.execute_script("document.elementFromPoint(823, 500).click();")
    time.sleep(6)
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        src = f.get_attribute("src")
        if src and "katalogus" in src: results["szombathely"]["jovoheti_link"] = src; break
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


# ===============================================================================
# FŐVEZÉRLŐ (EGYESÍTETT)
# ===============================================================================

def main():
    print("=== MASTER SCANNER: FLAYER SCANNER + COOP ALL-IN-ONE ===")
    all_flyers = []
    all_flyers.extend(scan_penny())
    all_flyers.extend(scan_lidl())
    all_flyers.extend(scan_metro())
    all_flyers.extend(scan_tesco())
    all_flyers.extend(scan_auchan())
    all_flyers.extend(scan_spar())
    all_flyers.extend(scan_aldi())
    all_flyers.extend(scan_cba_combined())

    print("\n🚀 COOP MISSZIÓ INDUL (HEADLESS)...")
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

    for key, links in coop_results.items():
        url_to_check = links.get("aktualis_link") or links.get("jovoheti_link") or ""
        url_lower = url_to_check.lower()
        store_display_name = f"Coop {key}"
        if "mecsek" in url_lower: store_display_name = "Coop Mecsek Füszért"
        elif "tisza" in url_lower or "szolnok" in url_lower: store_display_name = "Tisza-Coop"
        elif "alfold" in url_lower or "kecskemet" in url_lower: store_display_name = "Alföld Pro-Coop"
        elif "hetforras" in url_lower or "szombathely" in url_lower: store_display_name = "Hétforrás"
        elif "eszak-kelet" in url_lower or "debrecen" in url_lower: store_display_name = "Észak-Kelet Pro-Coop"
        elif "honi" in url_lower: store_display_name = "Honi-Coop"
        elif "polus" in url_lower: store_display_name = "Pólus-Coop"

        for bad in ["Zrt.", "Zrt", "Kft.", "Kft", "Kereskedelmi"]: store_display_name = store_display_name.replace(bad, "").strip()

        if links.get("aktualis_link"):
            all_flyers.append({"store": store_display_name, "title": "Aktuális", "url": links["aktualis_link"], "validity": "Keresés..."})
        if links.get("jovoheti_link"):
            all_flyers.append({"store": store_display_name, "title": "Jövő heti", "url": links["jovoheti_link"], "validity": "Keresés..."})

    final_json = {"last_updated": str(datetime.datetime.now()), "flyers": all_flyers}
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=4)
    print(f"\n💾 SIKER! {len(all_flyers)} újság mentve ide: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
