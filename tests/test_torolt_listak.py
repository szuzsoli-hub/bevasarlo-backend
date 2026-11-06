# A törölt közös listák szerver-oldali védelmének tesztje.
#
# Háttér: ha egy lista törlődött, de egy másik telefon nem kapta meg az értesítést,
# az a következő feltöltésével újra létrehozta a listát. A javítás óta a szerver
# megjegyzi a törölt kódokat (torolt_listak), és nem hozza létre őket újra.
#
# Valódi adatbázis nem kell: a tesztek memóriában futó MongoDB-utánzaton mennek,
# élő szerverhez és az Atlashoz nem nyúlnak.
#
# Futtatás:
#   pip install -r requirements.txt -r requirements-dev.txt
#   python -m pytest tests -q

import importlib
import os
import sys
import types
from unittest import mock

import mongomock
import pytest

KULCS = "teszt-kulcs"
FEJLEC = {"X-API-KEY": KULCS}


@pytest.fixture(scope="module")
def szerver():
    os.environ["BACKEND_API_KEY"] = KULCS
    os.environ.pop("BACKEND_API_KEY_OLD", None)
    os.environ["API_KEY"] = "nem-hasznalt"
    # Az AI-rész ehhez a teszthez nem kell, ne legyen a kulcsától és a csomagtól függő.
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=lambda **_: object())
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with mock.patch("pymongo.MongoClient", mongomock.MongoClient):
        sys.modules.pop("main", None)
        modul = importlib.import_module("main")
    return modul


@pytest.fixture()
def app(szerver):
    for gyujtemeny in (szerver.kollekcio, szerver.tagok_kollekcio, szerver.torolt_listak):
        gyujtemeny.delete_many({})
    szerver.VED.torol()
    return szerver.app.test_client()


def feltolt(app, kod, user, base=0, tetelek=("Tej",)):
    return app.post("/sync_list", headers=FEJLEC, json={
        "family_id": kod,
        "user_id": user,
        "base_timestamp": base,
        "list_data": {"storeName": "Próba", "items": [{"id": t, "name": t} for t in tetelek]},
    })


def lekerd(app, kod, user=None):
    utvonal = f"/get_list?family_id={kod}" + (f"&user_id={user}" if user else "")
    return app.get(utvonal, headers=FEJLEC)


def kilep(app, kod, user):
    return app.post("/leave_group", headers=FEJLEC, json={"family_id": kod, "user_id": user})


def csatlakozik(app, kod, user):
    return app.post("/join_group", headers=FEJLEC, json={
        "family_id": kod, "user_id": user, "user_name": user, "timestamp": 1,
    })


def kozos_lista(app, kod="ABC123", alapito="alapito", tag="tag"):
    assert feltolt(app, kod, alapito).status_code == 200
    assert csatlakozik(app, kod, tag).status_code == 200


# ---------------------------------------------------------------------------
# Mindhárom törlési út megjegyzi a kódot
# ---------------------------------------------------------------------------

def test_alapito_torlese_megjegyzi_a_kodot(app, szerver):
    kozos_lista(app)
    valasz = kilep(app, "ABC123", "alapito")
    assert valasz.get_json()["status"] == "owner_dissolved"
    assert szerver.kollekcio.find_one({"family_id": "ABC123"}) is None
    assert szerver.torolt_listak.find_one({"family_id": "ABC123"}) is not None


def test_utolso_tag_kilepese_megjegyzi_a_kodot(app, szerver):
    kozos_lista(app)
    # Az alapító helyett egy másik azonosító a lista tulajdonosa: így a sima kilépés
    # útja fut, és az utolsó tag távozásakor törlődik a lista.
    szerver.kollekcio.update_one({"family_id": "ABC123"}, {"$set": {"owner_id": "mas"}})
    assert kilep(app, "ABC123", "alapito").get_json()["status"] == "left"
    assert szerver.torolt_listak.find_one({"family_id": "ABC123"}) is None
    assert kilep(app, "ABC123", "tag").get_json()["status"] == "last_member_left"
    assert szerver.torolt_listak.find_one({"family_id": "ABC123"}) is not None


def test_fioktorles_megjegyzi_a_kodot(app, szerver):
    assert feltolt(app, "ABC123", "egyedul").status_code == 200
    assert app.post("/delete_user_data", headers=FEJLEC,
                    json={"user_id": "egyedul"}).status_code == 200
    assert szerver.kollekcio.find_one({"family_id": "ABC123"}) is None
    assert szerver.torolt_listak.find_one({"family_id": "ABC123"}) is not None


def test_fioktorles_nem_jegyzi_meg_ha_masok_maradnak(app, szerver):
    kozos_lista(app)
    assert app.post("/delete_user_data", headers=FEJLEC,
                    json={"user_id": "tag"}).status_code == 200
    assert szerver.kollekcio.find_one({"family_id": "ABC123"}) is not None
    assert szerver.torolt_listak.find_one({"family_id": "ABC123"}) is None


