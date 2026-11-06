# A Google-igazolás (X-ID-TOKEN) ellenőrzésének és számlálásának tesztjei
# (igazolas.py és a main.py kapuőre).
#
# Mit ellenőriz:
#   - csak a Google kulcsával aláírt, nekünk szóló, le nem járt igazolás
#     érvényes; a hamisított, idegen, sérült, lejárt külön esetként számít;
#   - a Google kulcsait ritkán töltjük le, kitalált kulcsnévvel nem lehet
#     letöltésre kényszeríteni, letöltési hibánál a régiek maradnak;
#   - a számok naponta, végpontonként kerülnek az adatbázisba, azonosító
#     helyett csak ujjlenyomattal, és hiba esetén sem vesznek el;
#   - a szerver semmit nem utasít el az igazolás miatt: a régi app (igazolás
#     nélkül) és a hibás igazolás ugyanúgy kiszolgálva.
#
# A Google helyett saját, a teszt közben készített kulcs ír alá; net nem kell.

import base64
import hashlib
import hmac
import importlib
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest import mock

import mongomock
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt as google_jwt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import igazolas  # noqa: E402

MOST = 1_800_000_000
KULCS = "teszt-kulcs"
FEJLEC = {"X-API-KEY": KULCS}


def _kulcspar():
    kulcs = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privat = kulcs.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
    # A Google is X.509-tanúsítványként adja közre a kulcsait.
    nev = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "teszt")])
    tan = (x509.CertificateBuilder().subject_name(nev).issuer_name(nev)
           .public_key(kulcs.public_key()).serial_number(1)
           .not_valid_before(datetime(2020, 1, 1)).not_valid_after(datetime(2040, 1, 1))
           .sign(kulcs, hashes.SHA256()))
    return privat, tan.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
def kulcsok():
    """A "google" és egy idegen kulcspár: (privát, tanúsítvány)."""
    return {"google": _kulcspar(), "idegen": _kulcspar()}


def alair(kulcsok, kid="k1", ki="google", **mezok):
    adat = {"iss": "https://accounts.google.com", "aud": igazolas.WEBES_KLIENS,
            "azp": "android-kliens", "sub": "111", "iat": MOST - 10, "exp": MOST + 3600}
    adat.update(mezok)
    for k in [k for k, v in adat.items() if v is None]:
        del adat[k]
    signer = crypt.RSASigner.from_string(kulcsok[ki][0], key_id=kid)
    return google_jwt.encode(signer, adat).decode()


def b64(adat):
    return base64.urlsafe_b64encode(json.dumps(adat).encode()).rstrip(b"=").decode()


class Letolto:
    """A Google kulcslistája helyett: számolja a letöltéseket, és kérésre hibázik."""

    def __init__(self, kulcsok, mp=3600):
        self.kulcsok = {"k1": kulcsok["google"][1]}
        self.mp = mp
        self.hivas = 0
        self.hiba = None

    def __call__(self):
        self.hivas += 1
        if self.hiba:
            raise self.hiba
        return dict(self.kulcsok), self.mp


@pytest.fixture()
def ora():
    return [float(MOST)]


@pytest.fixture()
def letolto(kulcsok):
    return Letolto(kulcsok)


@pytest.fixture()
def ell(letolto, ora):
    tar = igazolas.KulcsTar(letolto=letolto, ido=lambda: ora[0])
    return igazolas.Ellenorzo(kulcstar=tar, ido=lambda: ora[0])


# ---------------------------------------------------------------------------
# Ellenőrzés
# ---------------------------------------------------------------------------

def test_ervenyes_igazolas(ell, kulcsok):
    assert ell.ellenoriz(alair(kulcsok)) == (igazolas.ERVENYES, "111")


@pytest.mark.parametrize("ertek", [None, ""])
def test_nincs_igazolas(ell, ertek, letolto):
    assert ell.ellenoriz(ertek) == (igazolas.NINCS, None)
    assert letolto.hivas == 0


@pytest.mark.parametrize("kibocsato", ["accounts.google.com", "https://accounts.google.com"])
def test_mindket_kibocsato_jo(ell, kulcsok, kibocsato):
    assert ell.ellenoriz(alair(kulcsok, iss=kibocsato)).allapot == igazolas.ERVENYES


