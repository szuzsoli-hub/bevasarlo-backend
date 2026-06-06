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
    """
    Lefotózza az újság első oldalát és GPT-4o Vision-nal elemzi.
    Visszatér: dict pl. {"food": True, "valid": True}
    """
    try:
        print(f"   📸 GPT Vision ellenőrzés: {url[:60]}...")
        driver.get(url)
        time.sleep(4)
        screenshot = driver.get_screenshot_as_png()
        img_b64 = base64.b64encode(screenshot).decode('utf-8')

        today_str = datetime.date.today().strftime('%Y.%m.%d')

        prompt = f"""Ez egy magyar szupermarket újság borítója. Mai dátum: {today_str}.

Kérlek válaszolj CSAK JSON formátumban, semmi más szöveg:
{{
  "food": true/false,
  "valid": true/false
}}

Szabályok:
- food: true ha élelmiszer/food jellegű újság (grill, jégkrém, nyári étel, hús, zöldség stb.)
- food: false ha non-food (ruha, elektronika, kert, bútor, sport, barkács stb.)
- valid: true ha "visszavonásig" vagy "visszavonásáig" szerepel
- valid: true ha konkrét záró dátum van és még nem múlt el ({today_str} előtt)
- valid: false ha konkrét záró dátum van és már elmúlt"""

        response = req_lib.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{img_b64}"
                                }
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }
                ]
            },
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            text = data['choices'][0]['message']['content'].strip()
            text = re.sub(r'```json|```', '', text).strip()
            result = json.loads(text)
            food = result.get('food', True)
            valid = result.get('valid', True)
            print(f"   🧠 GPT Vision: food={food}, valid={valid}")
            return {"food": food, "valid": valid}
        else:
            print(f"   ⚠️ GPT Vision API hiba: {response.status_code}")
            return {"food": True, "valid": True}

    except Exception as e:
        print(f"   ⚠️ GPT Vision hiba: {e}")
        return {"food": True, "valid": True}


def scan_spar_only():
    print("=== 🎯 SPAR LINKVADÁSZ (Selenium + GPT Vision) ===")
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
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1)
        except Exception as e:
            print(f"⚠️ WebDriverWait timeout, folytatás: {e}")
            time.sleep(5)

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
            if not is_interesting:
                continue

            if "getPdf" in raw_href or ".pdf" in raw_href or "ViewPdf" in raw_href:
                continue

            full_url = raw_href
            if raw_href.startswith('/'):
                full_url = f"https://www.spar.hu{raw_href}"

            if full_url.rstrip('/') == "https://www.spar.hu/ajanlatok":
                continue

            if full_url in seen_urls:
                continue

            seen_urls.add(full_url)

            # --- URL dátum kinyerése ---
            date_match = re.search(r'(202[4-9]|2[4-9])[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])', full_url)
            is_old = False
            if date_match:
                y_str, m_str, d_str = date_match.groups()
                year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
                try:
                    flyer_date = datetime.date(year, int(m_str), int(d_str))
                    if flyer_date < cutoff_date:
                        is_old = True
                except:
                    pass

            # --- 1. SPAR alap szórólapok ---
            if ('/ajanlatok/spar/' in full_url or
                '/ajanlatok/spar-market/' in full_url or
                '/ajanlatok/spar-extra/' in full_url):

                if is_old:
                    print(f"🔍 Régi dátum → Vision valid check: {full_url[-50:]}")
                    result = ask_gpt_vision(driver, full_url)
                    driver.get(url)
                    time.sleep(3)
                    if not result["valid"]:
                        print(f"⛔ LEJÁRT (Vision): {full_url[-50:]}")
                        continue

                title = "SPAR Újság"
                if "spar-market" in full_url.lower():
                    title = "SPAR market"
                elif "spar-extra" in full_url.lower():
                    title = "SPAR Partner (Extra)"

                print(f"✅ TALÁLAT: {title} | {full_url}")
                found_flyers.append({
                    "store": "Spar",
                    "title": full_url.rstrip('/').split('/')[-1],
                    "url": full_url
                })
                continue

            # --- 2. INTERSPAR ---
            if '/ajanlatok/interspar/' in full_url:
                if 'szorolap' in full_url.lower():
                    # Alap INTERSPAR szórólap
                    if is_old:
                        print(f"🔍 Régi INTERSPAR szórólap → Vision valid check: {full_url[-50:]}")
                        result = ask_gpt_vision(driver, full_url)
                        driver.get(url)
                        time.sleep(3)
                        if not result["valid"]:
                            print(f"⛔ LEJÁRT (Vision): {full_url[-50:]}")
                            continue
                    print(f"✅ TALÁLAT: INTERSPAR | {full_url}")
                    found_flyers.append({
                        "store": "Spar",
                        "title": full_url.rstrip('/').split('/')[-1],
                        "url": full_url
                    })
                else:
                    # INTERSPAR nem szórólap → Vision: food + valid
                    print(f"🔍 INTERSPAR nem szórólap → Vision food+valid: {full_url[-50:]}")
                    result = ask_gpt_vision(driver, full_url)
                    driver.get(url)
                    time.sleep(3)
                    if not result["food"]:
                        print(f"🚫 NON-FOOD (Vision): {full_url[-50:]}")
                        continue
                    if not result["valid"]:
                        print(f"⛔ LEJÁRT (Vision): {full_url[-50:]}")
                        continue
                    print(f"✅ TALÁLAT: INTERSPAR (Vision OK) | {full_url}")
                    found_flyers.append({
                        "store": "Spar",
                        "title": full_url.rstrip('/').split('/')[-1],
                        "url": full_url
                    })
                continue

            # --- 3. EGYEB kategória → Vision: food + valid ---
            if '/ajanlatok/egyeb/' in full_url:
                print(f"🔍 EGYEB → Vision food+valid: {full_url[-50:]}")
                result = ask_gpt_vision(driver, full_url)
                driver.get(url)
                time.sleep(3)
                if not result["food"]:
                    print(f"🚫 NON-FOOD (Vision): {full_url[-50:]}")
                    continue
                if not result["valid"]:
                    print(f"⛔ LEJÁRT (Vision): {full_url[-50:]}")
                    continue
                print(f"✅ TALÁLAT: SPAR Egyéb (Vision OK) | {full_url}")
                found_flyers.append({
                    "store": "Spar",
                    "title": full_url.rstrip('/').split('/')[-1],
                    "url": full_url
                })
                continue

            # --- 4. Egyéb ismeretlen → kihagyva ---
            print(f"⏭️ Ismeretlen kategória, kihagyva: {full_url[-50:]}")

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
