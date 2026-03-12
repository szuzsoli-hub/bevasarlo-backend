import os
from flask import Flask, request, jsonify, Response # Response hozzáadva
from openai import OpenAI
import base64
import json
from pymongo import MongoClient
import urllib.request
from datetime import datetime, timezone, timedelta
import uuid # UUID hozzáadva a képek azonosításához
import certifi

app = Flask(__name__)

# ==============================================================================
# 🛡️ BIZTONSÁGI PAJZS (KAPUŐR)
# ==============================================================================
EXPECTED_API_KEY = "v9X$kL2#pQ8@mZ5*eR1!tY7^bN4&hW3xM9"

@app.before_request
def require_api_key():
    if request.path == '/': return
    # A /get_image/... útvonal kivételt képez, hogy az app simán betölthesse a linkeket
    if request.path.startswith('/get_image/'): return 
    client_key = request.headers.get('X-API-KEY')
    if client_key != EXPECTED_API_KEY:
        return jsonify({"error": "Hozzáférés megtagadva. Érvénytelen API kulcs!"}), 401

# ==============================================================================
# 🔒 KULCSOK ÉS ADATBÁZIS (Render Environment)
# ==============================================================================
API_KEY = os.environ.get("API_KEY")
client = OpenAI(api_key=API_KEY)

MONGO_URI = os.environ.get("MONGO_URI")
# Az SSL hiba kikerülése érdekében megadjuk neki a certifi paramétert:
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo_client["bevasarlo_adatbazis"]
kollekcio = db["listak"]
tagok_kollekcio = db["csoport_tagok"]

ai_naplo = db["ai_naplo"]
# === ÚJ TÁBLA A KÉPEK TÁROLÁSÁHOZ (Hogy ne vesszenek el) ===
kepek_kollekcio = db["termek_kepek"]

def encode_image(image_file):
    return base64.b64encode(image_file.read()).decode('utf-8')

@app.route('/', methods=['GET'])
def index():
    return "Bevasarlo Backend (Full Cloud Sync + AI + Images) is running!"

# ==============================================================================
# 📸 AI KÉPFELISMERÉS + OKOS KVÓTA RENDSZER
# ==============================================================================

