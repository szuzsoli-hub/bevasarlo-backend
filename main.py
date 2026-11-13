import os
import hmac
from flask import Flask, request, jsonify, Response
from openai import OpenAI
import base64
import json
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
import uuid
import certifi
from flask_socketio import SocketIO, join_room, leave_room, emit # <-- ÚJ: A Rádiótorony alkatrészei
from coupons import register_coupon_routes
import vedelem
from vedelem import Vedelem, szoveg, kod, nev, egesz, cim_kulcs, felhasznalo_kulcs, ujjlenyomat
import atexit
import igazolas

app = Flask(__name__)

# Egy kérés legfeljebb 5 MB (az app képei 1600 px-esek, ennél jóval kisebbek).
# Nagyobbra a Flask magától 413-at ad, mielőtt beolvasná.
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# Ebből látszik kívülről (/latest_version), hogy melyik szerverkód fut.
SZERVER_VALTOZAT = "2026-11-06-igazolas"

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
LATEST_VERSION = "1.0.82"

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

# ==============================================================================
# 🚦 TÚL SOK KÉRÉS ELLEN (vedelem.py; a számok ott, a SZABALYOK táblában)
# ==============================================================================

def _tiltas_naplo(adat):
    """Minden tiltás a Render naplójába és a vedelem_naplo gyűjteménybe kerül
    (30 napig). A cím és az azonosító helyett csak az ujjlenyomatuk."""
    print(f"🚦 Védelem: tiltás {adat['tiltas_mp'] // 60} percre – {adat['tipus']} "
          f"{adat['kulcs']}, terület: {adat['terulet']}, szabály: {adat['szabaly']}")
    vedelem_naplo.insert_one(dict(adat, ido=datetime.now(timezone.utc)))


VED = Vedelem(naplo=_tiltas_naplo)
_CF_FEJLEC_LATTUK = False


def kero_cim():
    """A kérő címének ujjlenyomata a Cloudflare fejlécéből, vagy None."""
    global _CF_FEJLEC_LATTUK
    c = cim_kulcs(request.headers.get('CF-Connecting-IP'))
    if c and not _CF_FEJLEC_LATTUK:
        _CF_FEJLEC_LATTUK = True
        print("ℹ️ Védelem: a Cloudflare címfejléce megérkezett, a címkorlátok élnek.")
    return c


def tul_sok(mp):
    valasz = jsonify({
        "error": "Túl sok kérés! 🚦\n\nKérlek, próbáld újra később.",
        "retry_after": int(mp),
    })
    valasz.headers['Retry-After'] = str(int(mp))
    return valasz, 429


def cim_korlat(*szabalyok):
    """A kérő címére vonatkozó szabályok; 429-es válasz vagy None."""
    c = kero_cim()
    if not c:
        return None
    for sz in szabalyok:
        mp = VED.esemeny(sz, c)
        if mp:
            return tul_sok(mp)
    return None


def cim_tiltva(terulet):
    """429-es válasz, ha a kérő címe ezen a területen tiltva van; különben None."""
    c = kero_cim()
    mp = VED.tiltva(c, terulet) if c else 0
    return tul_sok(mp) if mp else None


def felhasznalo_korlat(azonosito, *szabalyok):
    """Az azonosítóra vonatkozó szabályok (minden kérése, sok cím egyszerre,
    plusz a megadottak); 429-es válasz vagy None."""
    f = felhasznalo_kulcs(azonosito)
    c = kero_cim()
    if c:
        mp = VED.cim(f, c)
        if mp:
            return tul_sok(mp)
    for sz in ("f_osszes",) + szabalyok:
        mp = VED.esemeny(sz, f)
        if mp:
            return tul_sok(mp)
    return None


def felhasznalo_korlat_egy(azonosito, szabaly):
    """Egyetlen további szabály ugyanarra a kérésre (a felhasznalo_korlat()
    után, hogy a kérés ne számítson kétszer)."""
    mp = VED.esemeny(szabaly, felhasznalo_kulcs(azonosito))
    return tul_sok(mp) if mp else None


@app.before_request
def keresek_cimenkent():
    # A Socket.IO már élő kapcsolatának kérései (sid) nem számítanak, csak az
    # új kapcsolat; különben a régi, hosszú lekérdezéses mód is beleszámítana.
    if request.path.startswith('/socket.io/') and request.args.get('sid'):
        return
    return cim_korlat("ip_osszes")


