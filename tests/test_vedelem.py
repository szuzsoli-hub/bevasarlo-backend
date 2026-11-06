# A 2026.11.06-i biztonsági javítás tesztjei (vedelem.py és a végpontok).
#
# Mit ellenőriz:
#   - a 11.06-i átnézés helyi bizonyításai (pl. egy kéréssel minden lista
#     törlése) már nem mennek át;
#   - a túl sok kérés ideiglenes tiltást kap (címenként, felhasználónként,
#     sok címről egyszerre), a rendes használat (10–20 fotó egymás után, egy
#     család egy címről) nem;
#   - a meglévő AI-védelmek (Pro, havi keret, 5/perc) nem játszhatók ki:
#     egyszerre beküldött képekkel, adattörléssel, új azonosítóval;
#   - a régi app (1.0.80) kérései változatlanul mennek.
#
# Valódi adatbázis, RevenueCat és OpenAI nem kell: minden memóriában fut.
#
# Futtatás:
#   pip install -r requirements.txt -r requirements-dev.txt
#   python -m pytest tests -q

import importlib
import io
import json
import os
import sys
import threading
import types
from datetime import datetime, timezone
from unittest import mock

import mongomock
import pytest

KULCS = "teszt-kulcs"
FEJLEC = {"X-API-KEY": KULCS}
WEBHOOK_KULCS = "teszt-webhook-fejlec"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def cimrol(n, **tobb):
    """Fejléc egy adott címről (a Cloudflare fejlécével)."""
    return dict(FEJLEC, **{"CF-Connecting-IP": f"203.0.113.{n}"}, **tobb)


@pytest.fixture(scope="module")
def szerver():
    os.environ["BACKEND_API_KEY"] = KULCS
    os.environ.pop("BACKEND_API_KEY_OLD", None)
    os.environ["API_KEY"] = "nem-hasznalt"
    os.environ["REVENUECAT_WEBHOOK_AUTH"] = WEBHOOK_KULCS
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=lambda **_: object())
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with mock.patch("pymongo.MongoClient", mongomock.MongoClient):
        sys.modules.pop("main", None)
        modul = importlib.import_module("main")
    os.environ.pop("REVENUECAT_WEBHOOK_AUTH", None)
    return modul


@pytest.fixture()
def ora(szerver, monkeypatch):
    """Kézzel léptethető óra a védelemnek és a szerver datetime.now()-jának."""
    t = [1_800_000_000.0]
    monkeypatch.setattr(szerver.VED, "_ido", lambda: t[0])

    class Ido(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(t[0], tz or timezone.utc)

    monkeypatch.setattr(szerver, "datetime", Ido)
    return t


@pytest.fixture()
def app(szerver, ora):
    for g in (szerver.kollekcio, szerver.tagok_kollekcio, szerver.torolt_listak,
              szerver.kepek_kollekcio, szerver.ai_naplo, szerver.topup_tranzakciok,
              szerver.vedelem_naplo, szerver.kuponok_kollekcio):
        g.delete_many({})
    szerver.VED.torol()
    return szerver.app.test_client()


def feltolt(app, kod, user, base=0, fejlec=FEJLEC, tetelek=("Tej",)):
    return app.post("/sync_list", headers=fejlec, json={
        "family_id": kod, "user_id": user, "base_timestamp": base,
        "list_data": {"storeName": kod, "items": [{"id": t, "name": t} for t in tetelek]},
    })


def csatlakozik(app, kod, user, fejlec=FEJLEC):
    return app.post("/join_group", headers=fejlec, json={
        "family_id": kod, "user_id": user, "user_name": user, "timestamp": 1})


def ket_csalad(app):
    for kod, alapito, tag in (("AAA111", "anna", "bela"), ("BBB222", "cili", "dani")):
        assert feltolt(app, kod, alapito).status_code == 200
        assert csatlakozik(app, kod, tag).status_code == 200


# ---------------------------------------------------------------------------
# AI: hamis RevenueCat és OpenAI
# ---------------------------------------------------------------------------

@pytest.fixture()
def ai(szerver, monkeypatch):
    """Minden azonosító Pro; a kulcsok: a saját, plusz az esetleges
    előfizetés (allapot["tx"][azonosító])."""
    allapot = {"tx": {}, "pro": True, "openai": 0, "varj": None}

    def lekerdez(app_user_id):
        kulcsok = ["id:" + szerver.ujjlenyomat(app_user_id)]
        tx = allapot["tx"].get(app_user_id)
        if tx:
            kulcsok.append("tx:" + szerver.ujjlenyomat(tx))
        return allapot["pro"], 0, kulcsok

    def create(**_):
        allapot["openai"] += 1
        if allapot["varj"]:
            allapot["varj"].wait(5)
        uzenet = types.SimpleNamespace(content='{"product_name": "Tej"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=uzenet)])

    monkeypatch.setattr(szerver, "revenuecat_lekerdez", lekerdez)
    monkeypatch.setattr(szerver, "client", types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))))
    return allapot


