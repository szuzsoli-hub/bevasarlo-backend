import os
import hmac
from flask import Flask, request, jsonify, Response
from openai import OpenAI
import base64
import json
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import urllib.request
from datetime import datetime, timezone, timedelta
import uuid
import certifi
from flask_socketio import SocketIO, join_room, leave_room, emit # <-- ÚJ: A Rádiótorony alkatrészei
from coupons import register_coupon_routes

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
# A kulcsok a Render környezeti változóiból jönnek, NEM a kódból:
#   BACKEND_API_KEY     — az új kulcs (az új app-verziók ezt küldik)
#   BACKEND_API_KEY_OLD — a régi kulcs, átmenetileg (a régi app-verziók miatt)
# Üres / hiányzó változó nem kerül be. Ha mindkettő hiányzik, a szerver fut,
# de minden védett végpont 401-et ad (üres kulccsal senki nem jut be).

def _env_key(name):
    v = (os.environ.get(name) or "").strip()
    return v or None

ACCEPTED_API_KEYS = [k for k in (_env_key("BACKEND_API_KEY"),
                                 _env_key("BACKEND_API_KEY_OLD")) if k]
if not ACCEPTED_API_KEYS:
    print("🚨 Nincs beállítva BACKEND_API_KEY / BACKEND_API_KEY_OLD: a védett végpontok zárva (401).")

# ==============================================================================
# 📱 LEGFRISSEBB APP VERZIÓ (kézzel frissítendő minden kiadás után!)
# Ezt a versionName-mel (pubspec.yaml "1.0.50+55" elejével) kell szinkronban
# tartani, NEM a buildNumberrel. Csak azután emeld, hogy a kiadás ténylegesen
# megjelent a Play Áruházban, különben olyan frissítésre figyelmeztetsz,
# ami még nem tölthető le.
# ==============================================================================
LATEST_VERSION = "1.0.79"

# Kulcs nélkül elérhető útvonalak:
#   /webhook                — a RevenueCat hívja, X-API-KEY nélkül; a végpont
#                             jelenleg NEM ellenőrzi a hívót (az Authorization
#                             fejlécet sem), csak naplóz (ai_naplo, "logged")
#   /admin_generate_coupons — saját ADMIN_SECRET védi (coupons.py)
#   /redeem                 — a Brevo emailekben kiküldött, böngészőből nyitott
#                             kattintható link, nincs hozzá X-API-KEY headere
#   /latest_version         — a frissítés-értesítés kulcscsere után is működjön
#                             (a régi kulccsal futó app-verzióknál is)
PUBLIC_PATHS = {'/', '/webhook', '/admin_generate_coupons', '/redeem', '/latest_version'}
PUBLIC_PREFIXES = ('/get_image/', '/socket.io/')

@app.before_request
def require_api_key():
    if request.path in PUBLIC_PATHS or request.path.startswith(PUBLIC_PREFIXES):
        return

    client_key = (request.headers.get('X-API-KEY') or '').encode('utf-8')
    if not client_key or not any(hmac.compare_digest(client_key, k.encode('utf-8'))
                                 for k in ACCEPTED_API_KEYS):
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
kuponok_kollekcio = db["kuponok"]
# A már feldolgozott +20 kép vásárlások (transaction_id), hogy ugyanaz
# kétszer ne íródjon jóvá (lásd /topup_credits).
topup_tranzakciok = db["topup_tranzakciok"]
try:
    topup_tranzakciok.create_index("transaction_id", unique=True)
except Exception as e:
    # Induláskor ne álljon le a szerver; a következő indításkor újra próbálja.
    print(f"🚨 topup_tranzakciok index hiba: {e}")

# ==============================================================================
# 🔑 REVENUECAT SECRET KEY (modul-szinten, hogy a coupons.py is elérje)
# ==============================================================================
REVENUECAT_SECRET_KEY = os.environ.get("REVENUECAT_SECRET_KEY")

def encode_image(image_file):
    return base64.b64encode(image_file.read()).decode('utf-8')

@app.route('/', methods=['GET'])
def index():
    return "Bevasarlo Backend (Full Cloud Sync + AI + Images + WebSockets) is running!"

# ==============================================================================
# 📸 AI KÉPFELISMERÉS + OKOS KVÓTA RENDSZER
# ==============================================================================

TOPUP_PRODUCT_ID = "ai_topup_20"

