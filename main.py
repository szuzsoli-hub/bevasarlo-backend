import os
from flask import Flask, request, jsonify, Response
from openai import OpenAI
import base64
import json
from pymongo import MongoClient
import urllib.request
from datetime import datetime, timezone, timedelta
import uuid
import certifi
from flask_socketio import SocketIO, join_room, leave_room, emit # <-- ÚJ: A Rádiótorony alkatrészei

app = Flask(__name__)

# === ÚJ: RÁDIÓTORONY BEKAPCSOLÁSA (JAVÍTOTT VERZIÓ) ===
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode="threading",
    manage_session=False,    # <--- EZ AZ ÉLETMENTŐ SOR!
    ping_timeout=60, 
    ping_interval=25
)

# ==============================================================================
# 🛡️ BIZTONSÁGI PAJZS (KAPUŐR)
# ==============================================================================
EXPECTED_API_KEY = "v9X$kL2#pQ8@mZ5*eR1!tY7^bN4&hW3xM9"

@app.before_request
def require_api_key():
    if request.path == '/': return
    if request.path.startswith('/get_image/'): return 
    # A Socket.IO a /socket.io/ útvonalon kommunikál, ezt is át kell engednie a pajzsnak!
    if request.path.startswith('/socket.io/'): return 
    
    client_key = request.headers.get('X-API-KEY')
    if client_key != EXPECTED_API_KEY:
        return jsonify({"error": "Hozzáférés megtagadva. Érvénytelen API kulcs!"}), 401

# ==============================================================================
# 🔒 KULCSOK ÉS ADATBÁZIS (Render Environment)
# ==============================================================================
API_KEY = os.environ.get("API_KEY")
client = OpenAI(api_key=API_KEY)

MONGO_URI = os.environ.get("MONGO_URI")
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo_client["bevasarlo_adatbazis"]
kollekcio = db["listak"]
tagok_kollekcio = db["csoport_tagok"]
ai_naplo = db["ai_naplo"]
kepek_kollekcio = db["termek_kepek"]

def encode_image(image_file):
    return base64.b64encode(image_file.read()).decode('utf-8')

@app.route('/', methods=['GET'])
def index():
    return "Bevasarlo Backend (Full Cloud Sync + AI + Images + WebSockets) is running!"

# ==============================================================================
# 📸 AI KÉPFELISMERÉS + OKOS KVÓTA RENDSZER
# ==============================================================================

