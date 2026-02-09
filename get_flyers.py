import requests
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
import json
import os
import datetime
import re

# --- KONFIGURÁCIÓ ---
OUTPUT_FILE = 'assets/offers_test.json'


def hungarian_month_to_num(month_name):
    months = {
        'január': '01', 'február': '02', 'március': '03', 'április': '04',
        'május': '05', 'június': '06', 'július': '07', 'augusztus': '08',
        'szeptember': '09', 'október': '10', 'november': '11', 'december': '12'
    }
    return months.get(month_name.lower(), '01')


# ===============================================================================
# 1. PENNY VADÁSZ (Szövegfelismerés + Szigorú URL)
# ===============================================================================
def get_penny_flyers():
    print("--- 🛒 Penny Vadászat... ---")
    url = "https://www.penny.hu/reklamujsag"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    found_flyers = []
    links_seen = set()

    try:
        response = requests.get(url, headers=headers)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')

        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'rewe.co.at' in href or 'leaflet' in href:
                full_url = href.replace('\\u002F', '/')
                if href.startswith('/'): full_url = f"https://www.penny.hu{href}"
                if full_url not in links_seen:
                    links_seen.add(full_url)
                    text_content = link.get_text(" ", strip=True)
                    process_penny_link(full_url, found_flyers, text_content, "HTML")

        script = soup.find('script', id='__NUXT_DATA__')
        if script:
            matches = re.findall(r'https:[^"\'\s]*rewe\.co\.at[^"\'\s]*', script.string)
            for raw_link in matches:
                clean_link = raw_link.replace('\\u002F', '/').replace('\\/', '/')
                if '"' in clean_link: clean_link = clean_link.split('"')[0]
                if clean_link not in links_seen and '.jpg' not in clean_link:
                    links_seen.add(clean_link)
                    process_penny_link(clean_link, found_flyers, "", "Nuxt")
        return found_flyers
    except Exception as e:
        print(f"❌ Penny Hiba: {e}")
        return []


def process_penny_link(url, flyer_list, text_content, source):
    title = "Penny Újság"
    s_date, e_date = "N/A", "N/A"

    url_date = re.search(r'(\d{4})(\d{2})(\d{2})', url)
    if url_date:
        s_date = f"{url_date.group(1)}-{url_date.group(2)}-{url_date.group(3)}"
    elif 'eletmod' in url.lower():
        title = "Penny Életmód Katalógus"
    elif 'szezon' in url.lower():
        title = "Penny Szezonális Ajánlat"
    else:
        week_match = re.search(r'/(\d{4})(\d{2})/', url)
        if week_match:
            title = f"Penny Akciós Újság ({week_match.group(2)}. hét)"

    print(f"✅ Penny ({source}): {title} ({s_date} - {e_date})")
    flyer_list.append({"store": "Penny", "type": "online_viewer", "title": title, "url": url, "validity_start": s_date,
                       "validity_end": e_date})


# ===============================================================================
# 2. AUCHAN VADÁSZ (URL-ből Tól-Ig)
# ===============================================================================
def get_auchan_flyers():
    print("--- 🛒 Auchan Vadászat... ---")
    url = "https://auchan.hu/katalogusok"
    headers = {'User-Agent': 'Mozilla/5.0'}
    found = []
    try:
        response = requests.get(url, headers=headers)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')
        script = soup.find('script', id='__NUXT_DATA__')
        if not script: return []
        raw_data = json.loads(script.string)

        def resolve(val):
            return raw_data[val] if isinstance(val, int) and val < len(raw_data) else val

        for item in raw_data:
            if isinstance(item, dict) and 'flipbookUrl' in item:
                title = resolve(item.get('title'))
                link = resolve(item.get('flipbookUrl'))
                s_date = resolve(item.get('viewFromDate'))[:10] if resolve(item.get('viewFromDate')) else "N/A"
                e_date = resolve(item.get('viewToDate'))[:10] if resolve(item.get('viewToDate')) else "N/A"

                if link and (s_date == "N/A" or e_date == "N/A"):
                    range_match = re.search(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})', link)
                    if range_match:
                        y, m1, d1, m2, d2 = range_match.groups()
                        s_date = f"{y}-{m1}-{d1}"
                        e_date = f"{y}-{m2}-{d2}"
                    else:
                        full_range = re.findall(r'(\d{4})-(\d{2})-(\d{2})', link)
                        if len(full_range) >= 2:
                            s_date = f"{full_range[0][0]}-{full_range[0][1]}-{full_range[0][2]}"
                            e_date = f"{full_range[1][0]}-{full_range[1][1]}-{full_range[1][2]}"

                if link and 'http' in link:
                    print(f"✅ Auchan: {title} ({s_date} - {e_date})")
                    found.append({"store": "Auchan", "type": "online_viewer", "title": title, "url": link,
                                  "validity_start": s_date, "validity_end": e_date})
        return found
    except Exception as e:
        print(f"❌ Auchan Hiba: {e}")
        return []


