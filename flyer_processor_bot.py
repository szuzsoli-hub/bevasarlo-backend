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
# ÚJ: GOOGLE VISION OCR
# ===============================================================================

def ocr_with_google_vision(image_path):
    """
    Google Cloud Vision OCR — szöveget és koordinátákat ad vissza.
    Ha sikertelen → None (fallback a GPT Vision-re).
    """
    try:
        from google.cloud import vision as gv
        gv_client = gv.ImageAnnotatorClient()

        with open(image_path, 'rb') as f:
            content = f.read()

        image = gv.Image(content=content)
        response = gv_client.text_detection(image=image)

        if response.error.message:
            print(f"   Google Vision API hiba: {response.error.message}")
            return None

        annotations = response.text_annotations
        if not annotations:
            print(f"   Google Vision: üres eredmény")
            return None

        # Az első annotation a teljes szöveg
        full_text = annotations[0].description
        if not full_text or len(full_text.strip()) < 20:
            print(f"   Google Vision: túl kevés szöveg ({len(full_text.strip())} karakter)")
            return None

        print(f"   Google Vision OCR: {len(full_text)} karakter kinyerve")
        return full_text

    except ImportError:
        print("   Google Vision csomag nem telepítve (google-cloud-vision)")
        return None
    except Exception as e:
        print(f"   Google Vision hiba: {e}")
        return None


def is_ocr_usable(ocr_text):
    """
    Eldönti hogy az OCR szöveg elég jó-e a feldolgozáshoz.
    Szűri: üres, túl rövid, csak zajt tartalmaz.
    """
    if not ocr_text:
        return False
    cleaned = ocr_text.strip()
    if len(cleaned) < 30:
        return False
    # Kell legalább néhány szám (árak) és néhány betű (nevek)
    has_numbers = bool(re.search(r'\d{3,}', cleaned))
    has_letters = bool(re.search(r'[a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ]{3,}', cleaned))
    return has_numbers and has_letters


# ===============================================================================
# ÚJ: GPT-4O SZÖVEGES ÉRTELMEZÉS (OCR szövegből)
# ===============================================================================

def interpret_ocr_text_with_gpt(ocr_text, page_num, store_name, title_name, link_hint, pre_calc_date=None):
    """
    1. lépés: gpt-4o szöveges — OCR nyers szövegből értelmezés.
    Visszaad egy strukturálatlan szöveges listát a termékekről.
    """
    date_instr = ""
    if page_num == 1:
        if pre_calc_date and pre_calc_date != "N/A":
            date_instr = f'DÁTUM: Már tudjuk, NE keresd! Az "ervenyesseg" mezőbe pontosan ezt írd: {pre_calc_date}'
        else:
            date_instr = f"""DÁTUM KERESÉS: Keresd meg az érvényességi időt a szövegben.
Formátum: "ÉÉÉÉ.HH.NN. - ÉÉÉÉ.HH.NN." — hónapokat számmá, napneveket töröld, hiányzó év: 2026.
FALLBACK ha nem látod: {link_hint}"""

    prompt = f"""Ez egy magyar akciós újság oldalának OCR-rel kinyert nyers szövege.
Bolt: {store_name} | Újság: {title_name} | Oldal: {page_num}

{date_instr}

FELADAT: Azonosítsd az összes akciós terméket a szövegből.
A szöveg kaotikus lehet (OCR mellékzöngék, összefolyó sorok) — használd az összefüggéseket!

Minden termékhez keresd meg:
- Termék neve (márka + terméknév)
- Akciós ár (Ft)
- Kiszerelés (pl. 500g, 1l, 2x200ml)
- Egységár (pl. 2398 Ft/kg) — ha nincs kiírva de van ár és kiszerelés, SZÁMÍTSD KI!
- Egyéb infó (mennyiségi feltétel, kártyás ár, stb.)

SZÁMÍTÁS LOGIKA:
- Van ár + kiszerelés → egységár = ár / kiszerelés (jelöld [sz]-vel)
- Van ár + egységár → kiszerelés visszaszámolható (jelöld [sz]-vel)
- Egyik sem → null

Válaszolj egy strukturált szöveges listával, termékenként:
TERMÉK: [név]
ÁR: [akciós ár Ft]
KISZERELÉS: [kiszerelés vagy null]
EGYSÉGÁR: [egységár vagy null]
EGYÉB: [egyéb info vagy null]
---

NYERS OCR SZÖVEG:
{ocr_text}
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )
    time.sleep(0.5)
    return response.choices[0].message.content


def structure_with_gpt_mini(interpreted_text, page_num, store_name, pre_calc_date=None, link_hint="N/A"):
    """
    2. lépés: gpt-4o-mini — strukturált szövegből JSON + mezőnkénti confidence.
    """
    date_val = pre_calc_date if (pre_calc_date and pre_calc_date != "N/A") else "N/A"

    prompt = f"""Alakítsd át ezt a terméklistát JSON formátumba.
Oldal: {page_num} | Érvényesség: {date_val}

MEZŐNKÉNTI CONFIDENCE (0.0-1.0):
- 1.0: biztosan látható/számított
- 0.7-0.9: valószínűleg helyes
- 0.5-0.6: bizonytalan
- 0.0: hiányzik