def get_user_status(app_user_id):
    """Lekérdezi az aktív előfizetést ÉS a megvásárolt extra csomagokat."""
    REVENUECAT_API_KEY = "sk_eWifEVYaUmYuxmsMtQfjTVEoOKGID"
    
    url = f"https://api.revenuecat.com/v1/subscribers/{app_user_id}"
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {REVENUECAT_API_KEY}')
    req.add_header('Content-Type', 'application/json')
    
    is_pro = False
    extra_quota = 0
    
    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                data = json.loads(response.read().decode())
                subscriber = data.get("subscriber", {})
                
                # 1. Előfizetés (PRO) ellenőrzése
                entitlements = subscriber.get("entitlements", {})
                for ent_name, ent_data in entitlements.items():
                    expires_date_str = ent_data.get("expires_date")
                    if not expires_date_str:
                        is_pro = True
                        break
                    expires_date = datetime.strptime(expires_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if expires_date > datetime.now(timezone.utc):
                        is_pro = True
                        break

                # 2. Fogyóeszközök (Extra csomagok) ellenőrzése az elmúlt 30 napban
                non_subs = subscriber.get("non_subscriptions", {})
                for prod_id, purchases in non_subs.items():
                    for p in purchases:
                        p_date_str = p.get("purchase_date")
                        if p_date_str:
                            p_date = datetime.strptime(p_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                            # Ha az elmúlt 30 napban vett extra csomagot, adunk neki +100 kvótát csomagonként
                            if p_date > datetime.now(timezone.utc) - timedelta(days=30):
                                extra_quota += 100

    except Exception as e:
        print(f"🚨 RevenueCat hiba: {e}")
        
    return is_pro, extra_quota

@app.route('/analyze', methods=['POST'])
def analyze_image():
    app_user_id = request.form.get('app_user_id')
    now = datetime.now(timezone.utc)

    if not app_user_id:
         return jsonify({"error": "Hiányzó azonosító!"}), 400

    # 1. Lekérjük a státuszt (Pro-e, és van-e extra kerete)
    is_pro, extra_quota = get_user_status(app_user_id)

    if not is_pro and extra_quota == 0:
        return jsonify({
            "error": "Prémium funkció 💎\n\nAz AI képfelismerés használatához Pro előfizetés szükséges. Kérlek, válts Prémiumra a beállításokban!"
        }), 403

    # 2. Túlterhelés elleni védelem (Max 5 kérés az elmúlt 1 percben)
    one_minute_ago = now - timedelta(minutes=1)
    recent_requests = ai_naplo.count_documents({
        "app_user_id": app_user_id,
        "timestamp": {"$gte": one_minute_ago}
    })

    if recent_requests >= 5:
        return jsonify({
            "error": "Túl sok kérés! 🚦\n\nKérlek, várj egy picit (kb. 1 percet) a következő kép elemzése előtt!"
        }), 429

    # 3. Havi Kvóta ellenőrzése
    thirty_days_ago = now - timedelta(days=30)
    monthly_usage = ai_naplo.count_documents({
        "app_user_id": app_user_id,
        "timestamp": {"$gte": thirty_days_ago},
        "status": "success"
    })

    # PRO felhasználóknak 100 alapból + amit extraként vettek
    total_quota = (100 if is_pro else 0) + extra_quota

    if monthly_usage >= total_quota:
        return jsonify({
            "error": f"Kimerítetted a keretedet! 🔒\n\nElhasználtad a rendelkezésre álló {total_quota} db AI fotódat az elmúlt 30 napban. Vásárolj extra csomagot a folytatáshoz!"
        }), 403

    # 4. Képfeldolgozás az OpenAI-val
    if 'image' not in request.files: return jsonify({"error": "Nincs kép"}), 400
    image = request.files['image']
    base64_image = encode_image(image)
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """
                    Te egy profi magyar áruházi adatfeldolgozó AI vagy.
                    A feladatod: Kinyerni az adatokat egy termék fotójáról.
                    
                    A következő adatokat keresd meg és add vissza SZIGORÚAN JSON formátumban:
                    1. "product_name": A termék pontos neve (Márka + Típus).
                    2. "packaging": Kiszerelés (pl. "500 g", "1,5 l", "10 db"). Ha nincs, legyen "".
                    3. "price_single": Az 1 darabos ár. CSAK SZÁM! (pl. 1299).
                    4. "price_multi": A több darabos ár (pl. "2 db esetén"). CSAK SZÁM! Ha nincs, legyen "".
                    5. "multi_condition": A feltétel (pl. "2 db esetén"). Ha nincs, legyen "".
                    6. "unit_price": Egységár (pl. "2499 Ft/kg"). Ezt szövegesen hagyd meg.

                    Válasz formátum (JSON):
                    {
                        "product_name": "...",
                        "packaging": "...",
                        "price_single": "...",
                        "price_multi": "...",
                        "multi_condition": "...",
                        "unit_price": "..."
                    }
                    """
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Elemezd a képet és add vissza a JSON-t!"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        },
                    ],
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=300
        )
        
        # Szedjük szét az OpenAI JSON válaszát
        result_json = response.choices[0].message.content
        parsed_result = json.loads(result_json)

        # 5. Sikeres kérés naplózása
        ai_naplo.insert_one({
            "app_user_id": app_user_id,
            "timestamp": now,
            "action": "analyze_image",
            "status": "success"
        })
        monthly_usage += 1 # Hozzáadjuk a mostanit is a számlálóhoz

        # 6. Mérföldkő értesítések (25, 50, 75 elhasznált fotónál)
        maradek = total_quota - monthly_usage
        if monthly_usage in [25, 50, 75]:
            parsed_result["warning"] = f"Még {maradek} fotód maradt a havi AI keretedből!"

        # Visszaküldjük a kiegészített JSON-t a telefonnak
        return jsonify(parsed_result), 200

    except Exception as e:
        # Hibák naplózása
        ai_naplo.insert_one({
            "app_user_id": app_user_id,
            "timestamp": now,
            "action": "analyze_image",
            "status": "error",
            "error_msg": str(e)
        })
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# ☁️ KÉP TÁROLÁS ÉS KISZOLGÁLÁS (A "Saját Google Fotók")
# ==============================================================================

@app.route('/upload_image', methods=['POST'])
def upload_image():
    """Fogadja a telefonról a képet, és elmenti a MongoDB-be."""
    if 'image' not in request.files:
        return jsonify({"error": "Nincs kép a kérésben"}), 400
        
    image_file = request.files['image']
    image_data = image_file.read()
    
    # Biztonság: csak pici képeket engedünk (Max 500 KB)
    if len(image_data) > 500 * 1024:
        return jsonify({"error": "A kép túl nagy! Maximum 500KB engedélyezett."}), 400

    image_id = str(uuid.uuid4())
    
    kepek_kollekcio.insert_one({
        "image_id": image_id,
        "image_data": image_data, # Binárisan tároljuk a kisebb méret miatt
        "content_type": image_file.mimetype,
        "created_at": datetime.now(timezone.utc)
    })
    
    # Összerakjuk a publikus URL-t
    host_url = request.host_url.rstrip('/')
    image_url = f"{host_url}/get_image/{image_id}"
    
    return jsonify({"status": "success", "image_url": image_url}), 200