# ===============================================================================
# 3. ALDI VADÁSZ (Doboz teljes szövegének vizsgálata)
# ===============================================================================
def get_aldi_flyers():
    print("--- 🛒 ALDI Vadászat... ---")
    url = "https://www.aldi.hu/hu/ajanlatok/online-akcios-ujsag.html"
    headers = {'User-Agent': 'Mozilla/5.0'}
    found_flyers = []
    try:
        response = requests.get(url, headers=headers)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')

        for link_elem in soup.find_all('a', href=True):
            href = link_elem['href']
            if 'szorolap.aldi.hu' in href:
                title = link_elem.get('title')
                if not title or "Megnézem" in title:
                    img = link_elem.find('img')
                    title = img.get('alt') if img else "Aldi Akciós Újság"

                s_date, e_date = "N/A", "N/A"

                parent = link_elem.find_parent('div')
                grandparent = parent.find_parent('div') if parent else None
                full_text = ""
                if parent: full_text += parent.get_text(" ", strip=True)
                if grandparent: full_text += " " + grandparent.get_text(" ", strip=True)

                dates = re.findall(r'(\d{4})\.(\d{2})\.(\d{2})', full_text)
                if dates:
                    s_date = f"{dates[0][0]}-{dates[0][1]}-{dates[0][2]}"
                    if len(dates) > 1:
                        e_date = f"{dates[1][0]}-{dates[1][1]}-{dates[1][2]}"

                if not any(f['url'] == href for f in found_flyers):
                    print(f"✅ Aldi: {title} ({s_date} - {e_date})")
                    found_flyers.append({"store": "Aldi", "type": "online_viewer", "title": title, "url": href,
                                         "validity_start": s_date, "validity_end": e_date})
        return found_flyers
    except Exception as e:
        print(f"❌ Aldi Hiba: {e}")
        return []


# ===============================================================================
# 4. LIDL VADÁSZ (Alt szöveg és Title vizsgálata)
# ===============================================================================
def get_lidl_flyers():
    print("--- 🛒 LIDL Vadászat... ---")
    url = "https://www.lidl.hu/c/szorolap/s10013623"
    headers = {'User-Agent': 'Mozilla/5.0'}
    found_flyers = []
    try:
        response = requests.get(url, headers=headers)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')

        for flyer in soup.find_all('a', class_='flyer'):
            link = flyer.get('href')
            if not link: continue
            if not link.startswith('http'): link = f"https://www.lidl.hu{link}"

            text_content = flyer.get_text(" ", strip=True)
            img = flyer.find('img')
            alt_text = img.get('alt', '') if img else ""
            search_text = f"{text_content} {alt_text}"

            title = "Lidl Akciós Újság"
            raw_title = flyer.find(class_='flyer__title')
            if raw_title: title = raw_title.get_text(strip=True)

            s_date, e_date = "N/A", "N/A"
            dates = re.findall(r'(\d{2})\.(\d{2})', search_text)

            year = datetime.datetime.now().year
            if len(dates) >= 2:
                if int(dates[0][0]) == 1 and datetime.datetime.now().month == 12: year += 1
                s_date = f"{year}-{dates[0][0]}-{dates[0][1]}"
                e_date = f"{year}-{dates[1][0]}-{dates[1][1]}"
            elif len(dates) == 1:
                if int(dates[0][0]) == 1 and datetime.datetime.now().month == 12: year += 1
                s_date = f"{year}-{dates[0][0]}-{dates[0][1]}"

            if not any(f['url'] == link for f in found_flyers):
                print(f"✅ Lidl: {title} ({s_date} - {e_date})")
                found_flyers.append(
                    {"store": "Lidl", "type": "online_viewer", "title": title, "url": link, "validity_start": s_date,
                     "validity_end": e_date})
        return found_flyers
    except Exception as e:
        print(f"❌ Lidl Hiba: {e}")
        return []