def is_topup_product(prod_id):
    """
    True, ha a RevenueCat termékazonosító a +20 képes csomag. A Google Play
    azonosító jöhet simán ("ai_topup_20") vagy kiegészítéssel
    ("ai_topup_20:valami") — mindkettő számít, más termék nem.
    """
    return str(prod_id or "").split(":")[0] == TOPUP_PRODUCT_ID

class RevenueCatUnavailable(Exception):
    """A RevenueCat nem kérdezhető le (hálózat, hibás kulcs, szerverhiba).
    Ilyenkor NEM tudjuk, Pro-e a felhasználó: nem mondhatjuk, hogy elfogyott
    a kerete, és azt sem, hogy nincs előfizetése."""


def get_user_status(app_user_id):
    """(is_pro, extra_quota). Ha a RevenueCat nem válaszol rendesen,
    RevenueCatUnavailable-t dob. (Régen ilyenkor csendben "nem Pro" lett,
    amiből az app "elfogyott a képkereted" ablakot csinált.)"""
    url = f"https://api.revenuecat.com/v1/subscribers/{app_user_id}"
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {REVENUECAT_SECRET_KEY}')
    req.add_header('Content-Type', 'application/json')
    
    is_pro = False
    extra_quota = 0
    
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            # 201: a RevenueCat most hozta létre az ügyfelet (nincs vásárlása).
            if response.status not in (200, 201):
                raise RevenueCatUnavailable(f"HTTP {response.status}")
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

            # A non_subscriptions kulcsa a termékazonosító; CSAK a +20
            # képes csomag ad képet (a kávé és más egyszeri vásárlás nem).
            non_subs = subscriber.get("non_subscriptions", {})
            for prod_id, purchases in non_subs.items():
                if not is_topup_product(prod_id):
                    continue
                for p in purchases:
                    p_date_str = p.get("purchase_date")
                    if p_date_str:
                        p_date = datetime.strptime(p_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        if p_date > datetime.now(timezone.utc) - timedelta(days=30):
                            extra_quota += 20

    except RevenueCatUnavailable:
        raise
    except Exception as e:
        print(f"🚨 RevenueCat hiba: {e}")
        raise RevenueCatUnavailable(str(e)) from e
        
    return is_pro, extra_quota

AI_PRO_MONTHLY_QUOTA = 60


def ai_monthly_usage(app_user_id, now):
    """Az elmúlt 30 nap felhasznált képei. CSAK a sikeres képelemzés számít:
    az ai_naplo-ban más bejegyzés is "success" (pl. a +20 vásárlás naplója,
    topup_purchase), ami nélkül minden +20 vásárlás csak +19-et ért."""
    return ai_naplo.count_documents({
        "app_user_id": app_user_id,
        "action": "analyze_image",
        "timestamp": {"$gte": now - timedelta(days=30)},
        "status": "success"
    })


def ai_total_quota(is_pro, extra_quota):
    """A 30 napos keret: Pro-nak 60, plusz minden 30 napon belüli +20."""
    return (AI_PRO_MONTHLY_QUOTA if is_pro else 0) + extra_quota


@app.route('/ai_quota', methods=['GET'])
def ai_quota():
    """A megmaradt AI-képek száma (az app ezt mutatja a "X/60 kép"
    címkén). A szerver az igazság forrása; a kliens csak tárolja."""
    app_user_id = request.args.get('app_user_id')
    if not app_user_id:
        return jsonify({"error": "Hiányzó azonosító!"}), 400
    try:
        is_pro, extra_quota = get_user_status(app_user_id)
    except RevenueCatUnavailable:
        return jsonify({"error": "subscription_check_failed"}), 503
    total = ai_total_quota(is_pro, extra_quota)
    used = ai_monthly_usage(app_user_id, datetime.now(timezone.utc))
    return jsonify({
        "remaining": max(total - used, 0),
        "total": total,
        "is_pro": is_pro,
    }), 200


@app.route('/analyze', methods=['POST'])
def analyze_image():
    app_user_id = request.form.get('app_user_id')
    now = datetime.now(timezone.utc)

    if not app_user_id:
         return jsonify({"error": "Hiányzó azonosító!"}), 400

    # Hibakódok az appnak ("error"), emberi szöveg a "message"-ben:
    #   503 subscription_check_failed — a RevenueCat nem kérdezhető le
    #   403 not_pro                   — nincs Pro és nincs +20 kerete
    #   403 quota_exhausted           — elfogyott a képkeret
    # A régi app (1.0.78) csak a státuszkódot nézi: 403-ra a +20 ablakot
    # mutatja (mint eddig), 503-ra általános "próbáld újra" üzenetet.
    try:
        is_pro, extra_quota = get_user_status(app_user_id)
    except RevenueCatUnavailable:
        return jsonify({
            "error": "subscription_check_failed",
            "message": "Most nem sikerült ellenőrizni az előfizetésedet. Kérlek, próbáld újra később!"
        }), 503

    if not is_pro and extra_quota == 0:
        return jsonify({
            "error": "not_pro",
            "message": "Prémium funkció 💎\n\nAz AI képfelismerés használatához Pro előfizetés szükséges. Kérlek, válts Prémiumra a beállításokban!"
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

    monthly_usage = ai_monthly_usage(app_user_id, now)
    total_quota = ai_total_quota(is_pro, extra_quota)

    if monthly_usage >= total_quota:
        return jsonify({
            "error": "quota_exhausted",
            "remaining": 0,
            "total": total_quota,
            "message": f"Kimerítetted a keretedet! 🔒\n\nElhasználtad a rendelkezésre álló {total_quota} db AI fotódat az elmúlt 30 napban. Vásárolj extra csomagot a folytatáshoz!"
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
        if monthly_usage in [20, 40, 55]:
            parsed_result["warning"] = f"Még {maradek} fotód maradt a havi AI keretedből!"

        # Az app ebből frissíti a "X/60 kép" címkét (a régi app nem nézi).
        parsed_result["remaining"] = max(maradek, 0)
        parsed_result["total"] = total_quota

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
# 🛡️ TAGSÁG / KITILTÁS SEGÉDFÜGGVÉNY
# ==============================================================================
#
# ÚJ, 2026.08.29-i MÓDOSÍTÁS — VALÓDI TAGSÁG-ELLENŐRZÉS BEVEZETÉSE
#
# EDDIG: aki ismerte a 6 karakteres kódot, korlátlanul írhatott/olvashatott —
#        a szerver soha nem ellenőrizte, hogy a kérő valóban regisztrált
#        tagja-e a listának. Ez azt jelentette, hogy egy kitiltás/kizárás
#        funkció önmagában csak díszlet lett volna: a kitiltott fél a kóddal
#        továbbra is hozzáférne, ha a szerver nem venné ezt figyelembe.
#
# MOSTANTÓL: minden MEGLÉVŐ lista írásakor, és a kliens által kifejezetten
#            "folyamatos szinkronként" jelzett olvasásakor (lásd /get_list),
#            a szerver megnézi: a kérő user_id-nak van-e tagsági bejegyzése,
#            és nincs-e kitiltva. Ha nincs bejegyzés, vagy ki van tiltva,
#            403-at ad vissza.
#
# FONTOS, SZÁNDÉKOS KIVÉTEL: az ELŐNÉZETI lekérdezés (amikor valaki még csak
#            megnézi, kié egy kód, mielőtt csatlakozna) NEM küld user_id-t —
#            ez marad mindenki számára nyitott, hiszen még nem is tag.
#            Ez a kliens felelőssége: csak a already-csatlakozott, folyamatos
#            szinkron küldjön user_id-t a /get_list hívásakor.
#
# VISSZAFELÉ KOMPATIBILIS: ha egy régebbi app-verzió egyáltalán nem küld
#            user_id-t olvasáskor, nem ellenőrzünk nála — az írás (/sync_list)
#            viszont már most is mindig kap user_id-t minden verziónál, ott
#            az ellenőrzés azonnal, kivétel nélkül érvényes.
# ==============================================================================

def tagsag_tiltva(family_id, user_id):
    """
    True, ha a user_id-t BLOKKOLNI kell: nincs tagsági bejegyzése ehhez a
    listához, VAGY ki van tiltva. Ha nincs user_id megadva, nem blokkolunk
    (visszafelé kompatibilitás — lásd fenti megjegyzés).
    """
    if not user_id:
        return False
    tag = tagok_kollekcio.find_one({"family_id": family_id, "user_id": user_id})
    if tag is None:
        return True
    return bool(tag.get("banned", False))


def aktiv_tagok_szama(family_id):
    """
    A ténylegesen aktív (nem kitiltott) tagok száma — 2026.08.29-től ez a
    valós, felhasználóknak megjelenített taglétszám alapja, NEM az összes
    valaha regisztrált tag (ami a kitiltottakat is tartalmazná).
    """
    return tagok_kollekcio.count_documents(
        {"family_id": family_id, "banned": {"$ne": True}}
    )


# ==============================================================================
# ☁️ LISTA SZINKRONIZÁCIÓ (+ KUKÁSAUTÓ ÉS ALAPÍTÓ RÖGZÍTÉSE)
# ==============================================================================
#
# (A szerver-oldali időbélyeg és ütközés-védelem korábbi megjegyzése változatlan.)
# ==============================================================================
@app.route('/sync_list', methods=['POST'])
def sync_list():
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id') 
    if not family_id: return jsonify({"error": "Nincs id"}), 400
    
    list_data = data.get('list_data')
    base_timestamp = data.get('base_timestamp')

    regi_csalad = kollekcio.find_one({"family_id": family_id})

    # ÚJ: tagság/kitiltás-ellenőrzés — csak MEGLÉVŐ listánál. Új lista
    # létrehozásakor (regi_csalad is None) nincs mit ellenőrizni, hiszen
    # ekkor jön létre az Alapító tagsága is, lentebb.
    if regi_csalad and tagsag_tiltva(family_id, user_id):
        return jsonify({
            "error": "Nincs jogosultságod ehhez a listához.",
            "banned": True
        }), 403

    if regi_csalad:
        db_timestamp = regi_csalad.get("timestamp", 0)

        if base_timestamp is not None and base_timestamp != db_timestamp:
            print(f"⚠️ Ütközés! A kliens {base_timestamp} alapján mentett, "
                  f"de a szerveren jelenleg {db_timestamp} van (family_id={family_id}). "
                  f"Ez akkor is jelentkezik, ha a kliens 'base_timestamp'-je NAGYOBB, "
                  f"mint a szerveré — enélkül egy hibás/manipulált kliens megkerülhetné "
                  f"az ütközés-védelmet egy 'jövőbeli' értékkel.")
            # ÚJ, 2026.08.29: itt is a VALÓS, aktív taglétszámot adjuk vissza,
            # nem az összes valaha regisztráltat.
            conflict_member_count = aktiv_tagok_szama(family_id)
            return jsonify({
                "status": "conflict",
                "message": "Közben más frissítette a listát. Frissülj, majd próbáld újra.",
                "list_data": regi_csalad.get("list_data"),
                "timestamp": db_timestamp,
                "member_count": conflict_member_count
            }), 409

    try:
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

    # A szerver saját, aktuális ideje — SOHA nem a kliens órája.
    server_timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)

    kollekcio.update_one({"family_id": family_id}, 
                         {
                             "$set": {"list_data": list_data, "timestamp": server_timestamp},
                             "$setOnInsert": {"owner_id": user_id}
                         }, 
                         upsert=True)
                         
    if user_id:
        tagok_kollekcio.update_one(
            {"family_id": family_id, "user_id": user_id},
            {"$setOnInsert": {"user_name": "Alapító", "joined_at": server_timestamp, "banned": False}},
            upsert=True
        )

    socketio.emit('list_updated', {"family_id": family_id, "timestamp": server_timestamp}, room=family_id)
    return jsonify({"status": "success", "timestamp": server_timestamp}), 200

@app.route('/get_list', methods=['GET'])
def get_list():
    family_id = request.args.get('family_id')
    # ÚJ, OPCIONÁLIS paraméter: csak a folyamatos szinkron küldje el —
    # az előnézeti lekérdezés (csatlakozás előtt) NE küldje, hogy bárki
    # megnézhesse, kié egy kód, mielőtt ténylegesen csatlakozna.
    user_id = request.args.get('user_id')

    csalad = kollekcio.find_one({"family_id": family_id})
    if csalad:
        if user_id and tagsag_tiltva(family_id, user_id):
            return jsonify({
                "error": "Nincs jogosultságod ehhez a listához.",
                "banned": True
            }), 403

        # ÚJ, 2026.08.29: a VALÓS, aktív taglétszám — a kitiltott tagok nem
        # számítanak bele, hogy a felhasználó a helyes, aktuális állapotot
        # lássa (kitiltáskor -1, visszaengedéskor +1).
        member_count = aktiv_tagok_szama(family_id)
        owner_id = csalad.get("owner_id")
        owner_name = None
        if owner_id:
            owner_doc = tagok_kollekcio.find_one({"family_id": family_id, "user_id": owner_id})
            if owner_doc:
                owner_name = owner_doc.get("user_name")
        return jsonify({
            "exists": True,
            "list_data": csalad.get("list_data"),
            "timestamp": csalad.get("timestamp"),
            "member_count": member_count,
            "owner_id": owner_id,
            "owner_name": owner_name
        }), 200

    # ÚJ: explicit "exists": False — a kliens ne a member_count==0-ra
    # (ami egy vadonatúj, de legitim listánál is előfordulhatna) hanem
    # erre a mezőre alapozza a "nem létező kód" döntést.
    return jsonify({
        "exists": False,
        "list_data": {"items": []},
        "timestamp": 0,
        "member_count": 0,
        "owner_id": None,
        "owner_name": None
    }), 200

# ==============================================================================
# 📱 LEGFRISSEBB VERZIÓ LEKÉRDEZÉSE (induláskori frissítés-értesítéshez)
# ==============================================================================
@app.route('/latest_version', methods=['GET'])
def latest_version():
    return jsonify({"latest_version": LATEST_VERSION}), 200

# ==============================================================================
# 🤝 CSALÁD KEZELŐ FUNKCIÓK
# ==============================================================================

@app.route('/join_group', methods=['POST'])
def join_group():
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id')
    user_name = data.get('user_name')

    # ÚJ: nem engedjük regisztrálni a tagságot/nevet nem létező (pl. elgépelt)
    # kódra — eddig ez "csendben sikeres" csatlakozásnak tűnt a kliens felől,
    # miközben valójában egy szellem-bejegyzés jött létre.
    # Idempotens marad: az Alapító (vagy bármely meglévő tag) a SAJÁT nevét
    # bármikor frissítheti ugyanezen a végponton, mert a saját listája már
    # mindig létezik ilyenkor.
    csalad = kollekcio.find_one({"family_id": family_id})
    if not csalad:
        return jsonify({"error": "Ehhez a kódhoz nem tartozik lista.", "exists": False}), 404

    # ÚJ: ha valakit korábban kitiltottak, a kóddal való újra-"csatlakozás"
    # (join_group hívás) NE oldja fel automatikusan a tiltást — csak az
    # Alapító, a /moderate_member végponton keresztül teheti ezt meg.
    meglevo_tag = tagok_kollekcio.find_one({"family_id": family_id, "user_id": user_id})
    if meglevo_tag and meglevo_tag.get("banned", False):
        return jsonify({"error": "Ki lettél tiltva erről a listáról.", "banned": True}), 403

    tagok_kollekcio.update_one(
        {"family_id": family_id, "user_id": user_id},
        {
            "$set": {"user_name": user_name, "joined_at": data.get('timestamp')},
            "$setOnInsert": {"banned": False}
        },
        upsert=True
    )
    return jsonify({"status": "joined"}), 200

@app.route('/leave_group', methods=['POST'])
def leave_group():
    data = request.get_json()
    family_id = data.get('family_id')
    user_id = data.get('user_id')

    # ÚJ, 2026.08.29-i SZABÁLY: az Alapító nem egyszerűen "kilép" — ha ő
    # kezdeményezi a leválasztást (a kliens "Priváttá tétel" gombja, ami
    # az Alapítónál az EGYETLEN elérhető opció, a sima kilépés nála nem is
    # jelenik meg), az az EGÉSZ közös lista feloszlatását jelenti mindenki
    # számára — nem csak neki válik priváttá, hanem mindenkinek. A kliens
    # ugyanezt a végpontot hívja Alapítóként és nem-Alapítóként is; itt,
    # szerveroldalon dől el, melyik eset áll fenn.
    csalad = kollekcio.find_one({"family_id": family_id})

    if csalad and csalad.get("owner_id") == user_id:
        list_data = csalad.get("list_data") or {}
        try:
            if "items" in list_data:
                for item in list_data["items"]:
                    unit = item.get("unit", "")
                    if ":::" in unit:
                        link = unit.split(":::")[1]
                        if "/get_image/" in link:
                            kep_id = link.split("/")[-1]
                            kepek_kollekcio.delete_one({"image_id": kep_id})
        except Exception:
            pass

        tagok_kollekcio.delete_many({"family_id": family_id})
        kollekcio.delete_one({"family_id": family_id})

        # A maradék tagok kliensei ugyanazt a "group_deleted" eseményt kapják,
        # mint eddig is — a kliens-oldali kezelést kell privatizálásra
        # (tartalom megtartása, csak leválasztás a felhőről) módosítani,
        # NEM törlésre, ahogy eddig tette.
        socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)
        return jsonify({"status": "owner_dissolved"}), 200

    # Nem az Alapító lép ki: csak az ő tagsága törlődik, a csoport a
    # többieknek változatlanul tovább él.
    tagok_kollekcio.delete_one({"family_id": family_id, "user_id": user_id})
    maradek_tagok = tagok_kollekcio.count_documents({"family_id": family_id})
    
    if maradek_tagok == 0:
        kollekcio.delete_one({"family_id": family_id})
        socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)
        return jsonify({"status": "last_member_left"}), 200
        
    socketio.emit('list_updated', {"family_id": family_id}, room=family_id)
    return jsonify({"status": "left"}), 200

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
# 🛡️ MODERÁLÁS: TAGOK LISTÁZÁSA + KITILTÁS/VISSZAENGEDÉS (ÚJ, 2026.08.29)
# ==============================================================================
#
# Mindkét végpontot KIZÁRÓLAG az Alapító (owner_id) hívhatja sikeresen —
# bárki más 403-at kap. Az Alapítót saját magát nem lehet kitiltani.
# ==============================================================================

@app.route('/list_members', methods=['GET'])
def list_members():
    family_id = request.args.get('family_id')
    requester_id = request.args.get('user_id')
    if not family_id or not requester_id:
        return jsonify({"error": "Hiányzó paraméter"}), 400

    csalad = kollekcio.find_one({"family_id": family_id})
    if not csalad:
        return jsonify({"error": "Nincs ilyen lista", "exists": False}), 404

    if csalad.get("owner_id") != requester_id:
        return jsonify({"error": "Csak az Alapító kérheti le a tagok listáját."}), 403

    owner_id = csalad.get("owner_id")
    tagok = list(tagok_kollekcio.find({"family_id": family_id}))
    members = [{
        "user_id": t.get("user_id"),
        "user_name": t.get("user_name", "Ismeretlen"),
        "banned": bool(t.get("banned", False)),
        "joined_at": t.get("joined_at"),
        "is_owner": t.get("user_id") == owner_id,
    } for t in tagok]

    return jsonify({"members": members}), 200


@app.route('/moderate_member', methods=['POST'])
def moderate_member():
    data = request.get_json()
    family_id = data.get('family_id')
    requester_id = data.get('requester_id')
    target_user_id = data.get('target_user_id')
    banned = data.get('banned')  # true = kitiltás, false = visszaengedés

    if not all([family_id, requester_id, target_user_id]) or banned is None:
        return jsonify({"error": "Hiányzó paraméter"}), 400

    csalad = kollekcio.find_one({"family_id": family_id})
    if not csalad:
        return jsonify({"error": "Nincs ilyen lista", "exists": False}), 404

    if csalad.get("owner_id") != requester_id:
        return jsonify({"error": "Csak az Alapító tilthat ki/engedhet vissza tagot."}), 403

    if target_user_id == csalad.get("owner_id"):
        return jsonify({"error": "Az Alapítót nem lehet kitiltani."}), 400

    result = tagok_kollekcio.update_one(
        {"family_id": family_id, "user_id": target_user_id},
        {"$set": {"banned": bool(banned)}}
    )
    if result.matched_count == 0:
        return jsonify({"error": "Ez a felhasználó nem tagja a listának."}), 404

    # Jelezzük a szobában, hogy valami változott — a kitiltott fél (és a
    # többiek) legközelebbi szinkronja már az új állapotot fogja látni.
    socketio.emit('list_updated', {"family_id": family_id}, room=family_id)

    return jsonify({"status": "ok", "banned": bool(banned)}), 200


# ==============================================================================
# 💳 TOP-UP KREDITEK (+20 AI szkennelés)
# ==============================================================================
@app.route('/topup_credits', methods=['POST'])
def topup_credits():
    data = request.get_json()
    user_id = data.get('user_id')
    credits = data.get('credits', 20)

    if not user_id:
        return jsonify({"error": "Hiányzó user_id"}), 400

    now = datetime.now(timezone.utc)

    # Az új app-verziók a vásárlás tranzakció-azonosítóját is küldik, és
    # sikertelen válasznál újrapróbálják. Ugyanazt az azonosítót csak egyszer
    # írjuk jóvá; ismétlésnél 200 (hogy a kliens kivegye a sorából), de
    # credits_added: 0. Azonosító nélkül (régi app) marad a régi viselkedés.
    transaction_id = str(data.get('transaction_id') or '').strip()
    if transaction_id:
        try:
            topup_tranzakciok.insert_one({
                "transaction_id": transaction_id,
                "app_user_id": user_id,
                "credits": credits,
                "created_at": now
            })
        except DuplicateKeyError:
            return jsonify({"status": "success", "credits_added": 0, "duplicate": True}), 200

    # Naplózzuk a top-up vásárlást — a kvóta számítás (get_user_status)
    # a non_subscriptions alapján automatikusan látja a +20 kreditet,
    # ez a bejegyzés csak audit célú. Felhasznált képnek NEM számít: az
    # /analyze csak az "analyze_image" bejegyzéseket számolja.
    naplo = {
        "app_user_id": user_id,
        "timestamp": now,
        "action": "topup_purchase",
        "credits": credits,
        "status": "success"
    }
    if transaction_id:
        naplo["transaction_id"] = transaction_id
    ai_naplo.insert_one(naplo)

    return jsonify({"status": "success", "credits_added": credits}), 200


# ==============================================================================
# 🗑️ FELHASZNÁLÓ ADATOK TÖRLÉSE (GDPR — KÖTELEZŐ!)
# ==============================================================================
@app.route('/delete_user_data', methods=['POST'])
def delete_user_data():
    data = request.get_json()
    user_id = data.get('user_id')

    if not user_id:
        return jsonify({"error": "Hiányzó user_id"}), 400

    # 1. AI napló teljes törlése (+ a feldolgozott top-up tranzakciók)
    ai_naplo.delete_many({"app_user_id": user_id})
    topup_tranzakciok.delete_many({"app_user_id": user_id})

    # 2. Minden csoportból kiléptetés + üres csoportok törlése
    user_groups = list(tagok_kollekcio.find({"user_id": user_id}))
    for group in user_groups:
        family_id = group.get("family_id")
        tagok_kollekcio.delete_one({"family_id": family_id, "user_id": user_id})

        maradek = tagok_kollekcio.count_documents({"family_id": family_id})
        if maradek == 0:
            # Ha ő volt az utolsó tag, a lista és a képek is törlődnek
            lista_dok = kollekcio.find_one({"family_id": family_id})
            if lista_dok and "list_data" in lista_dok:
                if "items" in lista_dok["list_data"]:
                    for item in lista_dok["list_data"]["items"]:
                        unit = item.get("unit", "")
                        if ":::" in unit:
                            link = unit.split(":::")[1]
                            if "/get_image/" in link:
                                kep_id = link.split("/")[-1]
                                kepek_kollekcio.delete_one({"image_id": kep_id})
            kollekcio.delete_one({"family_id": family_id})
            socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)

    print(f"🗑️ GDPR törlés elvégezve: {user_id}")
    return jsonify({"status": "deleted", "message": "Minden adat törölve"}), 200


# ==============================================================================
# 🔔 REVENUECAT WEBHOOK (Lemondás / Lejárat / Fizetési probléma)
# ==============================================================================
@app.route('/webhook', methods=['POST'])
def revenuecat_webhook():
    data = request.get_json()

    if not data:
        return jsonify({"error": "Üres webhook"}), 400

    event = data.get('event', {})
    event_type = event.get('type')
    app_user_id = event.get('app_user_id')
    product_id = event.get('product_id', '')
    expiration_at_ms = event.get('expiration_at_ms')

    print(f"📣 RevenueCat webhook: {event_type} - {app_user_id} - {product_id}")

    # Naplózzuk az eseményt — a tényleges zárolást a Flutter végzi
    # a RevenueCat SDK entitlement ellenőrzése alapján (expires_date)
    if event_type in ['CANCELLATION', 'EXPIRATION', 'BILLING_ISSUES_DETECTED']:
        ai_naplo.insert_one({
            "app_user_id": app_user_id,
            "timestamp": datetime.now(timezone.utc),
            "action": f"subscription_{event_type.lower()}",
            "product_id": product_id,
            "expires_at_ms": expiration_at_ms,
            "status": "logged"
        })

    return jsonify({"status": "ok"}), 200


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

register_coupon_routes(app, kuponok_kollekcio, REVENUECAT_SECRET_KEY)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
