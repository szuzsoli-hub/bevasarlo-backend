import os
from flask import Flask, request, jsonify
from openai import OpenAI
import base64
import json

app = Flask(__name__)

# --- BIZTONSÁGOS KULCS KEZELÉS ---
# Itt már nincs benne a kulcs szövegesen!
# A program a Render beállításaiból (Environment Variables) fogja kiolvasni.
API_KEY = os.getenv("API_KEY")

# Ellenőrzés (csak hogy lásd a logokban, ha véletlenül hiányzik)
if not API_KEY:
    print("⚠️ FIGYELEM: Nincs beállítva az API_KEY környezeti változó!")

client = OpenAI(api_key=API_KEY)

def encode_image(image_file):
    return base64.b64encode(image_file.read()).decode('utf-8')

@app.route('/analyze', methods=['POST'])
def analyze_image():
    if 'image' not in request.files:
        return jsonify({"error": "Nincs kép feltöltve"}), 400
    
    image = request.files['image']
    base64_image = encode_image(image)

    print("📸 Kép érkezett a felhőbe, elemzés az 5 pontos stratégia szerint...")

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """
                    Te egy profi magyar áruházi adatfeldolgozó AI vagy.
                    A feladatod: Kinyerni az adatokat egy termék fotójáról (akciós újság kivágás vagy árcédula).
                    
                    A következő 5 adatot keresd meg és add vissza JSON formátumban:
                    1. "product_name": A termék pontos neve (Márka + Típus).
                    2. "packaging": Kiszerelés / Mennyiség (pl. "500 g", "1,5 l", "10 db", "dobozos"). Ha nincs, legyen "".
                    3. "price_single": Az 1 darabos (vagy normál) ár. CSAK SZÁM! (pl. 1299).
                    4. "price_multi": A több darabos (akciós) ár, ha van ilyen (pl. "2 db esetén" ár). CSAK SZÁM! Ha nincs, legyen "".
                    5. "multi_condition": A feltétel a több darabos árhoz (pl. "2 db esetén", "3 db-tól"). Ha nincs, legyen "".
                    6. "unit_price": Egységár (pl. "2499 Ft/kg", "500 Ft/l"). Ezt szövegesen hagyd meg.

                    Szabályok:
                    - Ha két ár van (egy nagy akciós és egy kisebb egységár), a nagyobbetűs a "price_single".
                    - Ha van "X db esetén" ár, az a "price_multi".
                    - A válaszod KIZÁRÓLAG a nyers JSON legyen, semmi más szöveg.
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
        print("✅ OpenAI válasz:", result_content)
        
        return result_content, 200, {'Content-Type': 'application/json'}

    except Exception as e:
        print("❌ Hiba történt:", str(e))
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # A Render dinamikus portot használ, ezért ezt így kell megadni:
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)