def elemez(app, user, fejlec=None):
    return app.post("/analyze", headers=fejlec or cimrol(1), data={
        "app_user_id": user, "image": (io.BytesIO(JPEG), "kep.jpg")},
        content_type="multipart/form-data")


def keret(app, user, fejlec=None):
    return app.get(f"/ai_quota?app_user_id={user}", headers=fejlec or cimrol(1))


# ===========================================================================
# 1. A tegnapi bizonyítások már nem mennek át (S2, S3)
# ===========================================================================

def test_fioktorles_keresofeltetellel_nem_torol_semmit(app, szerver):
    ket_csalad(app)
    szerver.ai_naplo.insert_one({"app_user_id": "anna", "action": "analyze_image", "status": "success"})
    r = app.post("/delete_user_data", headers=FEJLEC, json={"user_id": {"$ne": None}})
    assert r.status_code == 400
    assert szerver.kollekcio.count_documents({}) == 2
    assert szerver.tagok_kollekcio.count_documents({}) == 4
    assert szerver.ai_naplo.count_documents({}) == 1


def test_kilepes_keresofeltetellel_nem_torol(app, szerver):
    ket_csalad(app)
    for _ in range(4):
        r = app.post("/leave_group", headers=FEJLEC,
                     json={"family_id": {"$ne": None}, "user_id": {"$ne": None}})
        assert r.status_code == 400
    r = app.post("/leave_group", headers=FEJLEC, json={"family_id": {"$ne": None}, "user_id": "anna"})
    assert r.status_code == 200
    assert szerver.tagok_kollekcio.count_documents({}) == 4
    assert szerver.kollekcio.count_documents({}) == 2


def test_feltoltes_user_id_nelkul_nem_ir(app, szerver):
    ket_csalad(app)
    r = app.post("/sync_list", headers=FEJLEC, json={
        "family_id": "AAA111", "list_data": {"storeName": "FELTÖRVE", "items": []}})
    assert r.status_code == 400
    assert szerver.kollekcio.find_one({"family_id": "AAA111"})["list_data"]["storeName"] == "AAA111"


def test_feltoltes_idegen_azonositoval_403(app, szerver):
    ket_csalad(app)
    assert feltolt(app, "AAA111", "idegen", base=None).status_code == 403


@pytest.mark.parametrize("vegpont,torzs", [
    ("/join_group", {"family_id": "AAA111", "user_id": {"$gt": ""}, "user_name": "x"}),
    ("/join_group", {"family_id": {"$gt": ""}, "user_id": "x", "user_name": "x"}),
    ("/moderate_member", {"family_id": "AAA111", "requester_id": {"$ne": None},
                          "target_user_id": "bela", "banned": True}),
    ("/moderate_member", {"family_id": "AAA111", "requester_id": "anna",
                          "target_user_id": {"$ne": "anna"}, "banned": True}),
    ("/moderate_member", {"family_id": "AAA111", "requester_id": "anna",
                          "target_user_id": "bela", "banned": "igen"}),
    ("/update_token", {"user_id": {"$ne": None}, "fcm_token": "x"}),
    ("/update_token", {"user_id": "anna", "fcm_token": {"$x": 1}}),
    ("/topup_credits", {"user_id": {"$ne": None}}),
    ("/sync_list", {"family_id": {"$ne": None}, "user_id": "anna", "list_data": {"items": []}}),
    ("/sync_list", {"family_id": "AAA111", "user_id": "anna", "list_data": {"items": []},
                    "base_timestamp": {"$gt": 0}}),
])
def test_keresofeltetel_helyett_400_vagy_404(app, szerver, vegpont, torzs):
    ket_csalad(app)
    elotte = (list(szerver.tagok_kollekcio.find({}, {"_id": 0})),
              list(szerver.kollekcio.find({}, {"_id": 0})))
    r = app.post(vegpont, headers=FEJLEC, json=torzs)
    assert r.status_code in (400, 404)
    assert (list(szerver.tagok_kollekcio.find({}, {"_id": 0})),
            list(szerver.kollekcio.find({}, {"_id": 0}))) == elotte


@pytest.mark.parametrize("ut", [
    "/get_list?family_id=AAA111&user_id=" + "x" * 200,
    "/list_members?family_id=AAA111&user_id=" + "x" * 200,
    "/ai_quota?app_user_id=" + "x" * 200,
])
def test_tul_hosszu_azonosito_400(app, ut):
    assert app.get(ut, headers=FEJLEC).status_code == 400


def test_nem_json_torzs_400(app):
    assert app.post("/sync_list", headers=FEJLEC, data="[1,2]",
                    content_type="application/json").status_code == 400
    assert app.post("/join_group", headers=FEJLEC, data="nem json").status_code == 400


