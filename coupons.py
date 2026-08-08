import random
import string
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