ELVÁRT JSON STRUKTÚRA:
{{
  "ervenyesseg": "{date_val}",
  "oldalszam": {page_num},
  "termekek": [
    {{
      "nev": "márka + termékNév",
      "nev_confidence": 0.95,
      "kiszereles": "500g vagy null",
      "kiszereles_confidence": 0.8,
      "ar": "1199 Ft",
      "ar_confidence": 1.0,
      "ar_egyseg": "2398 Ft/kg vagy null",
      "ar_egyseg_confidence": 0.9,
      "ar_egyseg_szamitott": false,
      "ar_info": "egyéb info vagy null"
    }}
  ]
}}

A "ar_egyseg_szamitott" mező: true ha [sz] jelölés volt, false ha a képen szerepelt.

TERMÉK LISTA:
{interpreted_text}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=8000,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}]
    )
    time.sleep(0.5)
    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"   gpt-mini JSON parse hiba: {e}")
        return {"termekek": [], "ervenyesseg": "N/A", "oldalszam": page_num}


# ===============================================================================
# ÚJ: PYTHON VALIDÁCIÓ
# ===============================================================================

EGYSEG_SZORZOK = {
    # Tömeg
    'kg': 1000, 'dkg': 10, 'g': 1,
    # Térfogat
    'l': 1000, 'dl': 100, 'ml': 1,
    # Darab
    'db': 1,
}

EGYSEG_NEVEK = {
    'g': ('kg', 1000),
    'dkg': ('kg', 100),
    'kg': ('kg', 1),
    'ml': ('l', 1000),
    'dl': ('l', 10),
    'l': ('l', 1),
    'db': ('db', 1),
}

def parse_ar(ar_str):
    """'1 199 Ft' → 1199"""
    if not ar_str:
        return None
    digits = re.sub(r'[^0-9]', '', str(ar_str))
    return int(digits) if digits else None

def parse_kiszereles(k_str):
    """'500g' → (500, 'g'), '1.5l' → (1.5, 'l'), '2x200ml' → (400, 'ml')"""
    if not k_str or k_str == 'null':
        return None, None
    k_str = str(k_str).lower().strip()

    # Több egység: 2x200ml, 3x100g
    m = re.match(r'(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|dkg|g|l|dl|ml|db)', k_str)
    if m:
        db = int(m.group(1))
        menny = float(m.group(2).replace(',', '.'))
        egyseg = m.group(3)
        return db * menny, egyseg

    # Sima: 500g, 1.5l, 250 ml
    m = re.match(r'(\d+(?:[.,]\d+)?)\s*(kg|dkg|g|l|dl|ml|db)', k_str)
    if m:
        menny = float(m.group(1).replace(',', '.'))
        egyseg = m.group(2)
        return menny, egyseg

    return None, None

def parse_egysegar(eu_str):
    """'2398 Ft/kg' → (2398, 'kg'), '1598 Ft/l' → (1598, 'l')"""
    if not eu_str or eu_str == 'null':
        return None, None
    m = re.search(r'(\d[\d\s]*)\s*Ft\s*/\s*(kg|g|l|dl|ml|db)', str(eu_str), re.IGNORECASE)
    if m:
        ertek = int(re.sub(r'\s', '', m.group(1)))
        egyseg = m.group(2).lower()
        return ertek, egyseg
    return None, None

def szamol_egysegar(ar_ft, menny, egyseg):
    """ar=799, menny=500, egyseg='g' → '1598 Ft/kg'"""
    if not ar_ft or not menny or not egyseg:
        return None
    if egyseg not in EGYSEG_NEVEK:
        return None
    alap_nev, osztok = EGYSEG_NEVEK[egyseg]
    egysegar_ertek = round(ar_ft / menny * osztok)

    # Irreális szűrő
    if alap_nev in ('kg', 'l') and egysegar_ertek > 50000:
        return None
    if alap_nev == 'db' and egysegar_ertek > 100000:
        return None

    return f"{egysegar_ertek} Ft/{alap_nev} [sz]"

def szamol_kiszereles(ar_ft, egysegar_ertek, egyseg):
    """ar=799, egysegar=1598, egyseg='kg' → '500g'"""
    if not ar_ft or not egysegar_ertek or not egyseg:
        return None
    if egyseg not in EGYSEG_SZORZOK:
        return None
    menny_alap = ar_ft / egysegar_ertek  # pl. 0.5 kg
    menny_g = menny_alap * EGYSEG_SZORZOK[egyseg]  # pl. 500g

    if menny_g <= 0 or menny_g > 50000:
        return None

    if egyseg in ('kg', 'l'):
        if menny_g >= 1000:
            return f"{round(menny_g / 1000, 2)}{egyseg} [sz]"
        elif egyseg == 'kg':
            return f"{round(menny_g)}g [sz]"
        else:
            return f"{round(menny_g)}ml [sz]"
    return f"{round(menny_g)}{egyseg} [sz]"