def test_hibas_kod_nem_letezo_lista(app):
    """Rossz formájú kód: "nincs ilyen lista" (nem hiba), ahogy eddig."""
    for kod in ("abc123", "AB", "A" * 13, '{"$ne":null}', "ÁÉ1234"):
        r = app.get(f"/get_list?family_id={kod}", headers=FEJLEC)
        assert r.status_code == 200
        assert r.get_json()["exists"] is False
    assert csatlakozik(app, "abc123", "x").status_code == 404


# ===========================================================================
# 2. Az Alapító azonosítója csak a tagoknak (S3 a)
# ===========================================================================

def test_elonezet_nem_adja_ki_az_alapito_azonositojat(app):
    ket_csalad(app)
    torzs = app.get("/get_list?family_id=AAA111", headers=FEJLEC).get_json()
    assert torzs["exists"] is True
    assert torzs["owner_id"] is None
    assert torzs["owner_name"] == "Alapító"
    assert torzs["member_count"] == 2
    # A régi app a csatlakozáskor ebből építi a listát: ez marad.
    assert torzs["list_data"]["items"][0]["name"] == "Tej"


def test_a_tag_megkapja_az_alapito_azonositojat(app):
    ket_csalad(app)
    for user in ("anna", "bela"):
        torzs = app.get(f"/get_list?family_id=AAA111&user_id={user}", headers=FEJLEC).get_json()
        assert torzs["owner_id"] == "anna"


def test_idegen_azonositoval_403(app):
    ket_csalad(app)
    assert app.get("/get_list?family_id=AAA111&user_id=idegen", headers=FEJLEC).status_code == 403


def test_tag_nelkuli_listat_idegen_nem_torolhet(app, szerver):
    assert feltolt(app, "CCC333", "anna").status_code == 200
    szerver.tagok_kollekcio.delete_many({})
    r = app.post("/leave_group", headers=FEJLEC, json={"family_id": "CCC333", "user_id": "idegen"})
    assert r.status_code == 200
    assert szerver.kollekcio.find_one({"family_id": "CCC333"}) is not None
    assert szerver.torolt_listak.count_documents({}) == 0


# ===========================================================================
# 3. Méretkorlátok, webhook, képek (K1, K2)
# ===========================================================================

def test_tul_sok_tetel_413(app, szerver):
    r = feltolt(app, "DDD444", "anna", tetelek=[str(i) for i in range(5001)])
    assert r.status_code == 413
    assert szerver.kollekcio.count_documents({}) == 0
    assert feltolt(app, "DDD444", "anna", tetelek=[str(i) for i in range(5000)]).status_code == 200


def test_tul_nagy_lista_413(app):
    r = app.post("/sync_list", headers=FEJLEC, json={
        "family_id": "DDD444", "user_id": "anna", "base_timestamp": 0,
        "list_data": {"storeName": "x", "items": [{"name": "x" * 1100 * 1024}]}})
    assert r.status_code == 413


@pytest.mark.parametrize("list_data", [None, [], "x", {"items": "x"}, {"items": [1, 2]}])
def test_hibas_lista_400(app, list_data):
    r = app.post("/sync_list", headers=FEJLEC, json={
        "family_id": "DDD444", "user_id": "anna", "base_timestamp": 0, "list_data": list_data})
    assert r.status_code == 400


def test_5_mb_feletti_keres_413(app):
    r = app.post("/sync_list", headers=FEJLEC, data=b"{" + b" " * (5 * 1024 * 1024 + 10) + b"}",
                 content_type="application/json")
    assert r.status_code == 413
    r = app.post("/webhook", headers={"Authorization": WEBHOOK_KULCS},
                 data=b"x" * (6 * 1024 * 1024), content_type="application/json")
    assert r.status_code == 413


def test_webhook_fejlec_nelkul_401(app, szerver):
    esemeny = {"event": {"type": "CANCELLATION", "app_user_id": "$RCAnonymousID:abc"}}
    assert app.post("/webhook", json=esemeny).status_code == 401
    assert app.post("/webhook", json=esemeny, headers={"Authorization": "rossz"}).status_code == 401
    assert szerver.ai_naplo.count_documents({}) == 0
    r = app.post("/webhook", json=esemeny, headers={"Authorization": WEBHOOK_KULCS})
    assert r.status_code == 200
    assert szerver.ai_naplo.count_documents({"action": "subscription_cancellation"}) == 1


def test_webhook_zarva_ha_nincs_beallitva(app, szerver, monkeypatch):
    monkeypatch.setattr(szerver, "REVENUECAT_WEBHOOK_AUTH", None)
    r = app.post("/webhook", json={"event": {"type": "TEST"}}, headers={"Authorization": ""})
    assert r.status_code == 401


def test_webhook_hibas_esemeny_400(app):
    r = app.post("/webhook", json={"event": "x"}, headers={"Authorization": WEBHOOK_KULCS})
    assert r.status_code == 400


