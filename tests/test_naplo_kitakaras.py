# A szervernapló kitakarásának tesztje (2026.11.09.).
#
# Mit ellenőriz:
#   - a gunicorn hozzáférési naplójában a /get_list és a /list_members címében
#     a family_id és a user_id értékéből csak az első 4 jel látszik;
#   - a hibanaplóba került címből is (pl. "Error handling request …");
#   - minden más változatlan marad (más mezők, más címek, a sor többi része);
#   - a gunicorn.conf.py valóban ezt a naplót kapcsolja be;
#   - a szűrő nem dob el sort, és hibás naplóhívásnál sem áll meg.
#
# A kérések és a válaszok nem változnak: a main.py-hoz a javítás nem nyúl, ezt
# a többi tesztfájl végpont-tesztjei mutatják.
#
# Futtatás:
#   pip install -r requirements.txt -r requirements-dev.txt
#   python -m pytest tests -q

import importlib
import io
import logging
import os
import sys
import types
from datetime import timedelta
from pathlib import Path

import pytest

GYOKER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GYOKER))

import naplo_kitakaras as nk  # noqa: E402

KOD = "ab12cd34ef56"
USER = "109876543210987654321"


# --- takar() ---------------------------------------------------------------

@pytest.mark.parametrize("be, ki", [
    (f"GET /get_list?family_id={KOD}&user_id={USER} HTTP/1.1",
     "GET /get_list?family_id=ab12***&user_id=1098*** HTTP/1.1"),
    (f"GET /list_members?family_id={KOD}&user_id={USER} HTTP/1.1",
     "GET /list_members?family_id=ab12***&user_id=1098*** HTTP/1.1"),
    # fordított sorrend, csak az egyik mező
    (f"/get_list?user_id={USER}&family_id={KOD}",
     "/get_list?user_id=1098***&family_id=ab12***"),
    (f"/get_list?family_id={KOD}", "/get_list?family_id=ab12***"),
    # idézőjelek között (a gunicorn alapformátuma így írja a kérés sorát)
    (f'"GET /get_list?family_id={KOD}&user_id={USER} HTTP/1.1" 200 512',
     '"GET /get_list?family_id=ab12***&user_id=1098*** HTTP/1.1" 200 512'),
    # százalékkal kódolt érték: az is csak 4 jel
    ("/get_list?family_id=%C3%A1rv%C3%ADzt%C5%B1r%C5%91",
     "/get_list?family_id=%C3%***"),
])
def test_takar_kitakarja_a_kodot_es_az_azonositot(be, ki):
    assert nk.takar(be) == ki


@pytest.mark.parametrize("szoveg", [
    "GET /latest_version HTTP/1.1",
    "GET /ai_quota?app_user_id=$RCAnonymousID:abcdef123456 HTTP/1.1",
    "GET /get_list?family_id=&user_id= HTTP/1.1",
    "GET /socket.io/?EIO=4&transport=polling&t=abc HTTP/1.1",
    "user_idx=nem-mezo",
    "",
])
def test_takar_mast_nem_bant(szoveg):
    assert nk.takar(szoveg) == szoveg


def test_takar_rovid_ertek_es_nem_szoveg():
    assert nk.takar("/get_list?family_id=ab1") == "/get_list?family_id=ab1***"
    assert nk.takar(None) is None
    assert nk.takar(42) == 42


def test_a_teljes_ertek_sehol_nem_marad_meg():
    sor = nk.takar(f"/get_list?family_id={KOD}&user_id={USER}&family_id={KOD}")
    assert KOD not in sor and USER not in sor
    assert KOD[4:] not in sor and USER[4:] not in sor


# --- naplószűrő --------------------------------------------------------------

def _naplo(nev):
    kimenet = io.StringIO()
    naplo = logging.getLogger(nev)
    naplo.handlers[:] = [logging.StreamHandler(kimenet)]
    naplo.setLevel(logging.INFO)
    naplo.propagate = False
    nk.szurot_felrak(naplo)
    return naplo, kimenet


def test_szuro_szotaras_es_sorrendi_parameterrel():
    naplo, kimenet = _naplo("teszt.kitakaras.1")
    naplo.info("%(r)s %(s)s", {"r": f"GET /get_list?family_id={KOD}", "s": "200"})
    naplo.error("Error handling request %s", f"/list_members?family_id={KOD}&user_id={USER}")
    sorok = kimenet.getvalue().splitlines()
    assert sorok == ["GET /get_list?family_id=ab12*** 200",
                     "Error handling request /list_members?family_id=ab12***&user_id=1098***"]


def test_szuro_nem_dob_el_sort_es_hibas_hivasnal_sem_all_meg():
    naplo, kimenet = _naplo("teszt.kitakaras.2")
    naplo.info("rendes sor")
    naplo.info("hibás %s %s", "csak egy")  # a formázás hibás: a logging kezeli
    naplo.info("utána is megy")
    assert "rendes sor" in kimenet.getvalue()
    assert "utána is megy" in kimenet.getvalue()


