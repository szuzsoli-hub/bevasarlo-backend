import random
import string
import re
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

coupons_bp = Blueprint('coupons', __name__)

ADMIN_SECRET = "TesztAdmin2026Titkos!"  # ezt cseréld le saját titkos szövegre


def generate_coupon_code():
    part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{part1}-{part2}"


def register_coupon_routes(app, kuponok_kollekcio):
    @app.route('/admin_generate_coupons', methods=['GET'])
    def admin_generate_coupons():
        secret = request.args.get('secret')
        if secret != ADMIN_SECRET:
            return jsonify({"error": "unauthorized"}), 401
        count = int(request.args.get('count', 20))
        generated = []
        for _ in range(count):
            code = generate_coupon_code()
            while kuponok_kollekcio.find_one({"code": code}):
                code = generate_coupon_code()
            kuponok_kollekcio.insert_one({
                "code": code,
                "redeemed": False,
                "redeemed_at": None,
                "redeemed_by": None,
                "created_at": datetime.now(timezone.utc)
            })
            generated.append(code)
        return jsonify({"status": "success", "codes": generated}), 200

    @app.route('/redeem_coupon', methods=['POST'])
    def redeem_coupon():
        data = request.get_json()
        code = (data.get('code') or '').strip().upper()
        app_user_id = data.get('app_user_id')
        if not code or not app_user_id:
            return jsonify({"error": "missing_fields"}), 400
        kupon = kuponok_kollekcio.find_one({"code": code})
        if not kupon:
            return jsonify({"error": "invalid_code", "message": "Érvénytelen kód"}), 404
        if kupon.get("redeemed"):
            return jsonify({"error": "already_redeemed", "message": "Ez a kód már fel lett használva"}), 409
        kuponok_kollekcio.update_one(
            {"code": code},
            {"$set": {
                "redeemed": True,
                "redeemed_at": datetime.now(timezone.utc),
                "redeemed_by": app_user_id
            }}
        )
        return jsonify({
            "status": "success",
            "offer_id": "pro-tester-180days"
        }), 200

    @app.route('/redeem', methods=['GET'])
    def redeem_landing_page():
        """
        Kattintható https link a Brevo kampányokhoz.

        Ezt a linket küldjük ki emailben a bevasarlolista:// custom scheme
        HELYETT, mert az utóbbi a legtöbb email/SMS kliensben nem
        kattintható. Ez az oldal megnyitáskor azonnal megpróbálja a
        telepített appot elindítani a custom scheme-mel; ha nem sikerül
        (mert az app nincs telepítve), 1.5 mp után a Play Store oldalra
        irányít.
        """
        code = (request.args.get('code') or '').strip().upper()

        # Egyszerű validáció - ne engedjünk be akármilyen szemetet a
        # deep linkbe (XSS ellen is védelem, mert közvetlenül string
        # interpolációval kerül a HTML-be).
        if not re.fullmatch(r'[A-Z0-9]{4}-[A-Z0-9]{4}', code):
            code = ''

        deep_link = f'bevasarlolista://redeem-coupon?code={code}' if code else ''
        play_store_url = (
            'https://play.google.com/store/apps/details'
            '?id=com.bevasarlolista.app'
        )

        html = f'''<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bevásárlólista - Kupon beváltása</title>
<style>
  body {{
    font-family: -apple-system, Roboto, Arial, sans-serif;
    background: #f5f5f5;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    margin: 0;
    text-align: center;
    padding: 24px;
    box-sizing: border-box;
  }}
  .card {{
    background: white;
    border-radius: 16px;
    padding: 32px 24px;
    max-width: 360px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
  }}
  .icon {{ font-size: 48px; margin-bottom: 12px; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; color: #1a1a1a; }}
  p {{ color: #666; font-size: 14px; line-height: 1.5; }}
  .code {{
    font-family: monospace;
    font-size: 18px;
    font-weight: bold;
    letter-spacing: 2px;
    background: #f0f0f0;
    padding: 8px 16px;
    border-radius: 8px;
    display: inline-block;
    margin: 12px 0;
  }}
  a.btn {{
    display: inline-block;
    margin-top: 16px;
    background: #2e7d32;
    color: white;
    text-decoration: none;
    padding: 12px 24px;
    border-radius: 8px;
    font-weight: bold;
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">🎁</div>
    <h1>Kupon megnyitása...</h1>
    <p>Ha telepítve van az app, most automatikusan megnyílik.</p>
    {f'<div class="code">{code}</div>' if code else ''}
    <p id="fallback-text" style="display:none;">
      Úgy tűnik, még nincs telepítve az app. Irányítunk a Play Áruházba...
    </p>
    <a class="btn" href="{play_store_url}">Megnyitás a Play Áruházban</a>
  </div>

<script>
  (function() {{
    var deepLink = {deep_link!r};
    var playStore = {play_store_url!r};
    if (!deepLink) return;

    var opened = false;
    document.addEventListener('visibilitychange', function() {{
      if (document.hidden) opened = true;
    }});

    window.location.href = deepLink;

    setTimeout(function() {{
      if (!opened) {{
        document.getElementById('fallback-text').style.display = 'block';
        window.location.href = playStore;
      }}
    }}, 1500);
  }})();
</script>
</body>
</html>'''
        return html, 200, {'Content-Type': 'text/html; charset=utf-8'}