def feltolt_kepet(app, adat, tipus="image/jpeg", fejlec=FEJLEC):
    return app.post("/upload_image", headers=fejlec, data={
        "image": (io.BytesIO(adat), "kep.jpg", tipus)}, content_type="multipart/form-data")


def test_html_nem_toltheto_fel_kepkent(app, szerver):
    for adat, tipus in ((b"<html><script>alert(1)</script></html>", "text/html"),
                        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" + b" " * 20, "image/svg+xml"),
                        (b"<html>" + b" " * 20, "image/jpeg")):
        assert feltolt_kepet(app, adat, tipus).status_code == 400
    assert szerver.kepek_kollekcio.count_documents({}) == 0


@pytest.mark.parametrize("adat,tipus", [(JPEG, "image/jpeg"), (PNG, "image/png")])
def test_valodi_kep_feltoltheto_es_a_tipusa_a_tartalombol(app, adat, tipus):
    # Az app a fájltípust nem adja meg (application/octet-stream).
    r = feltolt_kepet(app, adat, "application/octet-stream")
    assert r.status_code == 200
    kep_id = r.get_json()["image_url"].split("/")[-1]
    v = app.get(f"/get_image/{kep_id}")
    assert v.status_code == 200
    assert v.mimetype == tipus
    assert v.headers["X-Content-Type-Options"] == "nosniff"
    assert v.data == adat


def test_regi_html_bejegyzes_letoltendo_fajl(app, szerver):
    szerver.kepek_kollekcio.insert_one({"image_id": "regi", "image_data": b"<html>x</html>",
                                        "content_type": "text/html"})
    v = app.get("/get_image/regi")
    assert v.mimetype == "application/octet-stream"
    assert v.headers["Content-Disposition"] == "attachment"
    assert v.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in v.headers["Content-Security-Policy"]


# ===========================================================================
# 4. Túl sok kérés: tiltás címenként, felhasználónként, sok címről
# ===========================================================================

def test_rossz_kodok_cimenkent_tiltva_a_sajat_lista_megy(app, ora):
    ket_csalad(app)
    for i in range(60):
        r = app.get(f"/get_list?family_id=F{i:05d}", headers=cimrol(9))
        assert r.status_code == 200 and r.get_json()["exists"] is False
    r = app.get("/get_list?family_id=FFFFF0", headers=cimrol(9))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 3000
    # Ugyanarról a címről: előnézet és új csatlakozás áll...
    assert app.get("/get_list?family_id=AAA111", headers=cimrol(9)).status_code == 429
    assert csatlakozik(app, "AAA111", "uj", fejlec=cimrol(9)).status_code == 429
    # ...a saját lista szinkronja nem.
    assert app.get("/get_list?family_id=AAA111&user_id=anna", headers=cimrol(9)).status_code == 200
    assert feltolt(app, "AAA111", "anna", base=None, fejlec=cimrol(9)).status_code == 200
    # Más címről minden megy.
    assert app.get("/get_list?family_id=AAA111", headers=cimrol(10)).status_code == 200
    # Egy óra múlva újra.
    ora[0] += 3601
    assert app.get("/get_list?family_id=AAA111", headers=cimrol(9)).status_code == 200


def test_torolt_kod_nem_szamit_rossz_kodnak(app, szerver):
    for i in range(80):
        szerver.torolt_listak.insert_one({"family_id": f"T{i:05d}"})
        r = app.get(f"/get_list?family_id=T{i:05d}&user_id=anna", headers=cimrol(9))
        assert r.get_json()["deleted"] is True


def test_cimrol_kulonbozo_nevekkel_is_tiltva(app, ai, ora):
    for i in range(60):
        assert keret(app, f"robot{i}", cimrol(7)).status_code == 200
    assert keret(app, "robot60", cimrol(7)).status_code == 429
    assert elemez(app, "robot61", cimrol(7)).status_code == 429
    assert keret(app, "valaki", cimrol(8)).status_code == 200
    # A tiltás csak az AI-ra szól: a lista megy.
    assert feltolt(app, "EEE555", "robot0", fejlec=cimrol(7)).status_code == 200


def test_x_forwarded_for_hamisitas_nem_segit(app, ai):
    for i in range(61):
        r = keret(app, f"robot{i}", cimrol(7, **{"X-Forwarded-For": f"10.0.0.{i}"}))
    assert r.status_code == 429


def test_cloudflare_fejlec_nelkul_nincs_cimkorlat(app):
    # Ha a fejléc nem érkezik meg, nem tiltunk címre (mindenki egy címnek látszana).
    for i in range(70):
        assert app.get(f"/get_list?family_id=G{i:05d}", headers=FEJLEC).status_code == 200


