import json
import re
import datetime
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup

# --- KONFIGURÁCIÓ ---
OUTPUT_FILE = 'spar_flyers.json'


def scan_spar_only():
    print("=== 🎯 SPAR LINKVADÁSZ (Selenium Keresés) ===")
    url = "https://www.spar.hu/ajanlatok"

    found_flyers = []

    # Selenium beállítások (Headless mód, mint az Auchannál)
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

    try:
        print(f"📡 Kapcsolódás (Selenium): {url} ...")
        driver.get(url)

        # Várunk pár másodpercet, hogy a JavaScript biztosan betöltse az újságokat
        print("⏳ Várakozás a kártyák betöltésére...")
        time.sleep(5)

        # Kinyerjük a JS által már legenerált, teljes HTML-t
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')

        links = soup.find_all('a', href=True)
        print(f"🔎 Talált linkek száma az oldalon: {len(links)} db")

        seen_urls = set()
        today = datetime.date.today()
        cutoff_date = today - datetime.timedelta(days=30)

        for a in links:
            raw_href = a['href']

            is_interesting = False
            if 'spar' in raw_href.lower() and ('ajanlatok' in raw_href.lower() or 'szorolap' in raw_href.lower()):
                is_interesting = True
            if not is_interesting: continue

            if "getPdf" in raw_href or ".pdf" in raw_href or "ViewPdf" in raw_href: continue

            full_url = raw_href
            if raw_href.startswith('/'): full_url = f"https://www.spar.hu{raw_href}"
            
            # --- ÚJ: A gyűjtőoldal (főoldal) kiszűrése! ---
            if full_url.rstrip('/') == "https://www.spar.hu/ajanlatok": 
                continue

            if full_url in seen_urls: continue

            # RUGALMAS DÁTUM KERESŐ
            date_match = re.search(r'(202[4-6]|2[4-6])[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])', full_url)
            validity_str = "Ismeretlen"

            if date_match:
                y_str, m_str, d_str = date_match.groups()
                try:
                    year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
                    month = int(m_str)
                    day = int(d_str)
                    flyer_date = datetime.date(year, month, day)

                    if flyer_date < cutoff_date:
                        continue  # Csak a nagyon régieket dobjuk el

                    end_date = flyer_date + datetime.timedelta(days=6)
                    validity_str = f"{flyer_date.strftime('%Y.%m.%d')}-{end_date.strftime('%Y.%m.%d')}"
                except ValueError:
                    pass

            title = "SPAR Újság"
            if "interspar" in full_url.lower():
                title = "INTERSPAR"
            elif "spar-market" in full_url.lower():
                title = "SPAR market"
            elif "spar-extra" in full_url.lower():
                title = "SPAR Partner (Extra)"

            print(f"✅ TALÁLAT: {title} | {validity_str} | {full_url}")

            found_flyers.append({
                "store": "Spar",
                "title": title,
                "url": full_url,
                "validity": validity_str
            })
            seen_urls.add(full_url)

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