def test_szuro_egyszer_kerul_fel():
    naplo, _ = _naplo("teszt.kitakaras.3")
    nk.szurot_felrak(naplo)
    nk.szurot_felrak(naplo)
    assert sum(isinstance(f, nk.TakaroSzuro) for f in naplo.filters) == 1


# --- a valódi gunicorn-napló -------------------------------------------------

@pytest.fixture(scope="module")
def gunicorn_modulok():
    """A gunicorn csak Linuxon fut (a Render is Linux). Windowson a betöltéshez
    hiányzó három rendszermodult a teszt idejére üres pótlással helyettesítjük;
    a naplózó kódja ugyanaz marad."""
    with pytest.MonkeyPatch.context() as mp:
        for nev in ("fcntl", "pwd", "grp"):
            try:
                importlib.import_module(nev)
            except ImportError:
                mp.setitem(sys.modules, nev, types.ModuleType(nev))
        if not hasattr(os, "geteuid"):
            mp.setattr(os, "geteuid", lambda: 1000, raising=False)
            mp.setattr(os, "getegid", lambda: 1000, raising=False)
        config = importlib.import_module("gunicorn.config")
        modul = importlib.reload(nk)
        assert hasattr(modul, "TakaroNaplo")
        yield config, modul


class _Valasz:
    status = "200 OK"
    sent = 512
    headers = [("Content-Type", "application/json")]


class _Keres:
    headers = [("USER-AGENT", "Dart/3.5 (dart:io)"), ("HOST", "bevasarlo-backend.onrender.com")]


def _kornyezet(cim):
    ut, _, lekerdezes = cim.partition("?")
    return {
        "REQUEST_METHOD": "GET", "RAW_URI": cim, "PATH_INFO": ut,
        "QUERY_STRING": lekerdezes, "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "10.0.0.1", "HTTP_USER_AGENT": "Dart/3.5 (dart:io)",
    }


def _gunicorn_naplo(config, modul, formatum=None):
    cfg = config.Config()
    cfg.set("accesslog", "-")
    if formatum:
        cfg.set("access_log_format", formatum)
    cfg.set("logger_class", modul.TakaroNaplo)
    naplo = cfg.logger_class(cfg)
    kimenet = io.StringIO()
    for h in naplo.access_log.handlers + naplo.error_log.handlers:
        h.setStream(kimenet)
    return naplo, kimenet


@pytest.mark.parametrize("vegpont", ["/get_list", "/list_members"])
def test_gunicorn_hozzaferesi_naplo_alapformatum(gunicorn_modulok, vegpont):
    config, modul = gunicorn_modulok
    naplo, kimenet = _gunicorn_naplo(config, modul)
    naplo.access(_Valasz(), _Keres(), _kornyezet(f"{vegpont}?family_id={KOD}&user_id={USER}"),
                 timedelta(milliseconds=12))
    sor = kimenet.getvalue()
    assert f'"GET {vegpont}?family_id=ab12***&user_id=1098*** HTTP/1.1" 200 512' in sor
    assert KOD not in sor and USER not in sor
    assert "Dart/3.5 (dart:io)" in sor and "10.0.0.1" in sor


def test_gunicorn_minden_cimet_kiiro_mezo_kitakarva(gunicorn_modulok):
    """Ha valaki más formátumot állítana be (q = lekérdezés, {raw_uri}e és
    {query_string}e = a környezet mezői), ott sem látszhat a teljes érték."""
    config, modul = gunicorn_modulok
    naplo, kimenet = _gunicorn_naplo(
        config, modul, "%(r)s | %(q)s | %(U)s | %({raw_uri}e)s | %({query_string}e)s")
    naplo.access(_Valasz(), _Keres(), _kornyezet(f"/get_list?family_id={KOD}&user_id={USER}"),
                 timedelta(milliseconds=12))
    sor = kimenet.getvalue()
    assert KOD not in sor and USER not in sor
    utolso = sor.strip().splitlines()[-1]
    assert utolso.count("family_id=ab12***") == 4
    assert utolso.count("user_id=1098***") == 4
    assert "| /get_list |" in utolso


def test_gunicorn_mas_cim_valtozatlan(gunicorn_modulok):
    config, modul = gunicorn_modulok
    naplo, kimenet = _gunicorn_naplo(config, modul)
    naplo.access(_Valasz(), _Keres(), _kornyezet("/latest_version"), timedelta(milliseconds=3))
    assert '"GET /latest_version HTTP/1.1" 200 512' in kimenet.getvalue()


def test_gunicorn_hibanaplo_kitakarva(gunicorn_modulok):
    config, modul = gunicorn_modulok
    naplo, kimenet = _gunicorn_naplo(config, modul)
    naplo.error("Error handling request %s", f"/get_list?family_id={KOD}&user_id={USER}")
    sor = kimenet.getvalue()
    assert "Error handling request /get_list?family_id=ab12***&user_id=1098***" in sor
    assert KOD not in sor and USER not in sor