# ===============================================================================
# 5. TESCO VADÁSZ (ÉRINTETLEN) ✅
# ===============================================================================
def get_tesco_flyers():
    print("--- 🛒 Tesco Vadászat... ---")
    url = "https://www.tesco.hu/akciok/katalogusok/"
    found_flyers = []
    try:
        response = cffi_requests.get(url, impersonate="chrome110", headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'tesco-ujsag' in href and ('hipermarket' in href or 'szupermarket' in href):
                full_url = href if href.startswith('http') else f"https://www.tesco.hu{href}"
                title = "Tesco Hipermarket" if "hipermarket" in href else "Tesco Szupermarket"
                s_date, e_date = "N/A", "N/A"
                date_match = re.search(r'(\d{4}-\d{2}-\d{2})', href)
                if date_match: s_date = date_match.group(1)

                if not any(f['url'] == full_url for f in found_flyers):
                    print(f"✅ Tesco: {title} ({s_date} - {e_date})")
                    found_flyers.append({"store": "Tesco", "type": "online_viewer", "title": title, "url": full_url,
                                         "validity_start": s_date, "validity_end": e_date})
        return found_flyers
    except Exception as e:
        print(f"❌ Tesco Hiba: {e}")
        return []


# ===============================================================================
# 6. SPAR VADÁSZ (Szövegkörnyezet vizsgálata)
# ===============================================================================
def get_spar_flyers():
    print("--- 🛒 SPAR Vadászat... ---")
    url = "https://www.spar.hu/ajanlatok"
    found_flyers = []
    try:
        response = cffi_requests.get(url, impersonate="chrome110", timeout=15)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'szorolap.spar.hu' in href and 'ViewPdf.ashx' in href:
                title = "SPAR Újság"
                if "interspar" in href:
                    title = "INTERSPAR Szórólap"
                elif "spar-market" in href:
                    title = "SPAR Market"
                elif "extra" in href:
                    title = "SPAR EXTRA"

                s_date, e_date = "N/A", "N/A"

                parent = link.find_parent('div')
                if parent:
                    text = parent.get_text(" ", strip=True)
                    dates = re.findall(r'(\d{2})\.(\d{2})\.?\s*-\s*(\d{2})\.(\d{2})\.?', text)
                    if dates:
                        y = datetime.datetime.now().year
                        m1, d1, m2, d2 = dates[0]
                        s_date = f"{y}-{m1}-{d1}"
                        e_date = f"{y}-{m2}-{d2}"
                    else:
                        date_match = re.search(r'/(\d{2})(\d{2})(\d{2})', href)
                        if date_match:
                            s_date = f"20{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"

                if not any(f['url'] == href for f in found_flyers):
                    print(f"✅ SPAR: {title} ({s_date} - {e_date})")
                    found_flyers.append({"store": "SPAR", "type": "online_viewer", "title": title, "url": href,
                                         "validity_start": s_date, "validity_end": e_date})
        return found_flyers
    except Exception as e:
        print(f"❌ SPAR Hiba: {e}")
        return []


# ===============================================================================
# 7. METRO VADÁSZ (API-ból - JAVÍTVA!) 🛠️ ✅
# ===============================================================================
def get_metro_flyers():
    print("--- 🛒 METRO Vadászat... ---")
    url = "https://cdn.metro-online.com/api/catalog-filter?resolution=600&feeds=metro-nagykereskedelem&collection_id=6365&metatags%5B%5D=channel=website"
    found_flyers = []
    try:
        response = requests.get(url, timeout=15)
        if response.status_code != 200: return []
        data = response.json()
        if 'items' in data:
            for item in data['items']:
                title = item.get('name', 'Metro Katalógus')
                link = item.get('url', '')

                # JAVÍTÁS ITT: Biztonságos ellenőrzés
                s_raw = item.get('validFrom')
                e_raw = item.get('validTo')

                s_date = s_raw[:10] if s_raw else "N/A"
                e_date = e_raw[:10] if e_raw else "N/A"

                cat_label = ""
                if "horeca" in title.lower():
                    cat_label = "[HORECA] "
                elif "kisker" in title.lower():
                    cat_label = "[KISKER] "
                elif "szezon" in title.lower():
                    cat_label = "[SZEZON] "
                final_title = f"{cat_label}{title}"
                print(f"✅ METRO: {final_title} ({s_date} - {e_date})")
                found_flyers.append({"store": "Metro", "type": "online_viewer", "title": final_title, "url": link,
                                     "validity_start": s_date, "validity_end": e_date})
        return found_flyers
    except Exception as e:
        print(f"❌ Metro Hiba: {e}")
        return []


# ===============================================================================
# 8. CBA PRÍMA 5 VADÁSZ (ÉRINTETLEN - MŰKÖDIK) ✅
# ===============================================================================
def get_cba_flyers():
    print("--- 🛒 CBA Príma 5 Vadászat... ---")
    url = "https://prima5.hu/index.php/prima/akciok-katalogusok"
    found_flyers = []
    try:
        response = cffi_requests.get(url, impersonate="chrome110", timeout=15)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')
        issuu_links = []
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src', '')
            if 'issuu.com' in src: issuu_links.append(src)
        date_pattern = re.compile(r'(202\d\.\s?\d{2}\.\s?\d{2}\.)\s*-\s*(\d{2}\.\s?\d{2}\.)')
        elements_with_date = soup.find_all(string=date_pattern)
        s_date, e_date, title = "N/A", "N/A", "CBA Príma Katalógus"
        if elements_with_date:
            text = elements_with_date[0]
            match = date_pattern.search(text)
            if match:
                start_str = match.group(1).replace(" ", "")
                end_str_short = match.group(2).replace(" ", "")
                s_date = start_str.strip('.').replace('.', '-')
                year = s_date.split('-')[0]
                e_date = f"{year}-{end_str_short.strip('.').replace('.', '-')}"
                if "katalógus" in text.lower():
                    title = text.strip().split("202")[0].strip()
                    if not title: title = "Aktuális CBA Príma Katalógus"
        for link in issuu_links:
            if not any(f['url'] == link for f in found_flyers):
                print(f"✅ CBA Príma: {title} ({s_date} - {e_date})")
                found_flyers.append(
                    {"store": "CBA", "type": "online_viewer", "title": title, "url": link, "validity_start": s_date,
                     "validity_end": e_date})
        return found_flyers
    except Exception as e:
        print(f"❌ CBA Hiba: {e}")
        return []


# ===============================================================================
# 9. COOP VADÁSZ (Szöveges Tól-Ig keresés)
# ===============================================================================
def get_coop_flyers():
    print("--- 🛒 COOP Vadászat... ---")
    url = "https://www.coop.hu/ajanlatkereso/"
    found_flyers = []
    try:
        response = cffi_requests.get(url, impersonate="chrome110", timeout=15)
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']
            text_content = link.get_text(" ", strip=True)
            parent = link.find_parent('div')
            grandparent = parent.find_parent('div') if parent else None
            full_text = f"{text_content} "
            if parent: full_text += parent.get_text(" ", strip=True) + " "
            if grandparent: full_text += grandparent.get_text(" ", strip=True)

            if 'szorolap' in href or 'ajanlat' in href or 'katalogus' in href:
                title = "COOP Újság"
                if "országos" in full_text.lower():
                    title = "Coop Országos Szórólap"
                elif "regionális" in full_text.lower():
                    title = "Coop Regionális Szórólap"

                if title == "COOP Újság" and len(text_content) < 5: continue

                s_date, e_date = "N/A", "N/A"

                dates = re.findall(r'(202\d)\.\s*([a-zA-Záéíóöőúüű]+)\s*(\d{1,2})\.', full_text, re.IGNORECASE)

                if len(dates) >= 2:
                    y1, m1_name, d1 = dates[0]
                    mn1 = hungarian_month_to_num(m1_name)
                    s_date = f"{y1}-{mn1}-{d1.zfill(2)}"
                    y2, m2_name, d2 = dates[1]
                    mn2 = hungarian_month_to_num(m2_name)
                    e_date = f"{y2}-{mn2}-{d2.zfill(2)}"

                elif len(dates) == 1:
                    y1, m1_name, d1 = dates[0]
                    mn1 = hungarian_month_to_num(m1_name)
                    s_date = f"{y1}-{mn1}-{d1.zfill(2)}"

                full_url = href
                if not href.startswith('http'): full_url = f"https://www.coop.hu{href}"

                if not any(f['url'] == full_url for f in found_flyers):
                    print(f"✅ COOP: {title} ({s_date} - {e_date})")
                    found_flyers.append({"store": "COOP", "type": "online_viewer", "title": title, "url": full_url,
                                         "validity_start": s_date, "validity_end": e_date})
        return found_flyers
    except Exception as e:
        print(f"❌ COOP Hiba: {e}")
        return []


# ===============================================================================
# FŐ PROGRAM
# ===============================================================================
if __name__ == "__main__":
    final_json = {
        "last_updated": str(datetime.datetime.now()),
        "flyers": []
    }

    final_json["flyers"].extend(get_penny_flyers())
    final_json["flyers"].extend(get_auchan_flyers())
    final_json["flyers"].extend(get_aldi_flyers())
    final_json["flyers"].extend(get_lidl_flyers())
    final_json["flyers"].extend(get_tesco_flyers())
    final_json["flyers"].extend(get_spar_flyers())
    final_json["flyers"].extend(get_metro_flyers())
    final_json["flyers"].extend(get_cba_flyers())
    final_json["flyers"].extend(get_coop_flyers())

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=4)

    print(f"\n💾 EREDMÉNY: {len(final_json['flyers'])} db újság mentve ide: {OUTPUT_FILE}")