def test_lejart_igazolas_kulon_eset(ell, kulcsok):
    # 60 mp eltérést tűrünk, utána lejárt (de a Google aláírta, a sub ismert).
    assert ell.ellenoriz(alair(kulcsok, exp=MOST - 59)).allapot == igazolas.ERVENYES
    assert ell.ellenoriz(alair(kulcsok, exp=MOST - 61)) == (igazolas.LEJART, "111")


def test_jovobeli_kiadas_hibas(ell, kulcsok):
    assert ell.ellenoriz(alair(kulcsok, iat=MOST + 59)).allapot == igazolas.ERVENYES
    assert ell.ellenoriz(alair(kulcsok, iat=MOST + 61)).allapot == igazolas.HIBAS


@pytest.mark.parametrize("mezok", [
    {"aud": "mas-app.apps.googleusercontent.com"},   # másnak szól
    {"aud": [igazolas.WEBES_KLIENS, "mas"]},
    {"aud": None},
    {"iss": "https://hamis.example"},
    {"iss": None},
    {"sub": None},
    {"sub": ""},
    {"sub": 111},
    {"sub": "x" * 256},
    {"exp": "holnap"},
    {"exp": True},
    {"iat": None},
    {"exp": None},
])
def test_hibas_tartalom(ell, kulcsok, mezok):
    assert ell.ellenoriz(alair(kulcsok, **mezok)) == (igazolas.HIBAS, None)


def test_idegen_kulccsal_alairt_hibas(ell, kulcsok):
    # Ugyanazzal a kulcsnévvel, de nem a Google kulcsával aláírva.
    assert ell.ellenoriz(alair(kulcsok, ki="idegen")) == (igazolas.HIBAS, None)


def test_atirt_tartalom_hibas(ell, kulcsok):
    fej, _, alairas = alair(kulcsok).split(".")
    masik = b64({"iss": "https://accounts.google.com", "aud": igazolas.WEBES_KLIENS,
                 "sub": "222", "iat": MOST - 10, "exp": MOST + 3600})
    assert ell.ellenoriz(f"{fej}.{masik}.{alairas}") == (igazolas.HIBAS, None)


def test_mas_algoritmus_hibas(ell, kulcsok):
    torzs = alair(kulcsok).split(".")[1]
    # Régi trükk: a nyilvános tanúsítvány mint HMAC-titok.
    fej = b64({"alg": "HS256", "kid": "k1", "typ": "JWT"})
    alairas = base64.urlsafe_b64encode(hmac.new(
        kulcsok["google"][1].encode(), f"{fej}.{torzs}".encode(), hashlib.sha256
    ).digest()).rstrip(b"=").decode()
    assert ell.ellenoriz(f"{fej}.{torzs}.{alairas}").allapot == igazolas.HIBAS
    for alg in ("none", "ES256", None):
        fej = b64({"alg": alg, "kid": "k1"})
        assert ell.ellenoriz(f"{fej}.{torzs}.").allapot == igazolas.HIBAS


def test_kulcsnev_nelkul_hibas(ell, kulcsok):
    fej, torzs, alairas = alair(kulcsok).split(".")
    for uj in ({"alg": "RS256"}, {"alg": "RS256", "kid": ""}, {"alg": "RS256", "kid": 5}):
        assert ell.ellenoriz(f"{b64(uj)}.{torzs}.{alairas}").allapot == igazolas.HIBAS


@pytest.mark.parametrize("szemet", [
    "a.b.c", "abc", "a.b", "a.b.c.d", "." * 2, "x" * 5000 + ".a.b",
    "é.é.é", "eyJhbGciOiJSUzI1NiJ9.e30", 12345, b"a.b.c", ["a.b.c"], {"a": 1},
])
def test_szemet_nem_dob(ell, szemet):
    assert ell.ellenoriz(szemet) == (igazolas.HIBAS, None)


