import os
from flask import Flask, request, jsonify
from openai import OpenAI
import base64
import json
from pymongo import MongoClient

app = Flask(__name__)

# ==============================================================================
# 🛡️ BIZTONSÁGI PAJZS (KAPUŐR) A FLUTTER APP-HOZ
# ==============================================================================
EXPECTED_API_KEY = "v9X$kL2#pQ8@mZ5*eR1!tY7^bN4&hW3xM9"

@app.before_request
def require_api_key():
    # A főoldalt (/) átengedjük, hogy a böngészőben lásd, hogy fut a szerver
    if request.path == '/':
        return

    # Minden más végpontnál kérjük az igazolványt!
    client_key = request.headers.get('X-API-KEY')
    if client_key != EXPECTED_API_KEY:
        print(f"🚨 ILLETÉKTELEN BEHATOLÁSI KÍSÉRLET! Kapott kulcs: {client_key}")
        return jsonify({"error": "Hozzáférés megtagadva. Érvénytelen API kulcs!"}), 401

# ==============================================================================
# 🔒 BIZTONSÁGOS KULCS BETÖLTÉS (Render Environment-ből)
# ==============================================================================
API_KEY = os.environ.get("API_KEY")

if not API_KEY:
    print("❌ HIBA: Nem találom az API_KEY környezeti változót!")
else:
    print(f"✅ API Kulcs sikeresen betöltve a titkos tárolóból.")

client = OpenAI(api_key=API_KEY)

# ==============================================================================
# 🗄️ MONGODB ADATBÁZIS ÖSSZEKÖTÉS
# ==============================================================================
MONGO_URI = os.environ.get("MONGO_URI")

if not MONGO_URI:
    print("❌ HIBA: Nem találom a MONGO_URI környezeti változót!")
else:
    print("✅ MongoDB Link betöltve.")
    
# Itt mongo_client-nek hívjuk, hogy ne vesszen össze az OpenAI client-jével!
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["bevasarlo_adatbazis"]
kollekcio = db["listak"]

def encode_image(image_file):
    return base64.b64encode(image_file.read()).decode('utf-8')

@app.route('/', methods=['GET'])
def index():
    return "Bevasarlo Backend (OpenAI GPT-4o + MongoDB Sync) is running!"

@app.route('/analyze', methods=['POST'])
def analyze_image():
    if 'image' not in request.files:
        return jsonify({"error": "Nincs kép feltöltve"}), 400
    
    image = request.files['image']
    base64_image = encode_image(image)

    print("\n📸 --- KÉP ÉRKEZETT ---")
    print("Elemzés indítása a GPT-4o modellel...")

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

        result_content = response.choices[0].message.content
        print("✅ SIKER! A GPT válasza:")
        print(result_content)
        
        return result_content, 200, {'Content-Type': 'application/json'}

    except Exception as e:
        print(f"❌ HIBA TÖRTÉNT: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# ☁️ ÚJ FUNKCIÓK: REAL-TIME SZINKRONIZÁCIÓ (MongoDB-vel)
# ==============================================================================

@app.route('/sync_list', methods=['POST'])
def sync_list():
    """Ide küldi az app a frissített listát."""
    try:
        data = request.get_json()
        family_id = data.get('family_id')
        list_data = data.get('list_data')
        timestamp = data.get('timestamp')

        if not family_id:
            return jsonify({"error": "Hiányzó family_id"}), 400

        # Mentés adatbázisba (upsert=True: ha nincs még ilyen kód, létrehozza, ha van, frissíti)
        kollekcio.update_one(
            {"family_id": family_id},
            {"$set": {
                "list_data": list_data,
                "timestamp": timestamp
            }},
            upsert=True
        )
        
        print(f"✅ Lista mentve a MongoDB-be a csoporthoz: {family_id}")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"❌ Szinkron hiba: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_list', methods=['GET'])
def get_list():
    """Innen kéri le az app a családtagok módosításait."""
    family_id = request.args.get('family_id')
    
    if not family_id:
        return jsonify({"error": "Hiányzó family_id"}), 400
        
    # Lekérdezés az adatbázisból
    csalad = kollekcio.find_one({"family_id": family_id})

    if csalad:
        # A _id mezőt a MongoDB generálja, de a Flutter nem tudja értelmezni, így azt kihagyjuk a válaszból
        return jsonify({
            "list_data": csalad.get("list_data", []),
            "timestamp": csalad.get("timestamp")
        }), 200
    else:
        return jsonify({"error": "Nincs adat ehhez a csoporthoz"}), 404

# ==============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