def kero_json():
    """A kérés JSON-törzse szótárként; ha nem az (vagy hibás), üres szótár."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def hibas(uzenet="Hibás kérés."):
    return jsonify({"error": uzenet}), 400


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
# A törölt közös listák kódjai, lásd lista_torolve_jelol().
torolt_listak = db["torolt_listak"]
# A már feldolgozott +20 kép vásárlások (transaction_id), hogy ugyanaz
# kétszer ne íródjon jóvá (lásd /topup_credits).
topup_tranzakciok = db["topup_tranzakciok"]
try:
    topup_tranzakciok.create_index("transaction_id", unique=True)
except Exception as e:
    # Induláskor ne álljon le a szerver; a következő indításkor újra próbálja.
    print(f"🚨 topup_tranzakciok index hiba: {e}")
# A túl sok kérés miatti tiltások naplója (vedelem.py); 30 nap után törlődik.
vedelem_naplo = db["vedelem_naplo"]
try:
    vedelem_naplo.create_index("ido", expireAfterSeconds=30 * 24 * 3600)
except Exception as e:
    print(f"🚨 vedelem_naplo index hiba: {e}")

# ==============================================================================
# 🔑 GOOGLE-IGAZOLÁS (igazolas.py): egyelőre csak ellenőrzés és számlálás
# ==============================================================================
# Az 1.0.81-es app az X-ID-TOKEN fejlécben elküldi a Google-bejelentkezés
# igazolását. Most még semmit nem utasítunk el miatta (a régi app nem küldi),
# csak számoljuk naponta és végpontonként az igazolas_szamlalo gyűjteményben
# (180 napig): jött-e, érvényes-e, és egyezik-e a kérés user_id-jével.
igazolas_szamlalo = db["igazolas_szamlalo"]
try:
    igazolas_szamlalo.create_index("letrejott", expireAfterSeconds=180 * 24 * 3600)
except Exception as e:
    print(f"🚨 igazolas_szamlalo index hiba: {e}")

IGAZOLAS = igazolas.Ellenorzo(
    cimzett=(os.environ.get("GOOGLE_WEB_CLIENT_ID") or "").strip() or igazolas.WEBES_KLIENS)
IGAZOLAS_SZAMLALO = igazolas.Szamlalo(igazolas_szamlalo)
atexit.register(IGAZOLAS_SZAMLALO.kiir)

# Az app ezeknél küldi az igazolást. Az érték: honnan jön a kérésben a
# Google-azonosító; None, ha a végpont a RevenueCat-azonosítóval dolgozik
# (azzal nincs mit összevetni, ott csak az érvényesség számít).
IGAZOLT_VEGPONTOK = {
    '/sync_list': lambda: kero_json().get('user_id'),
    '/get_list': lambda: request.args.get('user_id'),
    '/join_group': lambda: kero_json().get('user_id'),
    '/leave_group': lambda: kero_json().get('user_id'),
    '/list_members': lambda: request.args.get('user_id'),
    '/moderate_member': lambda: kero_json().get('requester_id'),
    '/ai_quota': None,
    '/analyze': None,
    '/upload_image': None,
    '/redeem_coupon': None,
    '/topup_credits': None,
}


@app.before_request
def igazolas_jegyzes():
    """Csak számol: soha nem utasít el, és hiba esetén sem állítja meg a
    kérést. A kulcs nélküli és a címenkénti korlát miatt elutasított kérések
    ide már nem jutnak el (az előző két kapuőr megállítja őket)."""
    if request.path not in IGAZOLT_VEGPONTOK:
        return
    try:
        eredmeny = IGAZOLAS.ellenoriz(request.headers.get(igazolas.FEJLEC))
        kinyero = IGAZOLT_VEGPONTOK[request.path]
        azonosito = szoveg(kinyero()) if kinyero else None
        eset = igazolas.eset(eredmeny, azonosito, osszevetheto=kinyero is not None)
        csoport = {igazolas.EGYEZIK: "igazolt", igazolas.NINCS: "igazolas_nelkul"}.get(eset)
        IGAZOLAS_SZAMLALO.jegyez(request.path.strip('/'), eset,
                                 felhasznalo=azonosito, csoport=csoport)
    except Exception as e:
        print(f"🔑 Igazolás: számlálási hiba ({type(e).__name__}).")

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


def revenuecat_subscriber_url(app_user_id):
    """A RevenueCat ügyfél-címe. Az azonosító kódolva kerül bele, hogy egy
    "/" vagy "?" ne vihesse a kérést a titkos kulccsal más címre."""
    return ("https://api.revenuecat.com/v1/subscribers/"
            + urllib.parse.quote(app_user_id, safe=''))


def _tranzakcio_alap(tx):
    """A Play-rendelésszám megújítás nélküli része: "GPA.1-2-3-4..5" -> "GPA.1-2-3-4"."""
    return tx.split("..")[0] if isinstance(tx, str) and tx else None


_TX_NAPLOZVA = False


def revenuecat_lekerdez(app_user_id):
    """(is_pro, extra_quota, keret_kulcsok). Ha a RevenueCat nem válaszol
    rendesen, RevenueCatUnavailable-t dob. (Régen ilyenkor csendben "nem Pro"
    lett, amiből az app "elfogyott a képkereted" ablakot csinált.)

    keret_kulcsok: kikhez számít egy elemzett kép. Az azonosító mellett az
    előfizetés és a +20-as vásárlás azonosítója (ujjlenyomatként), hogy
    újratelepítés és "vásárlás visszaállítása" után (új azonosító, ugyanaz
    az előfizetés) a havi keret ne kezdődjön elölről."""
    global _TX_NAPLOZVA
    req = urllib.request.Request(revenuecat_subscriber_url(app_user_id))
    req.add_header('Authorization', f'Bearer {REVENUECAT_SECRET_KEY}')
    req.add_header('Content-Type', 'application/json')

    is_pro = False
    extra_quota = 0
    kulcsok = ["id:" + ujjlenyomat(app_user_id)]
    now = datetime.now(timezone.utc)

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            # 201: a RevenueCat most hozta létre az ügyfelet (nincs vásárlása).
            if response.status not in (200, 201):
                raise RevenueCatUnavailable(f"HTTP {response.status}")
            data = json.loads(response.read().decode())
            subscriber = data.get("subscriber", {})
            subscriptions = subscriber.get("subscriptions") or {}

            entitlements = subscriber.get("entitlements", {})
            for ent_name, ent_data in entitlements.items():
                expires_date_str = ent_data.get("expires_date")
                aktiv = not expires_date_str
                if not aktiv:
                    expires_date = datetime.strptime(expires_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    aktiv = expires_date > now
                if aktiv:
                    is_pro = True
                    elofizetes = subscriptions.get(ent_data.get("product_identifier")) or {}
                    alap = _tranzakcio_alap(elofizetes.get("store_transaction_id"))
                    if alap:
                        kulcsok.append("tx:" + ujjlenyomat(alap))
                    if not _TX_NAPLOZVA:
                        _TX_NAPLOZVA = True
                        print(f"ℹ️ AI-keret: az előfizetés azonosítója "
                              f"{'megvan' if alap else 'NINCS meg'} a RevenueCat válaszában.")
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
                        if p_date > now - timedelta(days=30):
                            extra_quota += 20
                            tx = p.get("store_transaction_id") or p.get("id")
                            if isinstance(tx, str) and tx:
                                kulcsok.append("tx:" + ujjlenyomat(tx))

    except RevenueCatUnavailable:
        raise
    except Exception as e:
        print(f"🚨 RevenueCat hiba: {type(e).__name__}: {e}")
        raise RevenueCatUnavailable(str(e)) from e

    return is_pro, extra_quota, kulcsok


def get_user_status(app_user_id):
    """(is_pro, extra_quota), lásd revenuecat_lekerdez()."""
    is_pro, extra_quota, _ = revenuecat_lekerdez(app_user_id)
    return is_pro, extra_quota

AI_PRO_MONTHLY_QUOTA = 60


def ai_monthly_usage(app_user_id, now, kulcsok=()):
    """Az elmúlt 30 nap felhasznált képei. CSAK a sikeres képelemzés számít:
    az ai_naplo-ban más bejegyzés is "success" (pl. a +20 vásárlás naplója,
    topup_purchase), ami nélkül minden +20 vásárlás csak +19-et ért.

    Egy kép akkor is számít, ha más azonosítóval, de ugyanazzal az
    előfizetéssel készült (keret_kulcsok, lásd revenuecat_lekerdez), vagy ha
    az azonosító adatait azóta törölték (/delete_user_data)."""
    feltetel = [{"app_user_id": app_user_id}]
    if kulcsok:
        feltetel.append({"keret_kulcsok": {"$in": list(kulcsok)}})
    return ai_naplo.count_documents({
        "$or": feltetel,
        "action": "analyze_image",
        "timestamp": {"$gte": now - timedelta(days=30)},
        "status": "success"
    })


def ai_kero_azonosito(ertek):
    """Az AI-végpontok azonosítója (RevenueCat), vagy None, ha hiányzik/hibás."""
    return szoveg(ertek)


def ai_korlat(app_user_id):
    """Az AI-végpontok közös kéréskorlátja: cím és felhasználó."""
    return (cim_korlat("ip_ai")
            or felhasznalo_korlat(app_user_id, "f_ai_keres"))


def ai_total_quota(is_pro, extra_quota):
    """A 30 napos keret: Pro-nak 60, plusz minden 30 napon belüli +20."""
    return (AI_PRO_MONTHLY_QUOTA if is_pro else 0) + extra_quota


@app.route('/ai_quota', methods=['GET'])
def ai_quota():
    """A megmaradt AI-képek száma (az app ezt mutatja a "X/60 kép"
    címkén). A szerver az igazság forrása; a kliens csak tárolja."""
    app_user_id = ai_kero_azonosito(request.args.get('app_user_id'))
    if not app_user_id:
        return jsonify({"error": "Hiányzó azonosító!"}), 400
    tiltas = ai_korlat(app_user_id)
    if tiltas:
        return tiltas
    try:
        is_pro, extra_quota, kulcsok = revenuecat_lekerdez(app_user_id)
    except RevenueCatUnavailable:
        return jsonify({"error": "subscription_check_failed"}), 503
    total = ai_total_quota(is_pro, extra_quota)
    used = ai_monthly_usage(app_user_id, datetime.now(timezone.utc), kulcsok)
    return jsonify({
        "remaining": max(total - used, 0),
        "total": total,
        "is_pro": is_pro,
    }), 200


@app.route('/analyze', methods=['POST'])
def analyze_image():
    app_user_id = ai_kero_azonosito(request.form.get('app_user_id'))

    if not app_user_id:
         return jsonify({"error": "Hiányzó azonosító!"}), 400

    tiltas = ai_korlat(app_user_id)
    if tiltas:
        return tiltas

    # Egy azonosítóval egyszerre egy elemzés fut. Enélkül sok, egyszerre
    # beküldött kép mind átjutna az 5/perc és a havi keret ellenőrzésén,
    # mert a naplóba csak az elemzés végén kerülnek be.
    futo = felhasznalo_kulcs(app_user_id)
    if not VED.elkezd(futo):
        return jsonify({
            "error": "Túl sok kérés! 🚦\n\nKérlek, várj egy picit (kb. 1 percet) a következő kép elemzése előtt!"
        }), 429
    try:
        return _analyze_image(app_user_id)
    finally:
        VED.befejez(futo)


def _analyze_image(app_user_id):
    now = datetime.now(timezone.utc)

    # Hibakódok az appnak ("error"), emberi szöveg a "message"-ben:
    #   503 subscription_check_failed — a RevenueCat nem kérdezhető le
    #   403 not_pro                   — nincs Pro és nincs +20 kerete
    #   403 quota_exhausted           — elfogyott a képkeret
    # A régi app (1.0.78) csak a státuszkódot nézi: 403-ra a +20 ablakot
    # mutatja (mint eddig), 503-ra általános "próbáld újra" üzenetet.
    try:
        is_pro, extra_quota, kulcsok = revenuecat_lekerdez(app_user_id)
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

    monthly_usage = ai_monthly_usage(app_user_id, now, kulcsok)
    total_quota = ai_total_quota(is_pro, extra_quota)

    if monthly_usage >= total_quota:
        return jsonify({
            "error": "quota_exhausted",
            "remaining": 0,
            "total": total_quota,
            "message": f"Kimerítetted a keretedet! 🔒\n\nElhasználtad a rendelkezésre álló {total_quota} db AI fotódat az elmúlt 30 napban. Vásárolj extra csomagot a folytatáshoz!"
        }), 403

    if 'image' not in request.files: return jsonify({"error": "Nincs kép"}), 400

    # Óránként legfeljebb ennyi kép mehet elemzésre (vedelem.py, f_ai_kep).
    tiltas = felhasznalo_korlat_egy(app_user_id, "f_ai_kep")
    if tiltas:
        return tiltas

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
            "keret_kulcsok": kulcsok,
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
            "error_msg": str(e)[:500]
        })
        # A részletes hiba csak a naplóba kerül, az appnak általános szöveg.
        print(f"🚨 Képelemzési hiba: {type(e).__name__}")
        return jsonify({"error": "Most nem sikerült az elemzés. Kérlek, próbáld újra később!"}), 500

# ==============================================================================
# ☁️ KÉP TÁROLÁS ÉS KISZOLGÁLÁS
# ==============================================================================

def kep_tipusa(adat):
    """A kép típusa a fájl eleje alapján (nem a küldő szerint), vagy None,
    ha nem kép. Az app JPEG-et küld (a fájltípust nem adja meg); a régi
    képek között PNG, WebP, GIF és HEIC is lehet."""
    if not isinstance(adat, (bytes, bytearray)) or len(adat) < 12:
        return None
    if adat[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if adat[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if adat[:4] == b"RIFF" and adat[8:12] == b"WEBP":
        return "image/webp"
    if adat[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if adat[4:8] == b"ftyp" and adat[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    return None


@app.route('/upload_image', methods=['POST'])
def upload_image():
    tiltas = cim_korlat("ip_kep_ora", "ip_kep_nap")
    if tiltas:
        return tiltas

    if 'image' not in request.files:
        return jsonify({"error": "Nincs kép a kérésben"}), 400

    image_file = request.files['image']
    image_data = image_file.read()

    if len(image_data) > 500 * 1024:
        return jsonify({"error": "A kép túl nagy! Maximum 500KB engedélyezett."}), 400

    # Csak valódi kép mehet be: egy HTML- vagy SVG-fájl a szerver címéről
    # kiszolgálva weboldalként futna (pl. adathalászat a mi címünkön).
    tipus = kep_tipusa(image_data)
    if not tipus:
        return jsonify({"error": "Ez a fájl nem kép."}), 400

    image_id = str(uuid.uuid4())

    kepek_kollekcio.insert_one({
        "image_id": image_id,
        "image_data": image_data,
        "content_type": tipus,
        "created_at": datetime.now(timezone.utc)
    })

    host_url = request.host_url.rstrip('/')
    image_url = f"{host_url}/get_image/{image_id}"

    return jsonify({"status": "success", "image_url": image_url}), 200

@app.route('/get_image/<image_id>', methods=['GET'])
def get_image(image_id):
    if not szoveg(image_id, 64):
        return "Kép nem található", 404
    kep_dok = kepek_kollekcio.find_one({"image_id": image_id})

    if not kep_dok:
        return "Kép nem található", 404

    # A típust a tartalomból állapítjuk meg (a régi bejegyzéseknél a küldő
    # adta meg, ami bármi lehetett). Ami nem kép, az letöltendő fájl lesz,
    # és a böngésző nem futtatja.
    adat = kep_dok.get("image_data") or b""
    tipus = kep_tipusa(adat)
    response = Response(adat, mimetype=tipus or "application/octet-stream")
    if not tipus:
        response.headers['Content-Disposition'] = 'attachment'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
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

#
# 2026.11.06-I MÓDOSÍTÁS (biztonsági átnézés):
#   - Meglévő lista írásához a user_id KÖTELEZŐ (eddig nélküle bárki írhatott).
#     Minden appverzió küldi (az app feltöltés nélkül marad, ha nincs fiókja).
#   - Az előnézet (user_id nélküli /get_list) nem adja ki az Alapító
#     azonosítóját, csak a nevét: az azonosítóval bárki Alapítónak adhatta ki
#     magát (lista feloszlatása, tagok kitiltása). A tag a saját, user_id-s
#     lekérdezésében továbbra is megkapja.
#   - Az előnézet listaadata marad: a régi app (1.0.80) a csatlakozáskor ebből
#     építi fel a listát. Ezért a kitiltott tag user_id nélkül még olvashat;
#     ez a Google-igazolás kötelezővé tételével zárul le.
# ==============================================================================

def tagsag_tiltva(family_id, user_id):
    """
    True, ha a user_id-t BLOKKOLNI kell: nincs tagsági bejegyzése ehhez a
    listához, VAGY ki van tiltva. Ha nincs user_id megadva, nem blokkolunk
    (csak az előnézetnél fordulhat elő, lásd fenti megjegyzés; az írás
    user_id nélkül el sem jut idáig).
    """
    if not user_id:
        return False
    tag = tagok_kollekcio.find_one({"family_id": family_id, "user_id": user_id})
    if tag is None:
        return True
    return bool(tag.get("banned", False))


# Egy közös lista legfeljebb ennyi tétel és ennyi bájt (JSON-ként). Az app
# fájlmegnyitása is 5000 tételig enged; egy valódi lista ennek töredéke.
MAX_TETEL = 5000
MAX_LISTA_BAJT = 1024 * 1024


def lista_hibaja(list_data):
    """None, ha a feltöltött lista rendben van; különben (üzenet, kód)."""
    if not isinstance(list_data, dict):
        return "Hibás lista.", 400
    items = list_data.get("items", [])
    if not isinstance(items, list) or not all(isinstance(t, dict) for t in items):
        return "Hibás lista.", 400
    if len(items) > MAX_TETEL:
        return f"A lista túl hosszú (legfeljebb {MAX_TETEL} tétel).", 413
    try:
        meret = len(json.dumps(list_data, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return "Hibás lista.", 400
    if meret > MAX_LISTA_BAJT:
        return "A lista túl nagy.", 413
    return None


def kep_azonositok(list_data):
    """A listában hivatkozott, nálunk tárolt képek azonosítói."""
    azonositok = set()
    items = list_data.get("items") if isinstance(list_data, dict) else None
    for item in items if isinstance(items, list) else []:
        unit = item.get("unit", "") if isinstance(item, dict) else ""
        if isinstance(unit, str) and ":::" in unit:
            link = unit.split(":::")[1]
            if "/get_image/" in link:
                azonositok.add(link.split("/")[-1])
    return azonositok


def aktiv_tagok_szama(family_id):
    """
    A ténylegesen aktív (nem kitiltott) tagok száma — 2026.08.29-től ez a
    valós, felhasználóknak megjelenített taglétszám alapja, NEM az összes
    valaha regisztrált tag (ami a kitiltottakat is tartalmazná).
    """
    return tagok_kollekcio.count_documents(
        {"family_id": family_id, "banned": {"$ne": True}}
    )


def lista_torolve_jelol(family_id):
    """
    Megjegyzi, hogy ezt a kódot töröltük. A lista törlése ELŐTT kell hívni: ha
    utána a törlés elakad, a lista él tovább, és a bejegyzés nem zavar, mert
    minden végpont előbb magát a listát keresi.

    Miért kell: akinek az appja nem kapta meg a törlés értesítését (nem futott,
    nem volt internetje), az a következő feltöltésével újra létrehozná a listát.
    A feltöltésből ez nem különböztethető meg egy új listától, ezért a szervernek
    emlékeznie kell. Személyes adatot nem tárol. Nem jár le, mert egy telefon
    hónapokig lehet kikapcsolva.
    """
    if not family_id:
        return
    torolt_listak.update_one(
        {"family_id": family_id},
        {"$setOnInsert": {"deleted_at": int(datetime.now(timezone.utc).timestamp() * 1000)}},
        upsert=True
    )


def lista_torolve(family_id):
    """True, ha ezt a kódot korábban töröltük (lásd lista_torolve_jelol)."""
    return bool(family_id) and torolt_listak.find_one({"family_id": family_id}) is not None


# ==============================================================================
# ☁️ LISTA SZINKRONIZÁCIÓ (+ KUKÁSAUTÓ ÉS ALAPÍTÓ RÖGZÍTÉSE)
# ==============================================================================
#
# (A szerver-oldali időbélyeg és ütközés-védelem korábbi megjegyzése változatlan.)
# ==============================================================================
@app.route('/sync_list', methods=['POST'])
def sync_list():
    data = kero_json()
    family_id = kod(data.get('family_id'))
    if not family_id: return jsonify({"error": "Nincs id"}), 400
    user_id = szoveg(data.get('user_id'))
    if not user_id:
        return jsonify({"error": "Hiányzó azonosító!"}), 400

    list_data = data.get('list_data')
    hiba = lista_hibaja(list_data)
    if hiba:
        return jsonify({"error": hiba[0]}), hiba[1]

    base_timestamp = data.get('base_timestamp')
    if base_timestamp is not None and egesz(base_timestamp) is None:
        return hibas()

    tiltas = felhasznalo_korlat(user_id)
    if tiltas:
        return tiltas

    regi_csalad = kollekcio.find_one({"family_id": family_id})

    # Új közös lista: óránként legfeljebb ennyi címenként és felhasználónként.
    if not regi_csalad and not lista_torolve(family_id):
        tiltas = cim_korlat("ip_uj_lista") or felhasznalo_korlat_egy(user_id, "f_uj_lista")
        if tiltas:
            return tiltas

    # Törölt listát nem hozunk létre újra, akármelyik appverzió kéri. Élő
    # listánál ez nem fut le. Az új app a 410-ből tudja, hogy priváttá tegye a
    # listát; a régi app sima hibának veszi, és a tartalom nála megmarad.
    if not regi_csalad and lista_torolve(family_id):
        return jsonify({
            "error": "Ezt a közös listát törölték.",
            "exists": False,
            "deleted": True
        }), 410

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
            torlendo_kepek = kep_azonositok(regi_csalad["list_data"]) - kep_azonositok(list_data)
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

    tagok_kollekcio.update_one(
        {"family_id": family_id, "user_id": user_id},
        {"$setOnInsert": {"user_name": "Alapító", "joined_at": server_timestamp, "banned": False}},
        upsert=True
    )

    socketio.emit('list_updated', {"family_id": family_id, "timestamp": server_timestamp}, room=family_id)
    return jsonify({"status": "success", "timestamp": server_timestamp}), 200

def nem_letezo_kod_valasz(family_id):
    """A "nincs ilyen lista" válasza. Ha nem is mi töröltük, ez rossz kód:
    címenként csak korlátozott számban (a kódok végigpróbálása ellen)."""
    deleted = lista_torolve(family_id) if family_id else False
    if not deleted:
        tiltas = cim_korlat("ip_kodkereses")
        if tiltas:
            return tiltas
    # ÚJ: explicit "exists": False — a kliens ne a member_count==0-ra
    # (ami egy vadonatúj, de legitim listánál is előfordulhatna) hanem
    # erre a mezőre alapozza a "nem létező kód" döntést.
    return jsonify({
        "exists": False,
        # Csak akkor igaz, ha mi töröltük: üres adatbázisnál vagy ismeretlen kódnál
        # hamis, ezért az app ezen kívül semmitől nem tesz priváttá egy listát.
        "deleted": deleted,
        "list_data": {"items": []},
        "timestamp": 0,
        "member_count": 0,
        "owner_id": None,
        "owner_name": None
    }), 200


@app.route('/get_list', methods=['GET'])
def get_list():
    # ÚJ, OPCIONÁLIS paraméter: csak a folyamatos szinkron küldje el —
    # az előnézeti lekérdezés (csatlakozás előtt) NE küldje, hogy bárki
    # megnézhesse, kié egy kód, mielőtt ténylegesen csatlakozna.
    user_id = request.args.get('user_id')
    if user_id is not None:
        user_id = szoveg(user_id)
        if not user_id:
            return hibas()
        tiltas = felhasznalo_korlat(user_id)
        if tiltas:
            return tiltas

    # Hibás formájú kód: ilyen lista nincs (az app így a "nincs ilyen kód"
    # üzenetet mutatja, nem hálózati hibát).
    family_id = kod(request.args.get('family_id'))
    if not family_id:
        return nem_letezo_kod_valasz(None)

    csalad = kollekcio.find_one({"family_id": family_id})
    if not csalad:
        return nem_letezo_kod_valasz(family_id)

    if user_id and tagsag_tiltva(family_id, user_id):
        return jsonify({
            "error": "Nincs jogosultságod ehhez a listához.",
            "banned": True
        }), 403

    # Előnézet (user_id nélkül): ha a címről túl sok rossz kód jött, ez is áll.
    if not user_id:
        tiltas = cim_tiltva("kod")
        if tiltas:
            return tiltas

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
        # Csak a tag kapja meg (lásd a tagsag_tiltva fölötti megjegyzést).
        "owner_id": owner_id if user_id else None,
        "owner_name": owner_name
    }), 200

# ==============================================================================
# 📱 LEGFRISSEBB VERZIÓ LEKÉRDEZÉSE (induláskori frissítés-értesítéshez)
# ==============================================================================
@app.route('/latest_version', methods=['GET'])
def latest_version():
    # A "szerver" és a "cimfigyeles" csak ellenőrzéshez (melyik kód fut, és
    # megérkezik-e a Cloudflare címfejléce); az app csak a latest_version-t nézi.
    kero_cim()
    return jsonify({
        "latest_version": LATEST_VERSION,
        "szerver": SZERVER_VALTOZAT,
        "cimfigyeles": _CF_FEJLEC_LATTUK,
    }), 200

# ==============================================================================
# 🤝 CSALÁD KEZELŐ FUNKCIÓK
# ==============================================================================

@app.route('/join_group', methods=['POST'])
def join_group():
    data = kero_json()
    user_id = szoveg(data.get('user_id'))
    if not user_id:
        return jsonify({"error": "Hiányzó azonosító!"}), 400
    tiltas = felhasznalo_korlat(user_id)
    if tiltas:
        return tiltas
    user_name = nev(data.get('user_name'))
    family_id = kod(data.get('family_id'))
    if not family_id:
        tiltas = cim_korlat("ip_kodkereses")
        if tiltas:
            return tiltas
        return jsonify({"error": "Ehhez a kódhoz nem tartozik lista.", "exists": False}), 404

    # ÚJ: nem engedjük regisztrálni a tagságot/nevet nem létező (pl. elgépelt)
    # kódra — eddig ez "csendben sikeres" csatlakozásnak tűnt a kliens felől,
    # miközben valójában egy szellem-bejegyzés jött létre.
    # Idempotens marad: az Alapító (vagy bármely meglévő tag) a SAJÁT nevét
    # bármikor frissítheti ugyanezen a végponton, mert a saját listája már
    # mindig létezik ilyenkor.
    csalad = kollekcio.find_one({"family_id": family_id})
    if not csalad:
        if not lista_torolve(family_id):
            tiltas = cim_korlat("ip_kodkereses")
            if tiltas:
                return tiltas
        return jsonify({"error": "Ehhez a kódhoz nem tartozik lista.", "exists": False}), 404

    # ÚJ: ha valakit korábban kitiltottak, a kóddal való újra-"csatlakozás"
    # (join_group hívás) NE oldja fel automatikusan a tiltást — csak az
    # Alapító, a /moderate_member végponton keresztül teheti ezt meg.
    meglevo_tag = tagok_kollekcio.find_one({"family_id": family_id, "user_id": user_id})
    if meglevo_tag and meglevo_tag.get("banned", False):
        return jsonify({"error": "Ki lettél tiltva erről a listáról.", "banned": True}), 403

    # Új tag: ha a címről túl sok rossz kód jött, a csatlakozás is áll.
    if not meglevo_tag:
        tiltas = cim_tiltva("kod")
        if tiltas:
            return tiltas

    tagok_kollekcio.update_one(
        {"family_id": family_id, "user_id": user_id},
        {
            "$set": {"user_name": user_name, "joined_at": egesz(data.get('timestamp'))},
            "$setOnInsert": {"banned": False}
        },
        upsert=True
    )
    return jsonify({"status": "joined"}), 200

@app.route('/leave_group', methods=['POST'])
def leave_group():
    data = kero_json()
    user_id = szoveg(data.get('user_id'))
    if not user_id:
        return jsonify({"error": "Hiányzó azonosító!"}), 400
    tiltas = felhasznalo_korlat(user_id)
    if tiltas:
        return tiltas
    family_id = kod(data.get('family_id'))
    # Kód nélkül nincs miből kilépni (a régi viselkedés szerint 200).
    if not family_id:
        return jsonify({"status": "left"}), 200

    # ÚJ, 2026.08.29-i SZABÁLY: az Alapító nem egyszerűen "kilép" — ha ő
    # kezdeményezi a leválasztást (a kliens "Priváttá tétel" gombja, ami
    # az Alapítónál az EGYETLEN elérhető opció, a sima kilépés nála nem is
    # jelenik meg), az az EGÉSZ közös lista feloszlatását jelenti mindenki
    # számára — nem csak neki válik priváttá, hanem mindenkinek. A kliens
    # ugyanezt a végpontot hívja Alapítóként és nem-Alapítóként is; itt,
    # szerveroldalon dől el, melyik eset áll fenn.
    csalad = kollekcio.find_one({"family_id": family_id})

    if csalad and csalad.get("owner_id") == user_id:
        lista_torolve_jelol(family_id)
        try:
            for kep_id in kep_azonositok(csalad.get("list_data")):
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
    torles = tagok_kollekcio.delete_one({"family_id": family_id, "user_id": user_id})
    # Aki nem volt tag, az nem törölhet listát (eddig egy tag nélküli listát
    # bárki törölhetett a kóddal).
    if torles.deleted_count == 0:
        return jsonify({"status": "left"}), 200
    maradek_tagok = tagok_kollekcio.count_documents({"family_id": family_id})

    if maradek_tagok == 0:
        lista_torolve_jelol(family_id)
        kollekcio.delete_one({"family_id": family_id})
        socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)
        return jsonify({"status": "last_member_left"}), 200
        
    socketio.emit('list_updated', {"family_id": family_id}, room=family_id)
    return jsonify({"status": "left"}), 200

@app.route('/update_token', methods=['POST'])
def update_token():
    data = kero_json()
    user_id = szoveg(data.get('user_id'))
    fcm_token = data.get('fcm_token')
    if not user_id or (fcm_token is not None and not szoveg(fcm_token, 4096)):
        return hibas()
    tiltas = felhasznalo_korlat(user_id)
    if tiltas:
        return tiltas

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
    family_id = kod(request.args.get('family_id'))
    requester_id = szoveg(request.args.get('user_id'))
    if not family_id or not requester_id:
        return jsonify({"error": "Hiányzó paraméter"}), 400
    tiltas = felhasznalo_korlat(requester_id)
    if tiltas:
        return tiltas

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
    data = kero_json()
    family_id = kod(data.get('family_id'))
    requester_id = szoveg(data.get('requester_id'))
    target_user_id = szoveg(data.get('target_user_id'))
    banned = data.get('banned')  # true = kitiltás, false = visszaengedés

    if not all([family_id, requester_id, target_user_id]) or not isinstance(banned, bool):
        return jsonify({"error": "Hiányzó paraméter"}), 400
    tiltas = felhasznalo_korlat(requester_id)
    if tiltas:
        return tiltas

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
    data = kero_json()
    user_id = szoveg(data.get('user_id'))
    # Csak napló: a keretet a RevenueCat vásárlásaiból számoljuk, nem ebből.
    credits = egesz(data.get('credits', 20))
    if credits is None or not 0 <= credits <= 1000:
        credits = 20

    if not user_id:
        return jsonify({"error": "Hiányzó user_id"}), 400
    tiltas = felhasznalo_korlat(user_id)
    if tiltas:
        return tiltas

    now = datetime.now(timezone.utc)

    # Az új app-verziók a vásárlás tranzakció-azonosítóját is küldik, és
    # sikertelen válasznál újrapróbálják. Ugyanazt az azonosítót csak egyszer
    # írjuk jóvá; ismétlésnél 200 (hogy a kliens kivegye a sorából), de
    # credits_added: 0. Azonosító nélkül (régi app) marad a régi viselkedés.
    transaction_id = (szoveg(data.get('transaction_id'), 256) or '').strip()
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
    data = kero_json()
    user_id = szoveg(data.get('user_id'))

    if not user_id:
        return jsonify({"error": "Hiányzó user_id"}), 400
    tiltas = felhasznalo_korlat(user_id)
    if tiltas:
        return tiltas

    # 1. AI napló törlése (+ a feldolgozott top-up tranzakciók). Kivétel: az
    # utolsó 30 nap elemzett képei azonosító NÉLKÜL maradnak (csak a keret
    # ujjlenyomata marad), különben a törléssel a havi keret elölről
    # kezdődne. 30 nap után már nem számítanak.
    hatar = datetime.now(timezone.utc) - timedelta(days=31)
    sajat_kulcs = "id:" + ujjlenyomat(user_id)
    for bejegyzes in ai_naplo.find(
            {"app_user_id": user_id, "action": "analyze_image", "status": "success",
             "timestamp": {"$gte": hatar}},
            {"_id": 1, "keret_kulcsok": 1}):
        kulcsok = bejegyzes.get("keret_kulcsok")
        if not isinstance(kulcsok, list) or not kulcsok:
            kulcsok = [sajat_kulcs]
        ai_naplo.update_one({"_id": bejegyzes["_id"]},
                            {"$set": {"keret_kulcsok": kulcsok}, "$unset": {"app_user_id": ""}})
    ai_naplo.delete_many({"app_user_id": user_id})
    topup_tranzakciok.delete_many({"app_user_id": user_id})

    # 2. Minden csoportból kiléptetés + üres csoportok törlése
    user_groups = list(tagok_kollekcio.find({"user_id": user_id}))
    for group in user_groups:
        family_id = group.get("family_id")
        tagok_kollekcio.delete_one({"family_id": family_id, "user_id": user_id})

        maradek = tagok_kollekcio.count_documents({"family_id": family_id})
        if maradek == 0:
            lista_torolve_jelol(family_id)
            # Ha ő volt az utolsó tag, a lista és a képek is törlődnek
            lista_dok = kollekcio.find_one({"family_id": family_id})
            if lista_dok:
                for kep_id in kep_azonositok(lista_dok.get("list_data")):
                    kepek_kollekcio.delete_one({"image_id": kep_id})
            kollekcio.delete_one({"family_id": family_id})
            socketio.emit('group_deleted', {"family_id": family_id}, room=family_id)

    print(f"🗑️ GDPR törlés elvégezve: {ujjlenyomat(user_id)}")
    return jsonify({"status": "deleted", "message": "Minden adat törölve"}), 200


# ==============================================================================
# 🔔 REVENUECAT WEBHOOK (Lemondás / Lejárat / Fizetési probléma)
# ==============================================================================
# A RevenueCat a webhookban az Authorization fejlécben azt a szöveget küldi,
# amit a felületén (Integrations → Webhooks → Authorization header value)
# megadtunk; a Renderen ugyanez a REVENUECAT_WEBHOOK_AUTH. Ha a változó
# nincs beállítva, a webhook zárva (401): a webhook csak naplóz, így ez nem
# rontja el a vásárlásokat.
REVENUECAT_WEBHOOK_AUTH = _env_key("REVENUECAT_WEBHOOK_AUTH")
if not REVENUECAT_WEBHOOK_AUTH:
    print("🚨 Nincs beállítva REVENUECAT_WEBHOOK_AUTH: a /webhook zárva (401).")


def webhook_jogos():
    kapott = (request.headers.get('Authorization') or '').strip().encode('utf-8')
    return bool(REVENUECAT_WEBHOOK_AUTH) and bool(kapott) and hmac.compare_digest(
        kapott, REVENUECAT_WEBHOOK_AUTH.encode('utf-8'))


@app.route('/webhook', methods=['POST'])
def revenuecat_webhook():
    if not webhook_jogos():
        return jsonify({"error": "unauthorized"}), 401

    data = kero_json()

    if not data:
        return jsonify({"error": "Üres webhook"}), 400

    event = data.get('event')
    if not isinstance(event, dict):
        return hibas()
    event_type = szoveg(event.get('type'), 64)
    app_user_id = szoveg(event.get('app_user_id'))
    product_id = szoveg(event.get('product_id'), 200) or ''
    expiration_at_ms = egesz(event.get('expiration_at_ms'))

    print(f"📣 RevenueCat webhook: {event_type} - "
          f"{ujjlenyomat(app_user_id) if app_user_id else '-'} - {product_id}")

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
    family_id = kod(data.get('family_id')) if isinstance(data, dict) else None
    if family_id:
        join_room(family_id)

@socketio.on('leave_room')
def handle_leave_room(data):
    """Amikor bezárja a közös listát, elhagyja a csatornát."""
    family_id = kod(data.get('family_id')) if isinstance(data, dict) else None
    if family_id:
        leave_room(family_id)

# ==============================================================================

def kupon_korlat(app_user_id):
    """A kuponbeváltás kéréskorlátja (coupons.py hívja)."""
    return (cim_korlat("ip_kupon")
            or felhasznalo_korlat(app_user_id, "f_kupon"))


register_coupon_routes(app, kuponok_kollekcio, REVENUECAT_SECRET_KEY,
                       korlat=kupon_korlat, subscriber_url=revenuecat_subscriber_url)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