def test_gunicorn_ujratoltes_utan_is_egy_szuro(gunicorn_modulok):
    config, modul = gunicorn_modulok
    naplo, _ = _gunicorn_naplo(config, modul)
    naplo.setup(naplo.cfg)  # pl. HUP jelre
    for n in (naplo.access_log, naplo.error_log):
        assert sum(isinstance(f, modul.TakaroSzuro) for f in n.filters) == 1


def test_gunicorn_conf_ezt_a_naplot_kapcsolja_be(gunicorn_modulok):
    """A gunicorn a gunicorn.conf.py-t a saját betöltőjével olvassa be; itt
    is így töltjük, és megnézzük, hogy a napló osztálya a kitakaró."""
    config, modul = gunicorn_modulok
    cfg = config.Config()
    hely = {"__file__": str(GYOKER / "gunicorn.conf.py"), "__name__": "__config__"}
    kod = (GYOKER / "gunicorn.conf.py").read_text(encoding="utf-8")
    exec(compile(kod, "gunicorn.conf.py", "exec"), hely)
    cfg.set("logger_class", hely["logger_class"])
    assert cfg.logger_class.__name__ == "TakaroNaplo"
    assert issubclass(cfg.logger_class, importlib.import_module("gunicorn.glogging").Logger)
    # A Render indítási parancsa: ezekkel együtt is ez a napló marad.
    for kulcs, ertek in (("workers", 1), ("threads", 50), ("bind", ["0.0.0.0:10000"])):
        cfg.set(kulcs, ertek)
    assert cfg.logger_class.__name__ == "TakaroNaplo"


RENDER_INDITAS = ["gunicorn", "-w", "1", "--threads", "50", "--bind", "0.0.0.0:10000", "main:app"]
# A Render ezt magától beállítja a Python-szolgáltatásoknak (render.com/docs/
# environment-variables, "Python 3"): innen jön a hozzáférési napló.
RENDER_GUNICORN_CMD_ARGS = "--preload --access-logfile - --bind=0.0.0.0:10000"


def test_render_beallitasaival_is_a_kitakaro_naplo_fut(gunicorn_modulok, monkeypatch):
    """A gunicorn saját betöltője (WSGIApplication.load_config) a Render
    indítási parancsával és a GUNICORN_CMD_ARGS-szal: a gunicorn.conf.py
    betöltődik, a hozzáférési napló be van kapcsolva, és a sor kitakart."""
    config, modul = gunicorn_modulok
    # A Windows-on hiányzó folyamatkezelő (Arbiter) itt nem kell: csak a
    # beállítások betöltését futtatjuk, a szervert nem indítjuk el.
    if "gunicorn.app.base" not in sys.modules:
        try:
            importlib.import_module("gunicorn.arbiter")
        except Exception:
            arbiter = types.ModuleType("gunicorn.arbiter")
            arbiter.Arbiter = object
            monkeypatch.setitem(sys.modules, "gunicorn.arbiter", arbiter)
    wsgiapp = importlib.import_module("gunicorn.app.wsgiapp")

    monkeypatch.chdir(GYOKER)
    monkeypatch.setattr(config.Chdir, "default", str(GYOKER))
    monkeypatch.setenv("GUNICORN_CMD_ARGS", RENDER_GUNICORN_CMD_ARGS)
    monkeypatch.setattr(sys, "argv", RENDER_INDITAS)

    alkalmazas = wsgiapp.WSGIApplication("%(prog)s [OPTIONS] [APP_MODULE]")
    cfg = alkalmazas.cfg
    assert cfg.accesslog == "-"
    assert cfg.preload_app is True
    assert (cfg.workers, cfg.threads, cfg.bind) == (1, 50, ["0.0.0.0:10000"])
    assert alkalmazas.app_uri == "main:app"
    assert cfg.logger_class.__name__ == "TakaroNaplo"

    naplo = cfg.logger_class(cfg)
    kimenet = io.StringIO()
    for h in naplo.access_log.handlers:
        h.setStream(kimenet)
    naplo.access(_Valasz(), _Keres(), _kornyezet(f"/get_list?family_id={KOD}&user_id={USER}"),
                 timedelta(milliseconds=12))
    sor = kimenet.getvalue().strip().splitlines()[-1]
    assert '"GET /get_list?family_id=ab12***&user_id=1098*** HTTP/1.1" 200 512' in sor
    assert KOD not in sor and USER not in sor


def test_a_main_py_nem_valtozott_a_napló_miatt():
    """A kitakarás csak a naplót érinti: a Flask-app nem tölti be."""
    forras = (GYOKER / "main.py").read_text(encoding="utf-8")
    assert "naplo_kitakaras" not in forras
