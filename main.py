import os
from flask import Flask, request, jsonify
from openai import OpenAI
import base64
import json

app = Flask(__name__)

# ==============================================================================
# 🔑 KULCS BEÁLLÍTÁSA (BEÍRTAM A KULCSODAT!)
# ==============================================================================
API_KEY = "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"

print(f"✅ API Kulcs beégetve a kódba (Vége: ...{API_KEY[-4:]})")

client = OpenAI(api_key=API_KEY)

def encode_image(image_file):
    return base64.b64encode(image_file.read()).decode('utf-8')

@app.route('/analyze', methods=['POST'])
def analyze_image():
    if 'image' not in request.files:
        return jsonify({"error": "Nincs kép feltöltve"}), 400
    
    image = request.files['image']
    base64_image = encode_image(image)

    print("\n📸 --- KÉP ÉRKEZETT PC-RE ---")
    print("Elemzés indítása a GPT-4o modellel (JSON mód)...")

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
        
        # Visszaküldjük a tiszta JSON-t a telefonnak
        return result_content, 200, {'Content-Type': 'application/json'}

    except Exception as e:
        print(f"❌ HIBA TÖRTÉNT: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # A 0.0.0.0 cím fontos, hogy a telefon megtalálja a hálózaton!
    print(f"🚀 SZERVER FUT ITT: http://0.0.0.0:5000")
    print("Várakozás a telefon kérésére...")
    app.run(host='0.0.0.0', port=5000)