def test_mindenre_vonatkozo_cimkorlat(app, ora):
    for i in range(600):
        assert app.get("/latest_version", headers=cimrol(5)).status_code == 200
    assert app.get("/latest_version", headers=cimrol(5)).status_code == 429
    assert app.get("/get_list?family_id=AAA111&user_id=anna", headers=cimrol(5)).status_code == 429
    ora[0] += 3601
    assert app.get("/latest_version", headers=cimrol(5)).status_code == 200


def test_kulcs_nelkuli_robot_is_szamit(app):
    for i in range(600):
        assert app.get("/get_list?family_id=AAA111", headers={"CF-Connecting-IP": "198.51.100.1"}).status_code == 401
    assert app.get("/get_list?family_id=AAA111", headers={"CF-Connecting-IP": "198.51.100.1"}).status_code == 429


def test_ugyanaz_a_felhasznalo_sok_cimrol(app, ai, ora):
    for n in range(1, 9):
        assert keret(app, "kozos", cimrol(n)).status_code == 200
        ora[0] += 30
    r = keret(app, "kozos", cimrol(9))
    assert r.status_code == 429
    # A felhasználó mindenhonnan tiltva, más nem.
    assert keret(app, "kozos", cimrol(1)).status_code == 429
    assert keret(app, "masik", cimrol(1)).status_code == 200


def test_wifi_es_mobilnet_valtogatasa_rendben(app, ai, ora):
    for i in range(40):
        assert keret(app, "anna", cimrol(1 + i % 3)).status_code == 200
        ora[0] += 20


def test_ipv6_elotag_egy_cimnek_szamit(szerver):
    k = szerver.cim_kulcs
    assert k("2001:db8:1:2::1") == k("2001:db8:1:2:ffff:ffff:ffff:5")
    assert k("2001:db8:1:2::1") != k("2001:db8:1:3::1")
    assert k("::ffff:203.0.113.5") == k("203.0.113.5")
    assert k("nem cím") is None and k("") is None and k(None) is None


def test_tul_sok_uj_lista_cimenkent(app):
    for i in range(20):
        assert feltolt(app, f"N{i:05d}", f"u{i}", fejlec=cimrol(3)).status_code == 200
    assert feltolt(app, "NNNNN1", "u99", fejlec=cimrol(3)).status_code == 429
    # A meglévő listák szinkronja megy.
    assert feltolt(app, "N00000", "u0", base=None, fejlec=cimrol(3)).status_code == 200


def test_tul_sok_kepfeltoltes_cimenkent(app, ora):
    for i in range(30):
        assert feltolt_kepet(app, JPEG, fejlec=cimrol(4)).status_code == 200
        ora[0] += 10
    assert feltolt_kepet(app, JPEG, fejlec=cimrol(4)).status_code == 429


def test_tiltas_naplozva_cim_nelkul(app, szerver):
    for i in range(61):
        app.get(f"/get_list?family_id=H{i:05d}", headers=cimrol(9))
    naplo = list(szerver.vedelem_naplo.find({}, {"_id": 0}))
    assert len(naplo) == 1
    assert naplo[0]["szabaly"] == "ip_kodkereses" and naplo[0]["terulet"] == "kod"
    assert naplo[0]["tiltas_mp"] == 3600
    assert "203.0.113" not in json.dumps(naplo, default=str)


# ===========================================================================
# 5. Rendes használat: nem esik bele
# ===========================================================================

def test_20_foto_egymas_utan_ketszer_egy_oran_belul(app, ai, ora):
    """Két 20-as sorozat egy órán belül (a régi 5/perc szabály szerint
    12 mp-enként); minden kép után a keret is frissül."""
    for sorozat in range(2):
        for i in range(20):
            assert elemez(app, "anna").status_code == 200, (sorozat, i)
            assert keret(app, "anna").status_code == 200
            ora[0] += 13
        ora[0] += 300
    assert ai["openai"] == 40
    # Ugyanabban az órában a 41. már gyanús: egy órára áll.
    r = elemez(app, "anna")
    assert r.status_code == 429
    assert ai["openai"] == 40


def test_egy_csalad_egy_cimrol_sok_listaval(app, ora):
    """4 családtag, 10 közös lista, 15 percen át percenként mindegyiket
    lekéri és 3-at módosít: egyik szabály sem lép életbe."""
    tagok = ["anna", "bela", "cili", "dani"]
    for i in range(10):
        assert feltolt(app, f"K{i:05d}", "anna").status_code == 200
        for t in tagok[1:]:
            assert csatlakozik(app, f"K{i:05d}", t).status_code == 200
    for perc in range(15):
        for t in tagok:
            for i in range(10):
                assert app.get(f"/get_list?family_id=K{i:05d}&user_id={t}",
                               headers=cimrol(1)).status_code == 200
            for i in range(3):
                assert feltolt(app, f"K{i:05d}", t, base=None, fejlec=cimrol(1)).status_code == 200
        ora[0] += 60