def get_user_status(app_user_id):
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

                non_subs = subscriber.get("non_subscriptions", {})
                for prod_id, purchases in non_subs.items():
                    for p in purchases:
                        p_date_str = p.get("purchase_date")
                        if p_date_str:
                            p_date = datetime.strptime(p_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
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

    is_pro, extra_quota = get_user_status(app_user_id)

    if not is_pro and extra_quota == 0:
        return jsonify({
            "error": "Prémium funkció 💎\n\nAz AI képfelismerés használatához Pro előfizetés szükséges. Kérlek, válts Prémiumra a beállításokban!"
        }), 403

    one_minute_ago = now - timedelta(minutes=1)
    recent_requests = ai_naplo.count_documents({
        "app_user_id": app_user_id,
        "timestamp": {"$gte": one_minute_ago}
    })

    if recent_requests >= 5:
        return jsonify({
            "error": "Túl sok kérés! 🚦\n\nKérlek, várj egy picit (kb. 1 percet) a következő kép elemzése előtt!"
        }), 429

    thirty_days_ago = now - timedelta(days=30)
    monthly_usage = ai_naplo.count_documents({
        "app_user_id": app_user_id,
        "timestamp": {"$gte": thirty_days_ago},
        "status": "success"
    })

    total_quota = (100 if is_pro else 0) + extra_quota

    if monthly_usage >= total_quota:
        return jsonify({
            "error": f"Kimerítetted a keretedet! 🔒\n\nElhasználtad a rendelkezésre álló {total_quota} db AI fotódat az elmúlt 30 napban. Vásárolj extra csomagot a folytatáshoz!"
        }), 403

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
        
        result_json = response.choices[0].message.content
        parsed_result = json.loads(result_json)

        ai_naplo.insert_one({
            "app_user_id": app_user_id,
            "timestamp": now,
            "action": "analyze_image",
            "status": "success"
        })
        monthly_usage += 1

        maradek = total_quota - monthly_usage
        if monthly_usage in [25, 50, 75]:
            parsed_result["warning"] = f"Még {maradek} fotód maradt a havi AI keretedből!"

        return jsonify(parsed_result), 200

    except Exception as e:
        ai_naplo.insert_one({
            "app_user_id": app_user_id,
            "timestamp": now,
            "action": "analyze_image",
            "status": "error",
            "error_msg": str(e)
        })
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# ☁️ KÉP TÁROLÁS ÉS KISZOLGÁLÁS
# ==============================================================================

@app.route('/upload_image', methods=['POST'])
def upload_image():
    if 'image' not in request.files:
        return jsonify({"error": "Nincs kép a kérésben"}), 400
        
    image_file = request.files['image']
    image_data = image_file.read()
    
    if len(image_data) > 500 * 1024:
        return jsonify({"error": "A kép túl nagy! Maximum 500KB engedélyezett."}), 400

    image_id = str(uuid.uuid4())
    
    kepek_kollekcio.insert_one({
        "image_id": image_id,
        "image_data": image_data,
        "content_type": image_file.mimetype,
        "created_at": datetime.now(timezone.utc)
    })
    
    host_url = request.host_url.rstrip('/')
    image_url = f"{host_url}/get_image/{image_id}"
    
    return jsonify({"status": "success", "image_url": image_url}), 200

@app.route('/get_image/<image_id>', methods=['GET'])
def get_image(image_id):
    kep_dok = kepek_kollekcio.find_one({"image_id": image_id})
    
    if not kep_dok:
        return "Kép nem található", 404
        
    response = Response(kep_dok["image_data"], mimetype=kep_dok["content_type"])
    response.headers['Cache-Control'] = 'public, max-age=2592000'
    return response

# ==============================================================================
# ☁️ LISTA SZINKRONIZÁCIÓ (+ KUKÁSAUTÓ ÉS ALAPÍTÓ RÖGZÍTÉSE)
# ==============================================================================
@app.route('/sync_list', methods=['POST'])
def sync_list():
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id') 
    if not family_id: return jsonify({"error": "Nincs id"}), 400
    
    list_data = data.get('list_data')
    # === 1. JAVÍTÁS: Kinyerjük a beérkező adat időbélyegét ===
    incoming_timestamp = data.get('timestamp', 0)
    
    try:
        regi_csalad = kollekcio.find_one({"family_id": family_id})
        
        # === 2. JAVÍTÁS: A KRITIKUS IDŐBÉLYEG ELLENŐRZÉS (RACE CONDITION FIX) ===
        if regi_csalad:
            db_timestamp = regi_csalad.get("timestamp", 0)
            
            # Ha a DB-ben lévő adat frissebb, mint amit a telefon küld, ELDOBJUK!
            if incoming_timestamp < db_timestamp:
    print(f"⚠️ Elavult adat eldobva!")
    # Szólunk a küldőnek is, hogy töltse le a frissebb verziót!
    socketio.emit('list_updated', {"family_id": family_id, "timestamp": db_timestamp}, room=family_id)
    return jsonify({"status": "ignored", ...}), 200

        if regi_csalad and "list_data" in regi_csalad:
            regi_linkek = set()
            uj_linkek = set()
            
            if "items" in regi_csalad["list_data"]:
                for item in regi_csalad["list_data"]["items"]:
                    unit = item.get("unit", "")
                    if ":::" in unit:
                        link = unit.split(":::")[1]
                        if "/get_image/" in link:
                            regi_linkek.add(link.split("/")[-1])
            
            if list_data and "items" in list_data:
                for item in list_data["items"]:
                    unit = item.get("unit", "")
                    if ":::" in unit:
                        link = unit.split(":::")[1]
                        if "/get_image/" in link:
                            uj_linkek.add(link.split("/")[-1])
                            
            torlendo_kepek = regi_linkek - uj_linkek
            for kep_id in torlendo_kepek:
                kepek_kollekcio.delete_one({"image_id": kep_id})
    except Exception as e:
        pass

    # Csak akkor jutunk ide, ha az adat FRISS és érvényes!
    kollekcio.update_one({"family_id": family_id}, 
                         {
                             "$set": {"list_data": list_data, "timestamp": incoming_timestamp},
                             "$setOnInsert": {"owner_id": user_id}
                         }, 
                         upsert=True)
                         
    if user_id:
        tagok_kollekcio.update_one(
            {"family_id": family_id, "user_id": user_id},
            {"$setOnInsert": {"user_name": "Alapító", "joined_at": incoming_timestamp}},
            upsert=True
        )

    # Szólunk a többieknek, DE a küldőnek nem (ez akadályozza meg a végtelen hurkot!)
    socketio.emit('list_updated', {"family_id": family_id, "timestamp": incoming_timestamp}, room=family_id)

    return jsonify({"status": "success"}), 200

@app.route('/get_list', methods=['GET'])
def get_list():
    family_id = request.args.get('family_id')
    csalad = kollekcio.find_one({"family_id": family_id})
    if csalad:
        return jsonify({"list_data": csalad.get("list_data"), "timestamp": csalad.get("timestamp")}), 200
    
    # 404 HIBA HELYETT: Ha még nincs lista, adunk egy üreset, így nem omlik össze az app a csatlakozó félnél!
    return jsonify({"list_data": {"items": []}, "timestamp": 0}), 200

# ==============================================================================
# 🤝 CSALÁD KEZELŐ FUNKCIÓK
# ==============================================================================

@app.route('/join_group', methods=['POST'])
def join_group():
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id')
    user_name = data.get('user_name')

    tagok_kollekcio.update_one(
        {"family_id": family_id, "user_id": user_id},
        {"$set": {"user_name": user_name, "joined_at": data.get('timestamp')}},
        upsert=True
    )
    return jsonify({"status": "joined"}), 200

@app.route('/leave_group', methods=['POST'])
def leave_group():
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id')
    
    tagok_kollekcio.delete_one({"family_id": family_id, "user_id": user_id})
    maradek_tagok = tagok_kollekcio.count_documents({"family_id": family_id})
    
    if maradek_tagok == 0:
        kollekcio.delete_one({"family_id": family_id})
        # Szólunk rádión, hogy megszűnt a csoport
        socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)
        
    return jsonify({"status": "left"}), 200