def test_tul_hosszu_meg_sem_nezi(ell, kulcsok, letolto):
    # Rendesen aláírt, csak túl hosszú: a kulcsokat sem kérjük le hozzá.
    hosszu = alair(kulcsok, nonce="x" * igazolas.MAX_HOSSZ)
    assert ell.ellenoriz(hosszu) == (igazolas.HIBAS, None)
    assert letolto.hivas == 0
    assert ell.ellenoriz(alair(kulcsok, nonce="x" * 2000)).allapot == igazolas.ERVENYES


# ---------------------------------------------------------------------------
# A Google kulcsai
# ---------------------------------------------------------------------------

def test_kulcsok_tarolva_maradnak(ell, kulcsok, letolto, ora):
    for _ in range(5):
        assert ell.ellenoriz(alair(kulcsok)).allapot == igazolas.ERVENYES
    assert letolto.hivas == 1
    ora[0] += 3599
    ell.ellenoriz(alair(kulcsok))
    assert letolto.hivas == 1
    ora[0] += 2   # a tárolási idő lejárt: újra letölti
    ell.ellenoriz(alair(kulcsok))
    assert letolto.hivas == 2


def test_kitalalt_kulcsnevvel_nem_toltet_le_ujra(ell, kulcsok, letolto, ora):
    assert ell.ellenoriz(alair(kulcsok)).allapot == igazolas.ERVENYES
    for i in range(20):
        assert ell.ellenoriz(alair(kulcsok, kid=f"kitalalt{i}")).allapot == igazolas.HIBAS
    assert letolto.hivas == 1   # 60 mp-en belül nincs új letöltés
    ora[0] += 61
    assert ell.ellenoriz(alair(kulcsok, kid="kitalalt")).allapot == igazolas.HIBAS
    assert letolto.hivas == 2


def test_uj_kulcsot_a_google_listajabol_atveszi(ell, kulcsok, letolto, ora):
    assert ell.ellenoriz(alair(kulcsok)).allapot == igazolas.ERVENYES
    # A Google új kulcsra vált; az első vele aláírt igazolásra újra letöltünk.
    ora[0] += 61
    letolto.kulcsok["k2"] = kulcsok["idegen"][1]
    assert ell.ellenoriz(alair(kulcsok, kid="k2", ki="idegen")).allapot == igazolas.ERVENYES
    assert letolto.hivas == 2


def test_letoltesi_hiba_kulcs_nelkul_nem_ellenorizheto(ell, kulcsok, letolto, ora, capsys):
    letolto.hiba = OSError("nincs net")
    assert ell.ellenoriz(alair(kulcsok)) == (igazolas.NEM_ELLENORIZHETO, None)
    assert ell.ellenoriz(alair(kulcsok)).allapot == igazolas.NEM_ELLENORIZHETO
    assert letolto.hivas == 1   # hiba után sem próbálkozik minden kérésnél
    assert "nem tölthetők le (OSError)" in capsys.readouterr().out
    letolto.hiba = None
    ora[0] += 61
    assert ell.ellenoriz(alair(kulcsok)).allapot == igazolas.ERVENYES


def test_letoltesi_hiba_utan_a_regi_kulcsok_maradnak(ell, kulcsok, letolto, ora):
    assert ell.ellenoriz(alair(kulcsok)).allapot == igazolas.ERVENYES
    ora[0] += 3601
    letolto.hiba = RuntimeError("503")
    tok = alair(kulcsok, iat=ora[0] - 10, exp=ora[0] + 3600)
    assert ell.ellenoriz(tok).allapot == igazolas.ERVENYES
    assert letolto.hivas == 2


@pytest.mark.parametrize("fejlec, mp", [
    ("public, max-age=21000, must-revalidate, no-transform", 21000),
    (None, 3600), ("", 3600), ("no-cache", 3600),
    ("max-age=10", igazolas.TAROLAS_MIN), ("max-age=99999999", igazolas.TAROLAS_MAX),
])
def test_tarolasi_ido(fejlec, mp):
    assert igazolas.tarolasi_ido(fejlec) == mp