def test_a_regi_5_per_perc_marad_de_nem_tilt(app, ai, ora):
    for i in range(5):
        assert elemez(app, "anna").status_code == 200
        ora[0] += 1
    r = elemez(app, "anna")
    assert r.status_code == 429
    assert "1 percet" in r.get_json()["error"]
    ora[0] += 61
    assert elemez(app, "anna").status_code == 200


# ===========================================================================
# 6. A meglévő AI-védelmek nem játszhatók ki
# ===========================================================================

def test_egyszerre_bekuldott_kepek_kozul_csak_egy_fut(app, szerver, ai):
    ai["varj"] = threading.Event()
    eredmeny = {}

    def elso():
        eredmeny["elso"] = elemez(szerver.app.test_client(), "anna").status_code

    szal = threading.Thread(target=elso)
    szal.start()
    for _ in range(100):
        if ai["openai"]:
            break
        threading.Event().wait(0.02)
    assert ai["openai"] == 1
    # Amíg az első fut, a többi azonnal 429 (nem jut el az OpenAI-ig).
    for _ in range(5):
        assert elemez(szerver.app.test_client(), "anna").status_code == 429
    ai["varj"].set()
    szal.join(5)
    assert eredmeny["elso"] == 200
    assert ai["openai"] == 1
    # Más felhasználót nem fog vissza.
    ai["varj"] = None
    assert elemez(app, "bela").status_code == 200


def test_havi_keret_uj_azonositoval_sem_kezdodik_elolrol(app, szerver, ai, ora):
    """Újratelepítés + vásárlás visszaállítása: új azonosító, ugyanaz az előfizetés."""
    ai["tx"]["regi"] = "GPA.1111-2222-3333-44444..2"
    ai["tx"]["uj"] = "GPA.1111-2222-3333-44444"
    for i in range(60):
        szerver.ai_naplo.insert_one({
            "app_user_id": "regi", "action": "analyze_image", "status": "success",
            "keret_kulcsok": ["id:" + szerver.ujjlenyomat("regi"),
                              "tx:" + szerver.ujjlenyomat("GPA.1111-2222-3333-44444")],
            "timestamp": szerver.datetime.now(timezone.utc)})
    assert keret(app, "uj").get_json()["remaining"] == 0
    assert elemez(app, "uj").get_json()["error"] == "quota_exhausted"
    assert ai["openai"] == 0
    # Másik előfizetés: a saját 60-a megvan.
    ai["tx"]["harmadik"] = "GPA.9999-0000-0000-00000"
    assert keret(app, "harmadik").get_json()["remaining"] == 60


def test_adattorles_nem_nullazza_a_havi_keretet(app, szerver, ai):
    for i in range(60):   # a régi, kulcs nélküli bejegyzések is
        szerver.ai_naplo.insert_one({"app_user_id": "anna", "action": "analyze_image",
                                     "status": "success",
                                     "timestamp": szerver.datetime.now(timezone.utc)})
    assert app.post("/delete_user_data", headers=FEJLEC, json={"user_id": "anna"}).status_code == 200
    assert szerver.ai_naplo.count_documents({"app_user_id": "anna"}) == 0
    assert keret(app, "anna").get_json()["remaining"] == 0
    assert elemez(app, "anna").get_json()["error"] == "quota_exhausted"


def test_adattorles_a_regi_bejegyzeseket_torli(app, szerver, ai):
    regi = datetime(2026, 1, 1, tzinfo=timezone.utc)
    szerver.ai_naplo.insert_one({"app_user_id": "anna", "action": "analyze_image",
                                 "status": "success", "timestamp": regi})
    szerver.ai_naplo.insert_one({"app_user_id": "anna", "action": "topup_purchase",
                                 "status": "success", "timestamp": szerver.datetime.now(timezone.utc)})
    app.post("/delete_user_data", headers=FEJLEC, json={"user_id": "anna"})
    assert szerver.ai_naplo.count_documents({}) == 0


def test_nem_pro_nem_elemezhet(app, ai):
    ai["pro"] = False
    assert elemez(app, "anna").status_code == 403
    assert ai["openai"] == 0


def test_revenuecat_valasz_feldolgozasa(szerver, monkeypatch):
    kert = {}
    valasz = {"subscriber": {
        "entitlements": {"pro_mode": {"expires_date": "2099-01-01T00:00:00Z",
                                      "product_identifier": "pro_havi"}},
        "subscriptions": {"pro_havi": {"store_transaction_id": "GPA.1234-5678-9012-34567..5"}},
        "non_subscriptions": {"ai_topup_20": [
            {"purchase_date": "2099-01-01T00:00:00Z", "store_transaction_id": "GPA.5555"}]},
    }}

    class Valasz:
        status = 200
        def read(self): return json.dumps(valasz).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=None):
        kert["url"] = req.full_url
        return Valasz()

    monkeypatch.setattr(szerver.urllib.request, "urlopen", urlopen)
    is_pro, extra, kulcsok = szerver.revenuecat_lekerdez("$RCAnonymousID:ab/../c?x=1")
    assert kert["url"] == ("https://api.revenuecat.com/v1/subscribers/"
                           "%24RCAnonymousID%3Aab%2F..%2Fc%3Fx%3D1")
    assert is_pro is True and extra == 20
    assert "tx:" + szerver.ujjlenyomat("GPA.1234-5678-9012-34567") in kulcsok
    assert "tx:" + szerver.ujjlenyomat("GPA.5555") in kulcsok


