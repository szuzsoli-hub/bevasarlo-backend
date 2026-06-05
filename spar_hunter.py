import json
import re
import datetime
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

OUTPUT_FILE = 'spar_flyers.json'

def scan_spar_only():
    print("=== 🎯 SPAR LINKVADÁSZ (Selenium Keresés) ===")
    url = "https://www.spar.hu/ajanlatok"
    found_flyers = []

    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

    try:
        print(f"📡 Kapcsolódás (Selenium): {url} ...")
        driver.get(url)

        print("⏳ Várakozás az újságkártyák betöltésére (WebDriverWait, max 20 mp)...")
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 
                    "a[href*='szorolap'], a[href*='ajanlatok/spar'], a[href*='ajanlatok/interspar'], a[href*='ajanlatok/egyeb']"))
            )
            print("✅ Újságkártyák betöltve!")
            # Scroll az oldal aljára hogy az egyeb kártyák is betöltődjenek
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1)
        except Exception as e:
            print(f"⚠️ WebDriverWait timeout: {e}")
            time.sleep(5)

        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')

        # JSON-LD feldolgozás itt is!
        script_tags = soup.find_all('script', type='application/ld+json')
        catalog_data = None
        for script in script_tags:
            try:
                data = json.loads(script.string)
                if data.get('@type') == 'OfferCatalog':
                    catalog_data = data
                    break
            except:
                continue

        today = datetime.date.today()
        seen_urls = set()

        if catalog_data:
            print("✅ JSON-LD OfferCatalog találva, abból olvasunk!")
            items = catalog_data.get('itemListElement', [])
            print(f"🔎 Talált újságok száma: {len(items)} db")

            for item in items:
                url_item = item.get('url', '')
                name = item.get('name', '')
                end_date_str = item.get('endDate', '')
                start_date_str = item.get('startDate', '')

                if not url_item:
                    continue
                if url_item.startswith('/'):
                    url_item = f"https://www.spar.hu{url_item}"
                if url_item in seen_urls:
                    continue
                if '.pdf' in url_item.lower():
                    continue

                # Lejárt szűrés
                if end_date_str:
                    try:
                        end_date = datetime.date.fromisoformat(end_date_str)
                        if end_date < today:
                            print(f"⛔ LEJÁRT ({end_date_str}): {name}")
                            continue
                    except:
                        pass

                # INTERSPAR non-food szűrés
                if '/ajanlatok/interspar/' in url_item and 'szorolap' not in url_item.lower():
                    print(f"🚫 NON-FOOD: {name}")
                    continue

                # Szellem újság szűrés
                if not start_date_str and not end_date_str:
                    print(f"👻 SZELLEM: {name}")
                    continue

                validity_str = "Ismeretlen"
                if start_date_str and end_date_str:
                    try:
                        sd = datetime.date.fromisoformat(start_date_str)
                        ed = datetime.date.fromisoformat(end_date_str)
                        validity_str = f"{sd.strftime('%Y.%m.%d')}-{ed.strftime('%Y.%m.%d')}"
                    except:
                        pass
                elif start_date_str:
                    try:
                        sd = datetime.date.fromisoformat(start_date_str)
                        validity_str = f"{sd.strftime('%Y.%m.%d')}-tól visszavonásig"
                    except:
                        pass

                print(f"✅ TALÁLAT: {name} | {validity_str} | {url_item}")
                found_flyers.append({
                    "store": "Spar",
                    "title": url_item.rstrip('/').split('/')[-1],
                    "url": url_item,
                    "validity": validity_str
                })
                seen_urls.add(url_item)

        else:
            print("⚠️ JSON-LD nem található, fallback: <a> tagok...")
            # ... (eredeti <a> tag alapú logika marad fallbacknek)

    except Exception as e:
        print(f"❌ KRITIKUS HIBA (Selenium): {e}")
    finally:
        driver.quit()

    if found_flyers:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump({"flyers": found_flyers}, f, ensure_ascii=False, indent=4)
        print(f"\n💾 SIKER! {len(found_flyers)} db SPAR újság mentve ide: {OUTPUT_FILE}")
    else:
        print("\n⚠️ NEM TALÁLTAM ÚJSÁGOT.")

    return found_flyers


if __name__ == "__main__":
    scan_spar_only()