def test_kilepes_masok_mellett_nem_torol(app, szerver):
    kozos_lista(app)
    assert kilep(app, "ABC123", "tag").get_json()["status"] == "left"
    assert szerver.kollekcio.find_one({"family_id": "ABC123"}) is not None
    assert szerver.torolt_listak.find_one({"family_id": "ABC123"}) is None


def test_kod_nelkul_nem_jegyez_meg_semmit(app, szerver):
    assert kilep(app, None, "valaki").status_code == 200
    assert szerver.torolt_listak.count_documents({}) == 0


# ---------------------------------------------------------------------------
# A törölt lista nem jön létre újra
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", [0, 12345, None])
def test_torolt_kodra_a_feltoltes_410(app, szerver, base):
    kozos_lista(app)
    kilep(app, "ABC123", "alapito")

    valasz = feltolt(app, "ABC123", "tag", base=base)

    assert valasz.status_code == 410
    torzs = valasz.get_json()
    assert torzs["deleted"] is True
    assert torzs["exists"] is False
    assert szerver.kollekcio.find_one({"family_id": "ABC123"}) is None
    assert szerver.tagok_kollekcio.count_documents({"family_id": "ABC123"}) == 0


def test_az_eredeti_hiba_menete(app, szerver):
    """A tag appja nem futott a törléskor: letölt, majd visszatöltené a listát."""
    kozos_lista(app)
    kilep(app, "ABC123", "alapito")

    torzs = lekerd(app, "ABC123", "tag").get_json()
    assert torzs["exists"] is False
    assert torzs["deleted"] is True
    assert torzs["timestamp"] == 0

    assert feltolt(app, "ABC123", "tag", base=torzs["timestamp"]).status_code == 410
    assert lekerd(app, "ABC123", "tag").get_json()["exists"] is False
    assert szerver.kollekcio.count_documents({}) == 0


@pytest.mark.parametrize("user", [None, "tag", "idegen"])
def test_get_list_jelzi_a_torlest(app, user):
    kozos_lista(app)
    kilep(app, "ABC123", "alapito")
    valasz = lekerd(app, "ABC123", user)
    assert valasz.status_code == 200
    assert valasz.get_json()["deleted"] is True


def test_torolt_kodra_nem_lehet_csatlakozni(app):
    kozos_lista(app)
    kilep(app, "ABC123", "alapito")
    assert csatlakozik(app, "ABC123", "uj").status_code == 404


def test_ismetelt_kilepes_nem_ir_felul(app, szerver):
    kozos_lista(app)
    kilep(app, "ABC123", "alapito")
    elso = szerver.torolt_listak.find_one({"family_id": "ABC123"})["deleted_at"]
    assert kilep(app, "ABC123", "tag").status_code == 200
    assert szerver.torolt_listak.find_one({"family_id": "ABC123"})["deleted_at"] == elso
    assert szerver.torolt_listak.count_documents({"family_id": "ABC123"}) == 1


# ---------------------------------------------------------------------------
# Ami nem változhat
# ---------------------------------------------------------------------------

def test_soha_nem_hasznalt_kod_nem_torolt(app):
    torzs = lekerd(app, "UJKOD1", "valaki").get_json()
    assert torzs["exists"] is False
    assert torzs["deleted"] is False


def test_uj_lista_letrehozasa_valtozatlan(app, szerver):
    valasz = feltolt(app, "UJKOD1", "valaki")
    assert valasz.status_code == 200
    assert szerver.kollekcio.find_one({"family_id": "UJKOD1"})["owner_id"] == "valaki"
    torzs = lekerd(app, "UJKOD1", "valaki").get_json()
    assert torzs["exists"] is True
    assert "deleted" not in torzs


def test_elo_lista_rendes_feltoltese_valtozatlan(app):
    kozos_lista(app)
    alap = lekerd(app, "ABC123", "tag").get_json()["timestamp"]
    assert feltolt(app, "ABC123", "tag", base=alap, tetelek=("Tej", "Vaj")).status_code == 200
    assert len(lekerd(app, "ABC123", "tag").get_json()["list_data"]["items"]) == 2


def test_utkozes_valtozatlan(app):
    kozos_lista(app)
    valasz = feltolt(app, "ABC123", "tag", base=1)
    assert valasz.status_code == 409
    assert valasz.get_json()["status"] == "conflict"


def test_tagsag_ellenorzes_valtozatlan(app):
    kozos_lista(app)
    assert lekerd(app, "ABC123", "idegen").status_code == 403
    assert feltolt(app, "ABC123", "idegen").status_code == 403


def test_kulcs_nelkul_valtozatlanul_zart(app):
    assert app.get("/get_list?family_id=ABC123").status_code == 401


def test_ha_a_lista_mellett_ott_a_bejegyzes_a_lista_az_elso(app, szerver):
    """A törlés elakadt a bejegyzés után: a lista él tovább, a bejegyzés nem zavar."""
    kozos_lista(app)
    szerver.lista_torolve_jelol("ABC123")

    torzs = lekerd(app, "ABC123", "tag").get_json()
    assert torzs["exists"] is True
    assert "deleted" not in torzs
    assert feltolt(app, "ABC123", "tag", base=torzs["timestamp"]).status_code == 200