def test_elemzesi_hiba_altalanos_szoveg(app, szerver, ai, monkeypatch):
    def hibas(**_):
        raise RuntimeError("belső részlet: sk-titok")
    monkeypatch.setattr(szerver.client.chat.completions, "create", hibas)
    r = elemez(app, "anna")
    assert r.status_code == 500
    assert "sk-titok" not in r.get_data(as_text=True)


# ===========================================================================
# 7. Kuponok (A6)
# ===========================================================================

def test_kuponkod_formaja(szerver):
    import coupons
    for _ in range(50):
        assert coupons.KUPON_MINTA.fullmatch(coupons.generate_coupon_code())


def test_kupongeneralas_jelszoval_es_korlattal(app, monkeypatch):
    import coupons
    monkeypatch.setattr(coupons, "ADMIN_SECRET", "hosszu-teszt-jelszo")
    assert app.get("/admin_generate_coupons?secret=rossz").status_code == 401
    assert app.get("/admin_generate_coupons").status_code == 401
    r = app.get("/admin_generate_coupons?secret=hosszu-teszt-jelszo&count=900")
    assert r.status_code == 200
    assert len(r.get_data(as_text=True).splitlines()) == 500


def test_kuponbevaltas(app, szerver, monkeypatch):
    import coupons
    kapott = []
    monkeypatch.setattr(coupons, "_grant_promotional_entitlement",
                        lambda uid, *_a, **_k: (kapott.append(uid) or (True, None)))
    szerver.kuponok_kollekcio.insert_one({"code": "ABCD-1234", "redeemed": False})
    # Rossz formájú kód akkor sem váltható be, ha valahogy bekerült.
    szerver.kuponok_kollekcio.insert_one({"code": "ROSSZ", "redeemed": False})
    assert app.post("/redeem_coupon", headers=FEJLEC,
                    json={"code": {"$ne": ""}, "app_user_id": "a"}).status_code == 400
    assert app.post("/redeem_coupon", headers=FEJLEC,
                    json={"code": "ABCD-1234", "app_user_id": {"$ne": ""}}).status_code == 400
    assert app.post("/redeem_coupon", headers=FEJLEC,
                    json={"code": "ROSSZ", "app_user_id": "a"}).status_code == 404
    assert app.post("/redeem_coupon", headers=FEJLEC,
                    json={"code": "abcd-1234", "app_user_id": "a"}).status_code == 200
    assert kapott == ["a"]


def test_kuponprobalgatas_tiltva(app):
    for i in range(10):
        assert app.post("/redeem_coupon", headers=cimrol(6),
                        json={"code": f"AAAA-{i:04d}", "app_user_id": "a"}).status_code == 404
    assert app.post("/redeem_coupon", headers=cimrol(6),
                    json={"code": "AAAA-9999", "app_user_id": "a"}).status_code == 429


def test_kupon_url_kodolva(szerver):
    import coupons
    assert coupons._alap_subscriber_url("$RCAnonymousID:a/b") == \
        "https://api.revenuecat.com/v1/subscribers/%24RCAnonymousID%3Aa%2Fb"


# ===========================================================================
# 8. A régi app (1.0.80) útjai változatlanok
# ===========================================================================