@app.route('/get_image/<image_id>', methods=['GET'])
def get_image(image_id):
    """Visszaadja a képet, ha egy telefon meghívja a linket."""
    kep_dok = kepek_kollekcio.find_one({"image_id": image_id})
    
    if not kep_dok:
        return "Kép nem található", 404
        
    response = Response(kep_dok["image_data"], mimetype=kep_dok["content_type"])
    
    # BÖNGÉSZŐ GYORSÍTÓTÁRAZÁS BEKAPCSOLÁSA (1 HÓNAP)
    # Ezzel elkerüljük, hogy a telefonok folyamatosan töltsék ugyanazt a képet, 
    # ha valaki állandóan megnyitja a listát. Óvja a szervert!
    response.headers['Cache-Control'] = 'public, max-age=2592000'
    return response

# ==============================================================================
# ☁️ LISTA SZINKRONIZÁCIÓ (+ KUKÁSAUTÓ)
# ==============================================================================
@app.route('/sync_list', methods=['POST'])
def sync_list():
    data = request.get_json()
    family_id = data.get('family_id')
    if not family_id: return jsonify({"error": "Nincs id"}), 400
    
    list_data = data.get('list_data')
    
    # SZEMÉTSZÁLLÍTÁS (Kukásautó) LOGIKA
    try:
        # 1. Megnézzük mi volt a régi lista a szerveren
        regi_csalad = kollekcio.find_one({"family_id": family_id})
        if regi_csalad and "list_data" in regi_csalad:
            regi_linkek = set()
            uj_linkek = set()
            
            # Kigyűjtjük a régi linkeket
            if "items" in regi_csalad["list_data"]:
                for item in regi_csalad["list_data"]["items"]:
                    unit = item.get("unit", "")
                    if ":::" in unit:
                        link = unit.split(":::")[1]
                        if "/get_image/" in link:
                            regi_linkek.add(link.split("/")[-1]) # Csak az ID-t
            
            # Kigyűjtjük az új linkeket
            if list_data and "items" in list_data:
                for item in list_data["items"]:
                    unit = item.get("unit", "")
                    if ":::" in unit:
                        link = unit.split(":::")[1]
                        if "/get_image/" in link:
                            uj_linkek.add(link.split("/")[-1])
                            
            # Ami a régiben benne volt, de az újban nincs, azt törölték a telefonon!
            torlendo_kepek = regi_linkek - uj_linkek
            for kep_id in torlendo_kepek:
                kepek_kollekcio.delete_one({"image_id": kep_id})
                print(f"🗑️ Kép törölve a szerverről (szemétszállítás): {kep_id}")
    except Exception as e:
        print(f"Szemétszállítási hiba (nem blokkolja a mentést): {e}")

    # Mentjük a listát
    kollekcio.update_one({"family_id": family_id}, 
                         {"$set": {"list_data": list_data, "timestamp": data.get('timestamp')}}, 
                         upsert=True)
    return jsonify({"status": "success"}), 200

@app.route('/get_list', methods=['GET'])
def get_list():
    family_id = request.args.get('family_id')
    csalad = kollekcio.find_one({"family_id": family_id})
    if csalad:
        return jsonify({"list_data": csalad.get("list_data"), "timestamp": csalad.get("timestamp")}), 200
    return jsonify({"error": "Nincs adat"}), 404

# ==============================================================================
# 🤝 ÚJ: CSALÁD KEZELŐ FUNKCIÓK (Ami eddig hiányzott)
# ==============================================================================

@app.route('/join_group', methods=['POST'])
def join_group():
    """Amikor valaki beírja a családi kódot."""
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id')
    user_name = data.get('user_name')

    # Elmentjük, hogy ez a felhasználó tagja lett a csoportnak
    tagok_kollekcio.update_one(
        {"family_id": family_id, "user_id": user_id},
        {"$set": {"user_name": user_name, "joined_at": data.get('timestamp')}},
        upsert=True
    )
    print(f"🤝 {user_name} csatlakozott ide: {family_id}")
    return jsonify({"status": "joined"}), 200

@app.route('/leave_group', methods=['POST'])
def leave_group():
    """Amikor valaki kilép a csoportból."""
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id')
    tagok_kollekcio.delete_one({"family_id": family_id, "user_id": user_id})
    return jsonify({"status": "left"}), 200

@app.route('/update_token', methods=['POST'])
def update_token():
    """Értesítésekhez (FCM Token) mentése."""
    data = request.get_json()
    user_id = data.get('user_id')
    fcm_token = data.get('fcm_token')
    
    # Frissítjük a felhasználó értesítési kódját
    tagok_kollekcio.update_many(
        {"user_id": user_id},
        {"$set": {"fcm_token": fcm_token}}
    )
    return jsonify({"status": "token_updated"}), 200

# ==============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