@app.route('/delete_group', methods=['POST'])
def delete_group():
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id')
    
    lista = kollekcio.find_one({"family_id": family_id})
    
    if lista and lista.get("owner_id") == user_id:
        kollekcio.delete_one({"family_id": family_id})
        tagok_kollekcio.delete_many({"family_id": family_id})
        # ÚJ: Rádión azonnal kihajítunk mindenkit a szobából!
        socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)
        return jsonify({"status": "deleted"}), 200
    else:
        tagok_kollekcio.delete_one({"family_id": family_id, "user_id": user_id})
        maradek_tagok = tagok_kollekcio.count_documents({"family_id": family_id})
        if maradek_tagok == 0:
            kollekcio.delete_one({"family_id": family_id})
            socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)
        return jsonify({"status": "left_only", "warning": "Nem te vagy az alapító, így csak kiléptél."}), 200

@app.route('/update_token', methods=['POST'])
def update_token():
    data = request.get_json()
    user_id = data.get('user_id')
    fcm_token = data.get('fcm_token')
    
    tagok_kollekcio.update_many(
        {"user_id": user_id},
        {"$set": {"fcm_token": fcm_token}}
    )
    return jsonify({"status": "token_updated"}), 200

# ==============================================================================
# 📻 SOCKET.IO (WALKIE-TALKIE) ESEMÉNYEK
# ==============================================================================

@socketio.on('join_room')
def handle_join_room(data):
    """Amikor egy app megnyílik, rácsatlakozik a családja csatornájára."""
    family_id = data.get('family_id')
    if family_id:
        join_room(family_id)
        print(f"📡 Egy kliens rácsatlakozott a {family_id} szobára.")

@socketio.on('leave_room')
def handle_leave_room(data):
    """Amikor bezárja a közös listát, elhagyja a csatornát."""
    family_id = data.get('family_id')
    if family_id:
        leave_room(family_id)
        print(f"📡 Egy kliens lecsatlakozott a {family_id} szobáról.")

# ==============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # app.run helyett a socketio indítja el a szervert, hogy a rádió működjön!
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
