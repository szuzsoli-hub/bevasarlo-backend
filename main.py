import os
from flask import Flask, request, jsonify
from openai import OpenAI
import base64
import json
from pymongo import MongoClient

app = Flask(__name__)

# ==============================================================================
# 🛡️ BIZTONSÁGI PAJZS (KAPUŐR)
# ==============================================================================
EXPECTED_API_KEY = "v9X$kL2#pQ8@mZ5*eR1!tY7^bN4&hW3xM9"

@app.before_request
def require_api_key():
    if request.path == '/': return
    client_key = request.headers.get('X-API-KEY')
    if client_key != EXPECTED_API_KEY:
        return jsonify({"error": "Hozzáférés megtagadva. Érvénytelen API kulcs!"}), 401

# ==============================================================================
# 🔒 KULCSOK ÉS ADATBÁZIS (Render Environment)
# ==============================================================================
API_KEY = os.environ.get("API_KEY")
client = OpenAI(api_key=API_KEY)

MONGO_URI = os.environ.get("MONGO_URI")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["bevasarlo_adatbazis"]
kollekcio = db["listak"]
tagok_kollekcio = db["csoport_tagok"] # Új tábla a tagoknak

def encode_image(image_file):
    return base64.b64encode(image_file.read()).decode('utf-8')

@app.route('/', methods=['GET'])
def index():
    return "Bevasarlo Backend (Full Cloud Sync + AI) is running!"

# ==============================================================================
# 📸 AI KÉPFELISMERÉS
# ==============================================================================
@app.route('/analyze', methods=['POST'])
def analyze_image():
    if 'image' not in request.files: return jsonify({"error": "Nincs kép"}), 400
    image = request.files['image']
    base64_image = encode_image(image)
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Te egy profi magyar áruházi adatfeldolgozó AI vagy..."},
                      {"role": "user", "content": [{"type": "text", "text": "Elemezd a képet!"},
                                                   {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
            response_format={"type": "json_object"}
        )
        return response.choices[0].message.content, 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# ☁️ LISTA SZINKRONIZÁCIÓ
# ==============================================================================
@app.route('/sync_list', methods=['POST'])
def sync_list():
    data = request.get_json()
    family_id = data.get('family_id')
    if not family_id: return jsonify({"error": "Nincs id"}), 400
    kollekcio.update_one({"family_id": family_id}, 
                         {"$set": {"list_data": data.get('list_data'), "timestamp": data.get('timestamp')}}, 
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
