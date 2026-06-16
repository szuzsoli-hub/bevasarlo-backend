import json
import re
import datetime
import time
import base64
import os
import requests as req_lib
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

# --- KONFIGURÁCIÓ ---
OUTPUT_FILE = 'spar_flyers.json'
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


def ask_gpt_vision(driver, url):
    """GPT-4o Vision ellenőrzés — food és valid döntés"""
    try:
        print(f"   📸 GPT Vision: {url[:60]}...")
        driver.get(url)
        time.sleep(4)
        screenshot = driver.get_screenshot_as_png()
        img_b64 = base64.b64encode(screenshot).decode('utf-8')
        today_str = datetime.date.today().strftime('%Y.%m.%d')

        prompt = f"""Ez egy magyar szupermarket újság borítója. Mai dátum: {today_str}.

Kérlek válaszolj CSAK JSON formátumban, semmi más szöveg:
{{"food": true/false, "valid": true/false}}

Szabályok:
- food: true ha élelmiszer/food jellegű (grill, jégkrém, nyári étel, hús, zöldség stb.)
- food: false ha non-food (ruha, elektronika, kert, bútor, sport, barkács stb.)
- valid: true ha "visszavonásig" szerepel
- valid: true ha záró dátum még nem múlt el
- valid: false ha záró dátum már elmúlt"""

        response = req_lib.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 100,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                        {"type": "text", "text": prompt}
                    ]
                }]
            },
            timeout=30
        )

        if response.status_code == 200:
            text = response.json()['choices'][0]['message']['content'].strip()
            text = re.sub(r'```json|```', '', text).strip()
            result = json.loads(text)
            food = result.get('food', True)
            valid = result.get('valid', True)
            print(f"   🧠 Vision: food={food}, valid={valid}")
            return {"food": food, "valid": valid}
        else:
            print(f"   ⚠️ Vision API hiba: {response.status_code} - megtartjuk")
            return {"food": True, "valid": True}

    except Exception as e:
        print(f"   ⚠️ Vision hiba: {e} - megtartjuk")
        return {"food": True, "valid": True}


def scan_spar_only():
    print("=== 🎯 SPAR LINKVADÁSZ (JSON-LD alapú) ===")
    url = "https://www.spar.hu/ajanlatok"

    found_flyers = []
    today = datetime.date.today()

    def _spar_get_soup(url):
        """Spar oldal letöltése - 3 fallback úttal."""
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

        # 1. út: requests
        try:
            print(f"📡 1. út: requests -> {url}")
            r = req_lib.get(url, headers=headers, timeout=20)
            print(f"   requests HTTP: {r.status_code}")
            if r.status_code == 200:
                return BeautifulSoup(r.text, 'html.parser')
        except Exception as e:
            print(f"   requests hiba: {e}")

        # 2. út: curl_cffi
        try:
            print(f"   2. út: curl_cffi...")
            from curl_cffi import requests as cffi_req
            r2 = cffi_req.get(url, impersonate="chrome120", timeout=20)
            print(f"   curl_cffi HTTP: {r2.status_code}")
            if r2.status_code == 200:
                return BeautifulSoup(r2.text, 'html.parser')
        except Exception as e:
            print(f"   curl_cffi hiba: {e}")

        # 3. út: Selenium mobilos
        try:
            print(f"   3. út: Selenium mobilos...")
            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.chrome.options import Options
            from webdriver_manager.chrome import ChromeDriverManager
            opts = Options()
            opts.add_argument("--headless")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--window-size=390,844")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, \'webdriver\', {get: () => undefined})"
            })
            driver.get(url)
            time.sleep(8)
            src = driver.page_source
            driver.quit()
            print(f"   Selenium: oldal betöltve ({len(src)} karakter)")
            return BeautifulSoup(src, 'html.parser')
        except Exception as e:
            print(f"   Selenium hiba: {e}")

        return None

    try:
        soup = _spar_get_soup(url)
        if not soup:
            print("❌ Spar: egyik út sem működött")
            return []

        # JSON-LD keresés
        json_ld_data = None
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if data.get('@type') == 'OfferCatalog' and 'itemListElement' in data:
                    json_ld_data = data
                    break
            except:
                continue

        if not json_ld_data:
            print("❌ JSON-LD nem található az oldalon!")
            return []

        items = json_ld_data.get('itemListElement', [])
        print(f"🔎 JSON-LD-ben talált újságok száma: {len(items)} db")

        for item in items:
            name = item.get('name', '').strip()
            flyer_url = item.get('url', '').strip()
            end_date_str = item.get('endDate', '').strip()
            start_date_str = item.get('startDate', '').strip()

            if not flyer_url:
                continue

            # Szellem szűrés: üres startDate és endDate → DROP
            if not start_date_str and not end_date_str:
                print(f"👻 SZELLEM (nincs dátum): {flyer_url[-50:]}")
                continue

            # endDate ellenőrzés
            if end_date_str:
                try:
                    end_date = datetime.date.fromisoformat(end_date_str)
                    if end_date < today:
                        print(f"⛔ LEJÁRT ({end_date_str}): {flyer_url[-50:]}")
                        continue
                except:
                    pass

            # URL slug kinyerés
            slug = flyer_url.rstrip('/').split('/')[-1]

            # Érvényesség string
            validity_str = "Ismeretlen"
            if start_date_str and end_date_str:
                try:
                    start = datetime.date.fromisoformat(start_date_str)
                    end = datetime.date.fromisoformat(end_date_str)
                    validity_str = f"{start.strftime('%Y.%m.%d')}-{end.strftime('%Y.%m.%d')}"
                except:
                    validity_str = f"{start_date_str} - {end_date_str}"
            elif start_date_str:
                validity_str = f"{start_date_str}-tól"

            print(f"✅ TALÁLAT: {name} | {validity_str} | {flyer_url[-50:]}")
            found_flyers.append({
                "store": "Spar",
                "title": slug,
                "url": flyer_url,
                "validity": validity_str
            })

    except Exception as e:
        print(f"❌ KRITIKUS HIBA: {e}")

    if found_flyers:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump({"flyers": found_flyers}, f, ensure_ascii=False, indent=4)
        print(f"\n💾 SIKER! {len(found_flyers)} db SPAR újság mentve ide: {OUTPUT_FILE}")
    else:
        print("\n⚠️ NEM TALÁLTAM ÚJSÁGOT.")

    return found_flyers


if __name__ == "__main__":
    scan_spar_only()