def test_google_kulcsai_valaszt_ellenoriz(monkeypatch):
    class Valasz:
        def __init__(self, adat, fejlec="max-age=20000"):
            self.adat, self.headers = adat, {"Cache-Control": fejlec}

        def raise_for_status(self):
            pass

        def json(self):
            return self.adat

    hivasok = []

    def get(cim, timeout):
        hivasok.append((cim, timeout))
        return Valasz(valasz[0])

    monkeypatch.setattr(igazolas.requests, "get", get)
    valasz = [{"k1": "PEM"}]
    assert igazolas.google_kulcsai() == ({"k1": "PEM"}, 20000)
    assert hivasok == [(igazolas.KULCSOK_CIME, igazolas.LETOLTES_IDOKORLAT)]
    for rossz in ({}, [], "x", {"k1": 5}, {5: "PEM"}):
        valasz[0] = rossz
        with pytest.raises(ValueError):
            igazolas.google_kulcsai()

    # Hibakódos válasz (pl. 503): a törzsét meg sem nézzük.
    def hibakod(self):
        raise igazolas.requests.HTTPError("503")

    valasz[0] = {"k1": "PEM"}
    monkeypatch.setattr(Valasz, "raise_for_status", hibakod)
    with pytest.raises(igazolas.requests.HTTPError):
        igazolas.google_kulcsai()


# ---------------------------------------------------------------------------
# Összevetés a kérés azonosítójával
# ---------------------------------------------------------------------------

E = igazolas.Eredmeny


@pytest.mark.parametrize("eredmeny, azonosito, osszevetheto, vart", [
    (E("ervenyes", "111"), "111", True, "ervenyes_egyezik"),
    (E("ervenyes", "111"), "222", True, "ervenyes_elter"),
    (E("ervenyes", "111"), None, True, "ervenyes_azonosito_nelkul"),
    (E("ervenyes", "111"), "222", False, "ervenyes"),
    (E("nincs", None), "111", True, "nincs"),
    (E("lejart", "111"), "111", True, "lejart"),
    (E("hibas", None), "111", True, "hibas"),
    (E("nem_ellenorizheto", None), "111", True, "nem_ellenorizheto"),
])
def test_eset(eredmeny, azonosito, osszevetheto, vart):
    assert igazolas.eset(eredmeny, azonosito, osszevetheto) == vart


# ---------------------------------------------------------------------------
# Számlálás
# ---------------------------------------------------------------------------

@pytest.fixture()
def gyujtemeny():
    return mongomock.MongoClient().db.igazolas_szamlalo


@pytest.fixture()
def szamlalo(gyujtemeny, ora):
    return igazolas.Szamlalo(gyujtemeny, ido=lambda: ora[0])


NAP = datetime.fromtimestamp(MOST, timezone.utc).strftime("%Y-%m-%d")


def test_szamok_az_adatbazisba(szamlalo, gyujtemeny, capsys):
    szamlalo.jegyez("sync_list", "ervenyes_egyezik", "anna", "igazolt")
    szamlalo.jegyez("sync_list", "ervenyes_egyezik", "anna", "igazolt")
    szamlalo.jegyez("sync_list", "nincs", "bela", "igazolas_nelkul")
    szamlalo.jegyez("analyze", "ervenyes")
    assert gyujtemeny.count_documents({}) == 0   # még csak a memóriában
    szamlalo.kiir()
    doc = gyujtemeny.find_one({"_id": NAP})
    assert doc["vegpontok"] == {"sync_list": {"ervenyes_egyezik": 2, "nincs": 1},
                                "analyze": {"ervenyes": 1}}
    assert doc["felhasznalok"] == {"igazolt": [igazolas.ujjlenyomat("anna")],
                                   "igazolas_nelkul": [igazolas.ujjlenyomat("bela")]}
    assert isinstance(doc["letrejott"], datetime)
    assert "anna" not in repr(doc) and "bela" not in repr(doc)
    assert "Igazolás az előző kiírás óta: ervenyes 1, ervenyes_egyezik 2, nincs 1" \
        in capsys.readouterr().out

    # A következő kiírás hozzáad, a felhasználók nem duplázódnak.
    szamlalo.jegyez("sync_list", "nincs", "bela", "igazolas_nelkul")
    szamlalo.jegyez("sync_list", "nincs", "cili", "igazolas_nelkul")
    szamlalo.kiir()
    doc = gyujtemeny.find_one({"_id": NAP})
    assert doc["vegpontok"]["sync_list"] == {"ervenyes_egyezik": 2, "nincs": 3}
    assert sorted(doc["felhasznalok"]["igazolas_nelkul"]) == sorted(
        igazolas.ujjlenyomat(x) for x in ("bela", "cili"))