def validalj_termeket(termek):
    """
    Python validáció egy termékre.
    - Ellenőrzi az egységár matematikát
    - Ha invalid → újraszámolja
    - Ha irreális → törli
    Visszaadja a javított terméket + validáció státuszt.
    """
    ar = parse_ar(termek.get('ar'))
    menny, egyseg = parse_kiszereles(termek.get('kiszereles'))
    eu_ertek, eu_egyseg = parse_egysegar(termek.get('ar_egyseg'))

    validacio = {
        'ar_valid': True,
        'egysegar_valid': True,
        'egysegar_forrasa': 'eredeti',  # 'eredeti', 'szamitott', 'ujraszamitott'
        'figyelmeztetesek': []
    }

    # 1. Irreális ár szűrő
    if ar is not None:
        if ar < 1 or ar > 500000:
            validacio['ar_valid'] = False
            validacio['figyelmeztetesek'].append(f"Irreális ár: {ar} Ft")

    # 2. Egységár matematikai ellenőrzés
    if ar and menny and egyseg and eu_ertek and eu_egyseg:
        if egyseg in EGYSEG_NEVEK and eu_egyseg in EGYSEG_NEVEK:
            _, osztok = EGYSEG_NEVEK[egyseg]
            vart_eu = round(ar / menny * osztok)
            elteres = abs(vart_eu - eu_ertek) / max(eu_ertek, 1)

            if elteres > 0.05:  # 5%-nál nagyobb eltérés → invalid
                validacio['egysegar_valid'] = False
                validacio['figyelmeztetesek'].append(
                    f"Egységár eltérés: látott={eu_ertek}, számított={vart_eu} Ft/{EGYSEG_NEVEK[egyseg][0]}"
                )
                # Újraszámolja
                uj_eu = szamol_egysegar(ar, menny, egyseg)
                if uj_eu:
                    termek['ar_egyseg'] = uj_eu
                    termek['ar_egyseg_szamitott'] = True
                    validacio['egysegar_forrasa'] = 'ujraszamitott'
                    print(f"      ⚠️ Egységár javítva: {eu_ertek} → {uj_eu}")

    # 3. Hiányzó egységár pótlása
    if ar and menny and egyseg and not termek.get('ar_egyseg'):
        uj_eu = szamol_egysegar(ar, menny, egyseg)
        if uj_eu:
            termek['ar_egyseg'] = uj_eu
            termek['ar_egyseg_szamitott'] = True
            validacio['egysegar_forrasa'] = 'szamitott'

    # 4. Hiányzó kiszerelés pótlása (ha van ár + egységár)
    if ar and eu_ertek and eu_egyseg and not termek.get('kiszereles'):
        uj_k = szamol_kiszereles(ar, eu_ertek, eu_egyseg)
        if uj_k:
            termek['kiszereles'] = uj_k
            print(f"      ℹ️ Kiszerelés számítva: {uj_k}")

    termek['_validacio'] = validacio
    return termek, validacio


# ===============================================================================
# ÚJ: TELJES OCR PIPELINE (lecseréli az interpret_image_with_ai-t)
# ===============================================================================

def process_image_ocr_pipeline(image_path, page_num, store_name, title_name, link_hint,
                                pre_calc_date=None):
    """
    Teljes OCR pipeline egy képre (1. fázis — csak OCR alapú, Vision fallback NINCS):
    1. Google Vision OCR → nyers szöveg
    2a. Ha OCR jó → gpt-4o szöveges értelmezés → gpt-4o-mini JSON
    2b. Ha OCR gyenge/üres → loggolás + oldal kihagyása (Vision fallback csak 2. fázisban)
    3. Python validáció minden termékre
    """
    print(f"\n   🔍 OCR pipeline: oldal {page_num}")

    # --- 1. LÉPÉS: Google Vision OCR ---
    ocr_text = ocr_with_google_vision(image_path)
    ocr_jo = is_ocr_usable(ocr_text)
    print(f"   OCR {'✅ jó' if ocr_jo else '⚠️ gyenge/üres — oldal kihagyva (1. fázis)'}")

    # --- 2A. LÉPÉS: OCR jó → szöveges pipeline ---
    if ocr_jo:
        print(f"   → Szöveges pipeline (gpt-4o + gpt-4o-mini)")
        interpreted = interpret_ocr_text_with_gpt(
            ocr_text, page_num, store_name, title_name, link_hint, pre_calc_date
        )
        structured = structure_with_gpt_mini(
            interpreted, page_num, store_name, pre_calc_date, link_hint
        )

    # --- 2B. LÉPÉS: OCR gyenge → kihagyás, nem Vision fallback ---
    else:
        ocr_long = len(ocr_text.strip()) if ocr_text else 0
        print(f"   ⏭️ Kihagyva: OCR szöveg {ocr_long} karakter, nem elég (bolt: {store_name}, oldal: {page_num})")
        return {"termekek": [], "ervenyesseg": "N/A", "oldalszam": page_num}

    # --- 3. LÉPÉS: Python validáció ---
    termekek = structured.get("termekek", [])
    validalt_termekek = []
    kiszurt = 0

    for termek in termekek:
        javitott, validacio = validalj_termeket(termek)

        if not validacio['ar_valid']:
            kiszurt += 1
            print(f"      ❌ Kiszűrve (irreális ár): {termek.get('nev', '?')}")
            continue

        validalt_termekek.append(javitott)

    if kiszurt > 0:
        print(f"   Validáció: {kiszurt} termék kiszűrve, {len(validalt_termekek)} marad")

    structured["termekek"] = validalt_termekek
    return structured