def test_regi_app_teljes_kozos_lista_menete(app, szerver, ai, ora):
    f = cimrol(1)
    # Alapító: első feltöltés (base_timestamp nélkül), majd rendes feltöltés.
    r = app.post("/sync_list", headers=f, json={
        "family_id": "ABC123", "user_id": "111", "list_data": {"storeName": "Bolt", "items": [
            {"id": "1", "name": "Tej", "unit": "1 l", "checked": False}]}})
    assert r.status_code == 200
    ts = r.get_json()["timestamp"]
    # Csatlakozó: előnézet, csatlakozás, tagként lekérés.
    elo = app.get("/get_list?family_id=ABC123", headers=f).get_json()
    assert elo["exists"] and elo["owner_name"] == "Alapító" and elo["member_count"] == 1
    assert app.post("/join_group", headers=f, json={
        "family_id": "ABC123", "user_name": "Béla", "user_id": "222",
        "timestamp": 1_800_000_000_000}).status_code == 200
    tag = app.get("/get_list?family_id=ABC123&user_id=222", headers=f).get_json()
    assert tag["owner_id"] == "111" and tag["member_count"] == 2
    # A tag módosít; ütközés régi alapra.
    ora[0] += 5
    r = app.post("/sync_list", headers=f, json={
        "family_id": "ABC123", "user_id": "222", "base_timestamp": ts,
        "list_data": {"storeName": "Bolt", "items": [{"id": "1", "name": "Tej", "checked": True}]}})
    assert r.status_code == 200
    assert app.post("/sync_list", headers=f, json={
        "family_id": "ABC123", "user_id": "111", "base_timestamp": ts,
        "list_data": {"storeName": "Bolt", "items": []}}).status_code == 409
    # Alapító: tagok, kitiltás, visszaengedés.
    tagok = app.get("/list_members?family_id=ABC123&user_id=111", headers=f).get_json()["members"]
    assert {t["user_id"] for t in tagok} == {"111", "222"}
    for tiltas in (True, False):
        assert app.post("/moderate_member", headers=f, json={
            "family_id": "ABC123", "requester_id": "111", "target_user_id": "222",
            "banned": tiltas}).status_code == 200
    # AI, +20 vásárlás, keret.
    assert app.post("/analyze", headers=f, data={
        "app_user_id": "$RCAnonymousID:0123456789abcdef0123456789abcdef",
        "image": (io.BytesIO(JPEG), "image.jpg")}, content_type="multipart/form-data").status_code == 200
    assert app.post("/topup_credits", headers=f, json={
        "user_id": "$RCAnonymousID:0123456789abcdef0123456789abcdef", "credits": 20,
        "transaction_id": "GPA.1-2"}).status_code == 200
    assert keret(app, "$RCAnonymousID:0123456789abcdef0123456789abcdef", f).get_json()["remaining"] == 59
    # Kép csatolása.
    assert feltolt_kepet(app, JPEG, "application/octet-stream", fejlec=f).status_code == 200
    # Tag kilép, majd az Alapító feloszlatja.
    assert app.post("/leave_group", headers=f, json={"family_id": "ABC123", "user_id": "222"}).get_json()["status"] == "left"
    assert app.post("/leave_group", headers=f, json={"family_id": "ABC123", "user_id": "111"}).get_json()["status"] == "owner_dissolved"
    assert app.get("/get_list?family_id=ABC123&user_id=111", headers=f).get_json()["deleted"] is True


def test_latest_version_mutatja_a_valtozatot(app, szerver):
    torzs = app.get("/latest_version", headers={"CF-Connecting-IP": "203.0.113.1"}).get_json()
    assert torzs["latest_version"] == szerver.LATEST_VERSION
    assert torzs["szerver"] == szerver.SZERVER_VALTOZAT
    assert torzs["cimfigyeles"] is True


def test_socket_szoba_csak_ervenyes_koddal(szerver):
    kliens = szerver.socketio.test_client(szerver.app)
    kliens.emit("join_room", {"family_id": {"$ne": None}})
    kliens.emit("join_room", "nem szótár")
    kliens.emit("join_room", {"family_id": "ABC123"})
    assert kliens.is_connected()
    kliens.disconnect()


# ===========================================================================
# 9. A Vedelem osztály magában
# ===========================================================================

def test_vedelem_lejarat_es_terulet():
    import vedelem
    t = [0.0]
    naplo = []
    v = vedelem.Vedelem(naplo=naplo.append, ido=lambda: t[0],
                        szabalyok={"a": ("ai", 60, 3, 600), "m": ("minden", 60, 5, 100)})
    assert [v.esemeny("a", "k") for _ in range(3)] == [0, 0, 0]
    assert v.esemeny("a", "k") == 600
    assert v.tiltva("k", "ai") > 0 and v.tiltva("k", "kod") == 0
    t[0] += 601
    assert v.tiltva("k", "ai") == 0
    assert v.esemeny("a", "k") == 0
    # A "minden" tiltás minden területre szól.
    for _ in range(6):
        v.esemeny("m", "x")
    assert v.tiltva("x", "ai") > 0 and v.tiltva("x", "kod") > 0
    assert [n["szabaly"] for n in naplo] == ["a", "m"]


def test_vedelem_egyszerre_futo():
    import vedelem
    v = vedelem.Vedelem()
    assert v.elkezd("k") and not v.elkezd("k")
    v.befejez("k")
    assert v.elkezd("k")


def test_vedelem_memoria_nem_no(monkeypatch):
    import vedelem
    t = [0.0]
    v = vedelem.Vedelem(ido=lambda: t[0])
    for i in range(20000):
        v.esemeny("f_osszes", f"f:{i}")
        t[0] += 0.01
    t[0] += 120
    for i in range(5000):
        v.esemeny("f_osszes", "egy")
    assert len(v._esemenyek) < 6000