def test_ures_kiiras_nem_ir(szamlalo, gyujtemeny, capsys):
    szamlalo.kiir()
    assert gyujtemeny.count_documents({}) == 0
    assert capsys.readouterr().out == ""


def test_napok_kulon(szamlalo, gyujtemeny, ora):
    szamlalo.jegyez("get_list", "nincs")
    ora[0] += 24 * 3600
    szamlalo.jegyez("get_list", "nincs")
    szamlalo.jegyez("get_list", "nincs")
    szamlalo.kiir()
    szamok = {d["_id"]: d["vegpontok"]["get_list"]["nincs"] for d in gyujtemeny.find()}
    masnap = (datetime.fromtimestamp(MOST, timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    assert szamok == {NAP: 1, masnap: 2}


def test_magatol_kiir_ot_percenkent(szamlalo, gyujtemeny, ora):
    szamlalo.jegyez("get_list", "nincs")
    ora[0] += szamlalo.KIIRAS_MP - 1
    szamlalo.jegyez("get_list", "nincs")
    assert gyujtemeny.count_documents({}) == 0
    ora[0] += 1
    szamlalo.jegyez("get_list", "nincs")
    assert gyujtemeny.find_one({"_id": NAP})["vegpontok"]["get_list"]["nincs"] == 3


def test_kiirasi_hibanal_nem_vesz_el(szamlalo, gyujtemeny, ora, capsys):
    jo = szamlalo._gy
    szamlalo._gy = types.SimpleNamespace(
        update_one=mock.Mock(side_effect=RuntimeError("Mongo nem elérhető")))
    szamlalo.jegyez("sync_list", "ervenyes_egyezik", "anna", "igazolt")
    szamlalo.kiir()
    kimenet = capsys.readouterr().out
    assert "nem sikerült (RuntimeError)" in kimenet
    assert "előző kiírás óta" not in kimenet   # a meg nem írt számokat nem jelenti kiírtnak
    szamlalo._gy = jo
    szamlalo.jegyez("sync_list", "ervenyes_egyezik", "anna", "igazolt")
    szamlalo.kiir()
    doc = gyujtemeny.find_one({"_id": NAP})
    assert doc["vegpontok"]["sync_list"]["ervenyes_egyezik"] == 2
    assert doc["felhasznalok"]["igazolt"] == [igazolas.ujjlenyomat("anna")]
    szamlalo.kiir()   # nem írja ki még egyszer
    assert gyujtemeny.find_one({"_id": NAP})["vegpontok"]["sync_list"]["ervenyes_egyezik"] == 2


def test_csak_a_hibas_nap_marad_meg(szamlalo, gyujtemeny, ora):
    szamlalo.KIIRAS_MP = 10 ** 9   # csak kézi kiírás
    szamlalo.jegyez("get_list", "nincs")
    ora[0] += 24 * 3600
    szamlalo.jegyez("get_list", "hibas")
    valodi = gyujtemeny.update_one
    hivas = []

    def masodik_hibas(*a, **k):
        hivas.append(1)
        if len(hivas) == 2:
            raise RuntimeError("x")
        return valodi(*a, **k)

    szamlalo._gy = types.SimpleNamespace(update_one=masodik_hibas)
    szamlalo.kiir()
    szamlalo._gy = gyujtemeny
    szamlalo.kiir()
    szamok = sorted((d["_id"], d["vegpontok"]["get_list"]) for d in gyujtemeny.find())
    assert [s for _, s in szamok] == [{"nincs": 1}, {"hibas": 1}]


def test_kiiras_naplohiba_eseten_sem_dob(szamlalo, gyujtemeny, monkeypatch):
    # Leálláskor (atexit) a kimenet már lehet zárt vagy nem UTF-8-as.
    def rossz_print(*_a, **_k):
        raise UnicodeEncodeError("cp1250", "🔑", 0, 1, "nem kódolható")

    monkeypatch.setattr("builtins.print", rossz_print)
    szamlalo.jegyez("get_list", "nincs")
    szamlalo.kiir()
    monkeypatch.undo()
    assert gyujtemeny.find_one({"_id": NAP})["vegpontok"]["get_list"]["nincs"] == 1


def test_felhasznalok_szama_korlatos(szamlalo, gyujtemeny, monkeypatch):
    monkeypatch.setattr(igazolas.Szamlalo, "MAX_FELHASZNALO", 3)
    for i in range(10):
        szamlalo.jegyez("get_list", "nincs", f"u{i}", "igazolas_nelkul")
    szamlalo.kiir()
    doc = gyujtemeny.find_one({"_id": NAP})
    assert len(doc["felhasznalok"]["igazolas_nelkul"]) == 3
    assert doc["vegpontok"]["get_list"]["nincs"] == 10


# ---------------------------------------------------------------------------
# A szerverben (main.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def szerver():
    os.environ["BACKEND_API_KEY"] = KULCS
    os.environ.pop("BACKEND_API_KEY_OLD", None)
    os.environ["API_KEY"] = "nem-hasznalt"
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=lambda **_: object())
    with mock.patch("pymongo.MongoClient", mongomock.MongoClient):
        sys.modules.pop("main", None)
        modul = importlib.import_module("main")
    return modul


@pytest.fixture()
def app(szerver, kulcsok, monkeypatch):
    for g in (szerver.kollekcio, szerver.tagok_kollekcio, szerver.torolt_listak,
              szerver.vedelem_naplo, szerver.igazolas_szamlalo):
        g.delete_many({})
    szerver.VED.torol()
    letolto = Letolto(kulcsok)
    tar = igazolas.KulcsTar(letolto=letolto, ido=lambda: MOST)
    monkeypatch.setattr(szerver, "IGAZOLAS", igazolas.Ellenorzo(kulcstar=tar, ido=lambda: MOST))
    monkeypatch.setattr(szerver, "IGAZOLAS_SZAMLALO",
                        igazolas.Szamlalo(szerver.igazolas_szamlalo, ido=lambda: MOST))
    return szerver.app.test_client()


def szamok(szerver):
    szerver.IGAZOLAS_SZAMLALO.kiir()
    doc = szerver.igazolas_szamlalo.find_one({"_id": NAP}) or {}
    return doc.get("vegpontok", {})


def feltolt(app, user, fejlec, kod="AAA111"):
    return app.post("/sync_list", headers=fejlec, json={
        "family_id": kod, "user_id": user, "base_timestamp": 0,
        "list_data": {"storeName": "x", "items": [{"id": "Tej", "name": "Tej"}]}})


def test_szerver_szamol_de_nem_utasit_el(app, szerver, kulcsok):
    igazolassal = dict(FEJLEC, **{"X-ID-TOKEN": alair(kulcsok, sub="anna")})
    # Régi app: igazolás nélkül, ugyanúgy megy.
    assert feltolt(app, "anna", FEJLEC).status_code == 200
    # Új app: egyező igazolással.
    assert feltolt(app, "anna", igazolassal, kod="BBB222").status_code == 200
    assert app.get("/get_list?family_id=AAA111&user_id=anna",
                   headers=igazolassal).status_code == 200
    # Más igazolásával is kiszolgálva (még nincs tiltás), de külön számolva.
    r = app.get("/get_list?family_id=AAA111&user_id=bela", headers=igazolassal)
    assert r.status_code == 403   # bela nem tag: ugyanaz, mint igazolás nélkül
    assert app.get("/get_list?family_id=AAA111&user_id=bela",
                   headers=FEJLEC).status_code == 403
    # Hibás és lejárt igazolás: a kérés ugyanúgy megy.
    for tok in ("szemet", alair(kulcsok, sub="anna", exp=MOST - 3600)):
        assert app.get("/get_list?family_id=AAA111&user_id=anna",
                       headers=dict(FEJLEC, **{"X-ID-TOKEN": tok})).status_code == 200

    assert szamok(szerver) == {
        "sync_list": {"nincs": 1, "ervenyes_egyezik": 1},
        "get_list": {"ervenyes_egyezik": 1, "ervenyes_elter": 1, "nincs": 1,
                     "hibas": 1, "lejart": 1},
    }
    doc = szerver.igazolas_szamlalo.find_one({"_id": NAP})
    assert doc["felhasznalok"] == {
        "igazolt": [szerver.ujjlenyomat("anna")],
        "igazolas_nelkul": sorted({szerver.ujjlenyomat("anna"), szerver.ujjlenyomat("bela")}),
    }


def test_revenuecat_azonositos_vegpont_csak_ervenyesseget_szamol(app, szerver, kulcsok):
    fejlec = dict(FEJLEC, **{"X-ID-TOKEN": alair(kulcsok, sub="anna")})
    app.get("/ai_quota", headers=fejlec)            # azonosító nélkül: 400, de számít
    app.post("/redeem_coupon", headers=fejlec, json={})
    app.post("/topup_credits", headers=FEJLEC, json={})
    assert szamok(szerver) == {"ai_quota": {"ervenyes": 1}, "redeem_coupon": {"ervenyes": 1},
                               "topup_credits": {"nincs": 1}}
    assert "felhasznalok" not in szerver.igazolas_szamlalo.find_one({"_id": NAP})


def test_kulcs_nelkul_es_mas_vegponton_nem_szamol(app, szerver, kulcsok):
    tok = {"X-ID-TOKEN": alair(kulcsok, sub="anna")}
    assert app.get("/get_list?family_id=AAA111&user_id=anna", headers=tok).status_code == 401
    assert app.get("/latest_version", headers=tok).get_json()["szerver"] == "2026-11-06-igazolas"
    assert app.get("/", headers=tok).status_code == 200
    assert szamok(szerver) == {}


def test_szamlalasi_hiba_nem_allitja_meg_a_kerest(app, szerver, kulcsok, monkeypatch, capsys):
    monkeypatch.setattr(szerver.IGAZOLAS, "ellenoriz",
                        mock.Mock(side_effect=RuntimeError("váratlan")))
    fejlec = dict(FEJLEC, **{"X-ID-TOKEN": alair(kulcsok, sub="anna")})
    assert feltolt(app, "anna", fejlec).status_code == 200
    assert "számlálási hiba (RuntimeError)" in capsys.readouterr().out


def test_minden_szamolt_vegpont_letezik(szerver):
    utvonalak = {r.rule for r in szerver.app.url_map.iter_rules()}
    assert set(szerver.IGAZOLT_VEGPONTOK) <= utvonalak
    # Ahol a kérésben Google-azonosító van, ott össze is vetjük.
    google = {p for p, k in szerver.IGAZOLT_VEGPONTOK.items() if k}
    assert google == {"/sync_list", "/get_list", "/join_group", "/leave_group",
                      "/list_members", "/moderate_member"}


@pytest.mark.parametrize("utvonal, mezo", [
    ("/join_group", "user_id"), ("/leave_group", "user_id"), ("/moderate_member", "requester_id"),
])
def test_json_vegpontok_azonositoja(app, szerver, kulcsok, utvonal, mezo):
    fejlec = dict(FEJLEC, **{"X-ID-TOKEN": alair(kulcsok, sub="anna")})
    app.post(utvonal, headers=fejlec, json={mezo: "anna", "family_id": "ZZZ999"})
    app.post(utvonal, headers=fejlec, json={mezo: "bela", "family_id": "ZZZ999"})
    app.post(utvonal, headers=fejlec, json={"family_id": "ZZZ999"})
    nev = utvonal.strip("/")
    assert szamok(szerver) == {nev: {"ervenyes_egyezik": 1, "ervenyes_elter": 1,
                                     "ervenyes_azonosito_nelkul": 1}}


def test_list_members_azonositoja(app, szerver, kulcsok):
    fejlec = dict(FEJLEC, **{"X-ID-TOKEN": alair(kulcsok, sub="anna")})
    app.get("/list_members?family_id=ZZZ999&user_id=anna", headers=fejlec)
    app.get("/list_members?family_id=ZZZ999&user_id=" + "x" * 300, headers=fejlec)
    assert szamok(szerver) == {"list_members": {"ervenyes_egyezik": 1,
                                                "ervenyes_azonosito_nelkul": 1}}