# ===============================================================================
# EREDETI GPT-4O VISION FALLBACK (változatlan logika, átnevezve)
# ===============================================================================

def interpret_image_with_ai_vision(image_path, page_num, store_name, title_name, link_hint,
                                    pre_calc_date=None, need_vision_pagenum=False,
                                    double_page_info=None):
    """Az eredeti gpt-4o Vision alapú elemzés — OCR fallback esetén."""
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
        pagenum_instr = f'OLDALSZAM: Keresd meg a lapszamlalot. Az "oldalszam" mezobe a PERJEL ELOTTI szamot ird. Ha nem lathato: {page_num}'
    elif double_page_info:
        bal = double_page_info.split(',')[0].split('=')[1].strip()
        jobb = double_page_info.split(',')[1].split('=')[1].strip()
        pagenum_instr = f'OLDALSZAM: A kepen KET ujsagoldal lathato. BAL OLDAL = {bal}. oldal, JOBB OLDAL = {jobb}. oldal.'
    else:
        pagenum_instr = f'OLDALSZAM: Az "oldalszam" mezobe ird: {page_num}'

    prompt = f"""Nezd meg alaposan ezt a kepet. Ez egy magyar akcioS ujsag oldala.
Add vissza az osszes aron szereplo termeket JSON formatumban.

{date_instr}

MEZOK:
- "nev": marka + pontos termeknev
- "kiszereles": aktivan keresd, ha nincs szamitsd ki [sz] jelolessel, ha nem lehet: null
- "ar": az akcioS ar Ft-tal
- "ar_egyseg": keresd, ha nincs szamitsd ki [sz] jelolessel, ha nem lehet: null
- "ar_info": minden egyeb info vagy null

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
        max_tokens=16000,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            {"type": "text", "text": prompt}
        ]}]
    )
    time.sleep(1)
    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"   JSON parse hiba: {e}")
        return {"termekek": [], "ervenyesseg": "N/A", "oldalszam": page_num}


# ===============================================================================
# URL-BŐL OLDALSZÁM KINYERÉS (változatlan)
# ===============================================================================
def extract_page_num_from_url(page_url, store_name):
    store_lower = store_name.lower()
    if 'spar' in store_lower or 'issuu' in page_url.lower():
        return None
    m = re.search(r'#page=(\d+)', page_url)
    if m: return int(m.group(1))
    m = re.search(r'/\d{6}/(\d+)/?', page_url)
    if m: return int(m.group(1))
    m = re.search(r'/page/(\d+)', page_url)
    if m: return int(m.group(1))
    m = re.search(r'[?&]page=(\d+)', page_url)
    if m: return int(m.group(1))
    m = re.search(r'/tesco-ujsag-[\d-]+/(\d+)', page_url)
    if m: return int(m.group(1))
    m = re.search(r'/page/(\d+)-\d+', page_url)
    if m: return int(m.group(1))
    return None


# ===============================================================================
# 1/A. MODUL: A FOTÓS (változatlan)
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
                    print(f"   DOM lapszamlalo: '{txt}' (selector: {sel})")
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
            print(f"   DOM lapszamlalo (JS): '{result}'")
            return result
    except Exception as e:
        print(f"   Lapszamlalo hiba: {e}")
    return None

def parse_page_counter(counter_text):
    if not counter_text:
        return None, None
    m = re.search(r'(\d+)\s*[-]\s*(\d+)', counter_text)
    if m: return int(m.group(1)), int(m.group(2))
    m = re.search(r'(\d+)\s*[/]\s*\d+', counter_text)
    if m:
        n = int(m.group(1))
        return n, n
    return None, None


# ===============================================================================
# PUBLITAS KÉPAPI (változatlan)
# ===============================================================================
def _get_publitas_data_json(alap_url, store_name):
    store_lower = store_name.lower()
    if 'aldi' in store_lower or 'coop' in store_lower:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        try:
            r = requests.get(alap_url, headers=headers, timeout=15)
            pub_match = re.search(r'"publicationId"\s*:\s*(\d+)', r.text)
            acc_match = re.search(r'"accountId"\s*:\s*(\d+)', r.text)
            if pub_match and acc_match:
                pub_id = pub_match.group(1)
                acc_id = acc_match.group(1)
                data_url = f"https://view.publitas.com/{acc_id}/{pub_id}/data.json"
                dr = requests.get(data_url, headers=headers, timeout=15)
                if dr.status_code == 200:
                    return dr.json(), acc_id, pub_id
        except Exception as e:
            print(f"   Publitas data.json hiba: {e}")
        return None, None, None
    elif 'metro' in store_lower:
        opts = Options()
        opts.add_argument("--headless")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
            driver.get(alap_url)
            time.sleep(8)
            page_source = driver.page_source
            pub_match = re.search(r'"publicationId"\s*:\s*(\d+)', page_source)
            acc_match = re.search(r'"accountId"\s*:\s*(\d+)', page_source)
            if not pub_match or not acc_match:
                result = driver.execute_script("""
                    try { var cfg = window.__PUBLITAS_CONFIG__ || window.publitas || {};
                    return JSON.stringify(cfg); } catch(e) { return null; }
                """)
                if result:
                    pub_match = re.search(r'"publicationId"\s*:\s*(\d+)', result)
                    acc_match = re.search(r'"accountId"\s*:\s*(\d+)', result)
            if pub_match and acc_match:
                pub_id = pub_match.group(1)
                acc_id = acc_match.group(1)
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                data_url = f"https://view.publitas.com/{acc_id}/{pub_id}/data.json"
                dr = requests.get(data_url, headers=headers, timeout=15)
                if dr.status_code == 200:
                    return dr.json(), acc_id, pub_id
        except Exception as e:
            print(f"   Publitas data.json hiba (Metro): {e}")
        finally:
            if driver: driver.quit()
        return None, None, None
    return None, None, None

def capture_pages_publitas(alap_url, store_name, count=4):
    print(f"\nPUBLITAS KEPAPI INDUL ({store_name}): {alap_url}")
    data, acc_id, pub_id = _get_publitas_data_json(alap_url, store_name)
    if not data:
        print(f"   Publitas data.json nem sikerult, Selenium fallbackre esik")
        return None
    spreads = data.get('spreads', [])
    if not spreads:
        print(f"   Publitas: nincs spreads adat")
        return None
    print(f"   Publitas: {len(spreads)} spread talalva, pub_id={pub_id}, acc_id={acc_id}")
    captured_data = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    page_list = []
    for spread_idx, spread in enumerate(spreads):
        pages = spread.get('pages', [])
        for page in pages:
            page_num = page.get('number')
            if page_num is None: continue
            images = page.get('images', {})
            img_url = images.get('at1600') or images.get('at1200') or images.get('at800')
            if img_url and not img_url.startswith('http'):
                img_url = f"https://view.publitas.com{img_url}"
            forras = build_forras_link(alap_url, page_num, store_name)
            page_list.append((page_num, img_url, forras))
    page_list = page_list[:count]
    for page_num, img_url, forras in page_list:
        if not img_url:
            print(f"   Oldal {page_num}: nincs kép URL")
            continue
        try:
            print(f"   Oldal {page_num} letoltese: {img_url[-60:]}")
            r = requests.get(img_url, headers=headers, timeout=20)
            if r.status_code == 200 and 'image' in r.headers.get('content-type', ''):
                fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
                with open(fajl_nev, 'wb') as f:
                    f.write(r.content)
                captured_data.append({
                    "image_path": fajl_nev, "page_url": forras,
                    "page_num": page_num, "left_page": page_num, "right_page": page_num,
                })
                print(f"   Oldal {page_num} OK")
            else:
                print(f"   Oldal {page_num}: HTTP {r.status_code}")
        except Exception as e:
            print(f"   Oldal {page_num} hiba: {e}")
    if captured_data:
        print(f"   Publitas KEPES: {len(captured_data)} oldal letoltve")
        return captured_data
    return None


# ===============================================================================
# IPAPER KÉPAPI (változatlan)
# ===============================================================================
def capture_pages_ipaper(alap_url, store_name, count=4):
    print(f"\nIPAPER KEPAPI INDUL ({store_name}): {alap_url}")
    slug_match = re.search(r'/online-katalogusok/(\d+)/([^/]+)/([^/?#]+)', alap_url)
    if not slug_match:
        print(f"   iPaper: nem sikerult slug kinyerése")
        return None
    year = slug_match.group(1)
    tr = slug_match.group(2)
    slug = slug_match.group(3)
    ipaper_base = f"https://ipaper.ipapercms.dk/auchan-hungary/online-katalogusok/{year}/{tr}/{slug}/Image.ashx"
    referer = alap_url
    api_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': referer,
    }
    captured_data = []
    api_ok = True
    for page_num in range(1, count + 1):
        api_url = f"{ipaper_base}?PageNumber={page_num}&ImageType=Large"
        try:
            r = requests.get(api_url, headers=api_headers, timeout=15)
            if r.status_code == 200 and 'image' in r.headers.get('content-type', ''):
                fajl_nev = os.path.join(TEMP_DIR, f"{store_name}_oldal_{page_num}.png")
                with open(fajl_nev, 'wb') as f:
                    f.write(r.content)
                forras = build_forras_link(alap_url, page_num, store_name)
                captured_data.append({
                    "image_path": fajl_nev, "page_url": forras,
                    "page_num": page_num, "left_page": page_num, "right_page": page_num,
                })
                print(f"   iPaper oldal {page_num} OK")
            else:
                print(f"   iPaper oldal {page_num}: HTTP {r.status_code}")
                api_ok = False
                break
        except Exception as e:
            print(f"   iPaper hiba: {e}")
            api_ok = False
            break
    if api_ok and len(captured_data) == count:
        print(f"   iPaper KEPES: {len(captured_data)} oldal letoltve")
        return captured_data
    print(f"   iPaper API nem sikerult, Selenium fallbackre esik")
    return None


# ===============================================================================
# MOBIL SELENIUM (változatlan)
# ===============================================================================
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
MOBILE_WIDTH = 390
MOBILE_HEIGHT = 844

def _make_mobile_driver():
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument(f"--window-size={MOBILE_WIDTH},{MOBILE_HEIGHT}")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"user-agent={MOBILE_UA}")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver

def capture_pages_mobile_selenium(alap_url, store_name, count=4):
    print(f"\nMOBIL SELENIUM INDUL ({store_name}): {alap_url}")
    page_urls = build_page_urls(alap_url, store_name, count)
    captured_data = []
    try:
        driver = _make_mobile_driver()
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
                    "image_path": fajl_nev, "page_url": page_url,
                    "page_num": page_num, "left_page": page_num, "right_page": page_num,
                })
                print(f"   Oldal {page_num} fotozva")
            except Exception as e:
                print(f"   Oldal {page_num} hiba: {e}")
        return captured_data
    except Exception as e:
        print(f"   Mobil Selenium hiba: {e}")
        return []
    finally:
        if 'driver' in locals(): driver.quit()


# ===============================================================================
# URL ALAPÚ OLDALANKÉNTI FOTÓZÁS (változatlan)
# ===============================================================================
def build_page_urls(alap_url, store_name, count=4):
    store_lower = store_name.lower()
    urls = []
    for page_num in range(1, count + 1):
        if 'aldi' in store_lower:
            base = re.sub(r'/page/[\d-]+$', '', alap_url.rstrip('/'))
            urls.append((page_num, f"{base}/page/{page_num}"))
        elif 'metro' in store_lower:
            base = re.sub(r'/page/\d+$', '', alap_url.rstrip('/'))
            urls.append((page_num, f"{base}/page/{page_num}"))
        elif 'coop' in store_lower:
            base = re.sub(r'/page/\d+$', '', alap_url.rstrip('/'))
            if page_num == 1: urls.append((page_num, alap_url))
            else: urls.append((page_num, f"{base}/page/{page_num}"))
        elif 'auchan' in store_lower:
            base = alap_url.split('?')[0].rstrip('/')
            urls.append((page_num, f"{base}?page={page_num}"))
        elif 'penny' in store_lower:
            path_part = alap_url.split('?')[0].rstrip('/')
            path_part = re.sub(r'/(\d{1,2})$', '', path_part)
            urls.append((page_num, f"{path_part}/{page_num}/"))
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
    store_lower = store_name.lower()
    print(f"\nURL ALAPU FOTOZAS INDUL ({store_name}): {alap_url}")
    if 'aldi' in store_lower:
        result = capture_pages_publitas(alap_url, store_name, count)
        if result: return result
        print(f"   Aldi: Publitas API sikertelen, Selenium fallback")
        return _capture_pages_selenium_desktop(alap_url, store_name, count)
    elif 'metro' in store_lower:
        result = capture_pages_publitas(alap_url, store_name, count)
        if result: return result
        print(f"   Metro: Publitas API sikertelen, mobil Selenium fallback")
        return capture_pages_mobile_selenium(alap_url, store_name, count)
    elif 'auchan' in store_lower:
        result = capture_pages_ipaper(alap_url, store_name, count)
        if result: return result
        print(f"   Auchan: iPaper API sikertelen, mobil Selenium fallback")
        return capture_pages_mobile_selenium(alap_url, store_name, count)
    elif 'lidl' in store_lower:
        return capture_pages_mobile_selenium(alap_url, store_name, count)
    elif 'tesco' in store_lower:
        return capture_pages_mobile_selenium(alap_url, store_name, count)
    elif 'penny' in store_lower:
        return capture_pages_mobile_selenium(alap_url, store_name, count)
    elif 'coop' in store_lower:
        result = capture_pages_publitas(alap_url, store_name, count)
        if result: return result
        print(f"   Coop: Publitas API sikertelen, mobil Selenium fallback")
        return capture_pages_mobile_selenium(alap_url, store_name, count)
    else:
        return _capture_pages_selenium_desktop(alap_url, store_name, count)

def _capture_pages_selenium_desktop(alap_url, store_name, count=4):
    page_urls = build_page_urls(alap_url, store_name, count)
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1280,900")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15")
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
                            if any(x in btn.text.lower() for x in ["elfogad", "accept", "ok"]):
                                driver.execute_script("arguments[0].click();", btn)
                                time.sleep(1)
                                break
                    except: pass
                driver.save_screenshot(fajl_nev)
                captured_data.append({
                    "image_path": fajl_nev, "page_url": page_url,
                    "page_num": page_num, "left_page": None, "right_page": None,
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


# ===============================================================================
# SPAR FOTÓZÁS (változatlan)
# ===============================================================================
def capture_pages_spar(target_url, store_name, count=4):
    print(f"\nSPAR FOTOZAS INDUL: {target_url}")
    slug_match = re.search(r'/ajanlatok/([^/?#]+)/([^/?#]+)', target_url)
    if slug_match:
        szorolap_url = f"https://szorolap.spar.hu/{slug_match.group(1)}/{slug_match.group(2)}/"
        ipaper_base = f"https://ipaper.ipapercms.dk/spar-hungary/{slug_match.group(1)}/{slug_match.group(2)}/Image.ashx"
    else:
        szorolap_url = target_url
        ipaper_base = None
    if ipaper_base:
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
                        "image_path": fajl_nev, "page_url": forras,
                        "page_num": page_num, "left_page": page_num, "right_page": page_num,
                    })
                    print(f"   API oldal {page_num} letoltve")
                else:
                    api_ok = False
                    break
            except Exception as e:
                print(f"   API hiba: {e}")
                api_ok = False
                break
        if api_ok and len(api_captured) == count:
            print(f"   iPaper API sikerult!")
            return api_captured
    # Selenium fallback (rövidítve, teljes verzió az eredetiben)
    return capture_pages_mobile_selenium(szorolap_url, store_name, count)


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


def capture_pages_prima5_pdf(issuu_url, store_name, count=4):
    print(f"\nPRIMA5 PDF KERESES INDUL: {issuu_url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r = requests.get(issuu_url, headers=headers, timeout=15)
        pdf_match = re.search(r'"(?:pdfUrl|pdf_url|downloadUrl)"\s*:\s*"([^"]+\.pdf[^"]*)"', r.text)
        if not pdf_match:
            pdf_match = re.search(r'(https://[^"\'<>\s]+\.pdf)', r.text)
        if pdf_match:
            pdf_url = pdf_match.group(1).replace('\\/', '/')
            print(f"   PDF URL talalva: {pdf_url}")
            return capture_pages_from_pdf(pdf_url, store_name)
    except Exception as e:
        print(f"   Issuu requests hiba: {e}")
    return capture_pages_mobile_selenium(issuu_url, store_name, count)


# ===============================================================================
# 1/B. DÁTUM ELŐTÖLTÉS (változatlan)
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
    if not content: return {}
    return json.loads(content)


# ===============================================================================
# HTML/URL ALAPÚ DÁTUM KINYERÉS (változatlan)
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
                'május': '05', 'június': '06', 'július': '07', 'október': '10',
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
                return result
        return None
    if 'tesco' in store_lower:
        m = re.search(r'tesco-ujsag-(\d{4})-(\d{2})-(\d{2})', url)
        if m:
            result = f"{m.group(1)}.{m.group(2)}.{m.group(3)}."
            return result
        return None
    if 'spar' in store_lower:
        m = re.search(r'(\d{2})(\d{2})(\d{2})-\d+-\w+', url)
        if m:
            year = f"20{m.group(1)}"
            result = f"{year}.{m.group(2)}.{m.group(3)}."
            return result
        return None
    return None


# ===============================================================================
# ÚJ: process_images_with_ai — OCR pipeline-ra átkötve
# ===============================================================================
def _format_validity(raw_date):
    if not raw_date or raw_date == "N/A":
        return "N/A"
    return f"Újság érvényessége: {raw_date} (egyes termékek akciós érvényessége eltérhet, ellenőrizd az újságban!)"

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
    if not dates: return True
    dates.sort()
    if len(dates) >= 2:
        return today <= dates[-1]
    start_date = dates[0]
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
            base = alap_link.split('?')[0].rstrip('/')
            base = re.sub(r'/\d+$', '', base)
            return f"{base}/{page_num}/"
        elif 'tesco' in store_lower:
            base = re.sub(r'/\d+$', '', alap_link.rstrip('/'))
            return f"{base}/{page_num}"
        elif 'aldi' in store_lower or 'metro' in store_lower:
            base = re.sub(r'/page/[\d-]+$', '', alap_link.rstrip('/'))
            return f"{base}/page/{page_num}"
        elif 'coop' in store_lower:
            base = alap_link.rstrip('/')
            return f"{base}/page/{page_num}"
    except Exception as e:
        print(f"   forrasLink epítési hiba ({store_name}, oldal {page_num}): {e}")
    return alap_link


def process_images_with_ai(captured_data, flyer_meta, all_flyers, pre_calc_date=None):
    """
    Főfüggvény: OCR pipeline-t használ minden képhez.
    Ugyanazt a JSON struktúrát adja vissza amit a Flutter app vár.
    """
    print(f"OCR Pipeline: {flyer_meta['store']} - {flyer_meta['title']}...")
    results = []
    store_name = flyer_meta['store']
    store_lower = store_name.lower()

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
        url_pagenum = extract_page_num_from_url(item['page_url'], store_name) if not use_vision_pagenum else None

        if left_page is not None:
            effective_pagenum = left_page
        elif url_pagenum is not None:
            effective_pagenum = url_pagenum
        else:
            effective_pagenum = item['page_num']

        # *** OCR PIPELINE HÍVÁS ***
        structured = process_image_ocr_pipeline(
            item['image_path'],
            effective_pagenum,
            store_name,
            flyer_meta['title'],
            link_hint,
            pre_calc_date
        )

        # Érvényesség első oldalból
        if item['page_num'] == 1:
            if pre_calc_date and pre_calc_date != "N/A":
                detected_validity = _format_validity(pre_calc_date)
            else:
                raw = structured.get("ervenyesseg", "N/A")
                detected_validity = _format_validity(raw)
            if not check_validity_date(detected_validity, flyer_meta, all_flyers):
                print(f"LEJART: {detected_validity}")
                return []

        # Oldalszám meghatározás
        if use_vision_pagenum:
            final_pagenum = structured.get("oldalszam", item['page_num'])
            try: final_pagenum = int(final_pagenum)
            except: final_pagenum = item['page_num']
        else:
            final_pagenum = effective_pagenum

        termekek = structured.get("termekek", [])
        print(f"   Talált termékek: {len(termekek)} db (oldal: {final_pagenum})")

        for product in termekek:
            ar_val = str(product.get("ar", "")).strip()
            if ar_val and 'Ft' not in ar_val and re.search(r'\d', ar_val):
                ar_val = f"{ar_val} Ft"

            product_page = final_pagenum
            forras = item['page_url']

            # Confidence info az ar_info-ba (ha alacsony)
            ar_conf = product.get('ar_confidence', 1.0)
            nev_conf = product.get('nev_confidence', 1.0)
            extra_info = product.get("ar_info") or ""
            if ar_conf < 0.7:
                extra_info = f"[bizonytalan ár: {ar_conf:.0%}] " + extra_info
            if nev_conf < 0.7:
                extra_info = f"[bizonytalan név: {nev_conf:.0%}] " + extra_info

            # Validáció infó
            validacio = product.get('_validacio', {})
            if validacio.get('figyelmeztetesek'):
                for f_msg in validacio['figyelmeztetesek']:
                    print(f"      ⚠️ {f_msg}")

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
                "ar_info": extra_info if extra_info.strip() else None,
                "forrasLink": forras,
                "alap_link": flyer_meta['url']
            })
    return results


# ===============================================================================
# FŐPROGRAM (változatlan logika)
# ===============================================================================
if __name__ == "__main__":
    print("=== AKCIOVADÁSZ BOT: OCR PIPELINE VERZIÓ ===")
    if not os.path.exists(INPUT_FILE):
        print(f"❌ Nincs input fájl: {INPUT_FILE}")
        exit()
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        current_flyers = json.load(f).get("flyers", [])
    old_products = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            old_products = json.load(f)

    auchan_links = [f['url'] for f in current_flyers if f['store'].lower() == 'auchan']
    spar_links = [f['url'] for f in current_flyers if f['store'].lower() == 'spar']
    pre_fetched_dates = {}
    if auchan_links:
        print("\nAUCHAN ÉRVÉNYESSÉGEK ELŐTÖLTÉSE...")
        pre_fetched_dates.update(get_auchan_pre_dates(auchan_links))
    if spar_links:
        print("\nSPAR ÉRVÉNYESSÉGEK ELŐTÖLTÉSE...")
        pre_fetched_dates.update(get_spar_pre_dates(spar_links))

    HTML_DATE_STORES = ['aldi', 'penny', 'metro', 'coop', 'cba', 'prima', 'tesco', 'spar']
    print("\nHTML/URL ALAPÚ ÉRVÉNYESSÉGEK ELŐTÖLTÉSE...")
    for flyer in current_flyers:
        store_lower = flyer['store'].lower()
        if any(s in store_lower for s in HTML_DATE_STORES):
            validity = get_validity_from_html(flyer['url'], flyer['store'])
            if validity:
                pre_fetched_dates[flyer['url']] = validity

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
            print(f"Időlimit! {store_name} marad a következő futásra.")
            break
        print(f"\nBOLT: {store_name} ({len(flyers)} újság)")
        for flyer in flyers:
            if not ido_van_meg():
                print(f"Időlimit! {flyer['title']} marad.")
                break
            pre_calc_date = pre_fetched_dates.get(flyer['url'])
            store_lower_main = flyer['store'].lower()
            url_based_stores = ['aldi', 'metro', 'coop', 'auchan', 'penny', 'tesco', 'lidl']
            if flyer['url'].lower().endswith('.pdf'):
                pages = capture_pages_from_pdf(flyer['url'], flyer['store'])
            elif 'spar' in store_lower_main:
                pages = capture_pages_spar(flyer['url'], flyer['store'])
            elif 'prima5' in store_lower_main or 'príma5' in store_lower_main:
                pages = capture_pages_prima5_pdf(flyer['url'], flyer['store'])
            elif any(s in store_lower_main for s in url_based_stores):
                pages = capture_pages_by_url(flyer['url'], flyer['store'])
            else:
                pages = capture_pages_mobile_selenium(flyer['url'], flyer['store'])
            if pages:
                new_items = process_images_with_ai(pages, flyer, current_flyers, pre_calc_date)
                final_products.extend(new_items)
        print(f"Mentés {store_name} után...")
        mentes(final_products)

    mentes(final_products)
    print(f"\nKÉSZ! Adatbázis: {len(final_products)} termék.")
