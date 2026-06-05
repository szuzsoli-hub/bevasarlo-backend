import json
import re
import datetime
import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = 'spar_flyers.json'

def scan_spar_only():
    print("=== 🎯 SPAR LINKVADÁSZ (JSON-LD Keresés) ===")
    url = "https://www.spar.hu/ajanlatok"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    found_flyers = []
    today = datetime.date.today()

    try:
        print(f"📡 Kapcsolódás (requests): {url} ...")
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')

        # JSON-LD script tag megkeresése
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

        if not catalog_data:
            print("❌ Nem találtam OfferCatalog JSON-LD adatot!")
            return found_flyers

        items = catalog_data.get('itemListElement', [])
        print(f"🔎 Talált újságok száma a JSON-LD-ben: {len(items)} db")

        seen_urls = set()

        for item in items:
            url_item = item.get('url', '')
            name = item.get('name', '')
            start_date_str = item.get('startDate', '')
            end_date_str = item.get('endDate', '')

            if not url_item:
                continue

            # Teljes URL összerakása
            if url_item.startswith('/'):
                url_item = f"https://www.spar.hu{url_item}"

            if url_item in seen_urls:
                continue

            # PDF linkek kiszűrése
            if '.pdf' in url_item.lower():
                continue

            # Lejárt újságok kiszűrése endDate alapján
            if end_date_str:
                try:
                    end_date = datetime.date.fromisoformat(end_date_str)
                    if end_date < today:
                        print(f"⛔ LEJÁRT ({end_date_str}): {name}")
                        continue
                except:
                    pass

            # --- SPAR kategória alapú szűrés ---
            # 1. INTERSPAR Nyár → non-food → DROP
            if '/ajanlatok/interspar/' in url_item and 'szorolap' not in url_item.lower():
                print(f"🚫 NON-FOOD (INTERSPAR nem szórólap): {name}")
                continue

            # 2. Szellem újságok — 1208-sp-web típusú linkek kiszűrése
            if '1208-sp-web' in url_item or (not start_date_str and not end_date_str):
                print(f"👻 SZELLEM ÚJSÁG: {name}")
                continue

            # Érvényesség string
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
