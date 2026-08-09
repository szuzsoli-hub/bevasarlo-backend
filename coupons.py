import random
import string
import re
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify

coupons_bp = Blueprint('coupons', __name__)

ADMIN_SECRET = "TesztAdmin2026Titkos!"  # ezt cseréld le saját titkos szövegre


def generate_coupon_code():
    part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{part1}-{part2}"


def _grant_promotional_entitlement(app_user_id, revenuecat_secret_key,
                                    entitlement_id='pro_mode', days=180):
    """
    Közvetlenül, Google Play vásárlás NÉLKÜL ad [entitlement_id] jogosultságot
    a felhasználónak [days] napra a RevenueCat "Grant a Promotional
    Entitlement" API-ján keresztül.

    Ez azért kell, mert a Google Play soha nem engedi egy már-volt-előfizető
    fióknak "újra megvenni" ugyanazt a terméket, bármilyen ajánlattal
    próbálkozunk (a "Fejlesztő által meghatározott" jogosultság ezt NEM
    tudja felülírni - ez egy Play-szintű, nem eligibility-szintű
    korlátozás). A promotional entitlement grant teljesen megkerüli a
    Play Billing-et, ezért ez a tesztelőknél (akik jellemzően már voltak
    korábban előfizetők/tesztelők) is mindig működik.

    Visszatérési érték: (siker: bool, hibaüzenet: str | None)
    """
    end_time_ms = int(
        (datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000
    )
    url = (
        f'https://api.revenuecat.com/v1/subscribers/{app_user_id}'
        f'/entitlements/{entitlement_id}/promotional'
    )
    payload = json.dumps({'end_time_ms': end_time_ms}).encode('utf-8')

    req = urllib.request.Request(url, data=payload, method='POST')
    req.add_header('Authorization', f'Bearer {revenuecat_secret_key}')
    req.add_header('Content-Type', 'application/json')

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                return True, None
            return False, f'RevenueCat HTTP {resp.status}'
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        return False, f'RevenueCat hiba {e.code}: {body}'
    except Exception as e:
        return False, str(e)


def register_coupon_routes(app, kuponok_kollekcio, revenuecat_secret_key):
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

        # EZ AZ EGYETLEN MÓDOSÍTÁS: a nyers kód helyett a teljes,
        # koppintható linket írjuk ki, soronként egyet - így egyenként
        # is kijelölhető és másolható mindegyik.
        links = [
            f"https://bevasarlo-backend.onrender.com/redeem?code={code}"
            for code in generated
        ]
        return "\n".join(links), 200, {'Content-Type': 'text/plain; charset=utf-8'}

    @app.route('/redeem_coupon', methods=['POST'])
    def redeem_coupon():
        data = request.get_json()
        code = (data.get('code') or '').strip().upper()
        app_user_id = data.get('app_user_id')
        if not code or not app_user_id:
            return jsonify({"error": "missing_fields"}), 400

        # Atomikusan foglaljuk le a kódot (find_one_and_update), hogy két
        # egyidejű kérés ne válthassa be ugyanazt a kódot kétszer.
        kupon = kuponok_kollekcio.find_one_and_update(
            {"code": code, "redeemed": False},
            {"$set": {
                "redeemed": True,
                "redeemed_at": datetime.now(timezone.utc),
                "redeemed_by": app_user_id
            }}
        )

        if kupon is None:
            letezik = kuponok_kollekcio.find_one({"code": code})
            if not letezik:
                return jsonify({"error": "invalid_code", "message": "Érvénytelen kód"}), 404
            return jsonify({"error": "already_redeemed", "message": "Ez a kód már fel lett használva"}), 409

        ok, hiba = _grant_promotional_entitlement(app_user_id, revenuecat_secret_key)

        if not ok:
            # Ha a RevenueCat hívás elbukott, visszaállítjuk a kódot
            # beválthatóra, hogy a felhasználó (vagy te) újra
            # próbálkozhasson - a kód nem "égett el" hiába.
            kuponok_kollekcio.update_one(
                {"code": code},
                {"$set": {"redeemed": False, "redeemed_at": None, "redeemed_by": None}}
            )
            print(f"❌ RevenueCat entitlement grant hiba ({app_user_id}): {hiba}")
            return jsonify({
                "error": "grant_failed",
                "message": "A kupon beváltása most nem sikerült. Kérlek próbáld újra néhány perc múlva!"
            }), 502

        return jsonify({"status": "success"}), 200

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
