# A Google-bejelentkezés igazolásának (X-ID-TOKEN) ellenőrzése és számlálása.
#
# Az 1.0.81-es app minden felhasználóhoz kötött hívásnál elküldi a Google által
# aláírt igazolást (ID token) az X-ID-TOKEN fejlécben. Ez a fájl:
#   - ellenőrzi az aláírást a Google nyilvános kulcsaival, a kibocsátót, a
#     címzettet (az app webes kliensazonosítója) és a lejáratot;
#   - összeveti a benne lévő Google-azonosítót (sub) a kérés user_id-jével;
#   - számolja az eredményeket naponta és végpontonként.
#
# Egyelőre SEMMIT NEM UTASÍT EL: a régi app (1.0.80) nem küld igazolást, és
# ugyanúgy ki kell szolgálni. A számokból látszik majd, mikor tehető kötelezővé.
#
# Itt semmi nem dobhat kivételt a hívó felé. Várni csak a Google kulcsainak
# letöltésekor kell (legfeljebb LETOLTES_IDOKORLAT mp), és ez ritka: a kulcsokat
# a Google által megadott ideig tároljuk, hiba után pedig UJRAPROBA mp-ig nem
# próbálkozunk újra.

import re
import threading
import time
from collections import defaultdict, namedtuple
from datetime import datetime, timezone

import requests
from google.auth import jwt as google_jwt

from vedelem import ujjlenyomat

FEJLEC = "X-ID-TOKEN"

# A Google igazolásainak aláíró kulcsai, tanúsítványként ({kid: PEM}).
KULCSOK_CIME = "https://www.googleapis.com/oauth2/v1/certs"
KIBOCSATOK = frozenset({"accounts.google.com", "https://accounts.google.com"})

# Az app webes kliensazonosítója (az appban a serverClientId): ez a címzett
# (aud) az igazolásban. Nem titok, az appban is benne van.
WEBES_KLIENS = "812386484370-s4ai361kuph460umbn66dt1j49gfql01.apps.googleusercontent.com"

ORAELTERES = 60            # ennyi mp eltérést tűrünk a Google órája és a miénk között
MAX_HOSSZ = 4096           # a Google igazolása kb. 1000–1500 jel; a hosszabbat meg sem nézzük
LETOLTES_IDOKORLAT = 4     # mp
UJRAPROBA = 60             # két kulcsletöltés között legalább ennyi mp
TAROLAS_ALAP = 3600        # ha a Google nem mondja meg, meddig tárolhatók a kulcsok
TAROLAS_MIN = 300
TAROLAS_MAX = 24 * 3600

# Az ellenőrzés eredménye.
NINCS = "nincs"                          # nem jött igazolás (régi app, kijelentkezett felhasználó)
ERVENYES = "ervenyes"
LEJART = "lejart"
HIBAS = "hibas"                          # sérült, nem a Google-é, vagy nem nekünk szól
NEM_ELLENORIZHETO = "nem_ellenorizheto"  # a Google kulcsai most nem érhetők el

# Érvényes igazolás és Google-azonosítós végpont esetén az összevetés eredménye.
EGYEZIK = "ervenyes_egyezik"
ELTER = "ervenyes_elter"
AZONOSITO_NELKUL = "ervenyes_azonosito_nelkul"

Eredmeny = namedtuple("Eredmeny", "allapot sub")


def _naplo(uzenet):
    """A Render naplójába. Leálláskor is hívódik (atexit), amikor a kimenet
    már lehet zárt vagy nem UTF-8-as: ilyenkor inkább elmarad, mint hogy dobjon."""
    try:
        print(uzenet)
    except Exception:
        pass


# ==============================================================================
# A Google kulcsai
# ==============================================================================

def tarolasi_ido(cache_control):
    """Hány mp-ig tárolhatók a kulcsok (a Cache-Control max-age értéke, észszerű
    határok között)."""
    m = re.search(r"max-age=(\d+)", cache_control or "")
    mp = int(m.group(1)) if m else TAROLAS_ALAP
    return min(max(mp, TAROLAS_MIN), TAROLAS_MAX)


def google_kulcsai():
    """A Google mostani kulcsai ({kid: tanúsítvány}) és a tárolási idejük."""
    valasz = requests.get(KULCSOK_CIME, timeout=LETOLTES_IDOKORLAT)
    valasz.raise_for_status()
    kulcsok = valasz.json()
    if not isinstance(kulcsok, dict) or not kulcsok or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in kulcsok.items()):
        raise ValueError("váratlan válasz")
    return kulcsok, tarolasi_ido(valasz.headers.get("Cache-Control"))


class KulcsTar:
    """A Google kulcsai a memóriában. Lejáratkor, vagy ha ismeretlen kulccsal
    aláírt igazolás jön, újra letölti, de UJRAPROBA mp-en belül legfeljebb
    egyszer (különben kitalált kulcsnevekkel a Google-höz lehetne küldeni minket).
    Ha a letöltés nem sikerül, a régi kulcsok maradnak: a Google a lecserélt
    kulcsot is napokig közzéteszi még, tehát azok közben is jók."""

    def __init__(self, letolto=google_kulcsai, ido=time.monotonic):
        self._letolto = letolto
        self._ido = ido
        self._zar = threading.Lock()
        self._kulcsok = {}
        self._lejar = 0.0       # eddig érvényesek a tárolt kulcsok
        self._kovetkezo = 0.0   # ennél előbb nem töltünk le újra

    def kulcs(self, kid):
        """(a kid tanúsítványa vagy None, van-e egyáltalán kulcsunk)"""
        with self._zar:
            most = self._ido()
            if (most >= self._lejar or kid not in self._kulcsok) and most >= self._kovetkezo:
                self._letolt(most)
            return self._kulcsok.get(kid), bool(self._kulcsok)

    def _letolt(self, most):
        self._kovetkezo = most + UJRAPROBA
        try:
            kulcsok, mp = self._letolto()
        except Exception as e:
            _naplo(f"🔑 Igazolás: a Google kulcsai most nem tölthetők le ({type(e).__name__}).")
            return
        self._kulcsok = dict(kulcsok)
        self._lejar = most + mp


# ==============================================================================
# Ellenőrzés
# ==============================================================================

def _szam(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class Ellenorzo:
    def __init__(self, kulcstar=None, cimzett=WEBES_KLIENS, ido=time.time):
        self._kulcsok = kulcstar or KulcsTar()
        self._cimzett = cimzett
        self._ido = ido

    def ellenoriz(self, token):
        """Eredmeny(allapot, sub). A sub csak érvényes vagy lejárt (de a Google
        által aláírt) igazolásnál van kitöltve. Soha nem dob."""
        if token is None or token == "":
            return Eredmeny(NINCS, None)
        try:
            return self._ellenoriz(token)
        except Exception:
            return Eredmeny(HIBAS, None)

    def _ellenoriz(self, token):
        if (not isinstance(token, str) or len(token) > MAX_HOSSZ
                or not token.isascii() or token.count(".") != 2):
            return Eredmeny(HIBAS, None)

        fej = google_jwt.decode_header(token)
        kid = fej.get("kid")
        if fej.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            return Eredmeny(HIBAS, None)

        tanusitvany, van_kulcs = self._kulcsok.kulcs(kid)
        if tanusitvany is None:
            # Kulcs nélkül nem tudunk dönteni; ha van kulcsunk, csak ez nem a
            # Google-é (a friss listában sincs benne).
            return Eredmeny(HIBAS if van_kulcs else NEM_ELLENORIZHETO, None)

        # Aláírás és címzett. Az időt alább a saját óránkkal nézzük (így a
        # lejárt igazolás külön számolható), ezért itt az időeltérés tetszőleges.
        adat = google_jwt.decode(token, certs={kid: tanusitvany},
                                 audience=self._cimzett,
                                 clock_skew_in_seconds=10 ** 10)

        sub = adat.get("sub")
        iat, exp = adat.get("iat"), adat.get("exp")
        if (adat.get("iss") not in KIBOCSATOK
                or not isinstance(sub, str) or not 0 < len(sub) <= 255
                or not _szam(iat) or not _szam(exp)):
            return Eredmeny(HIBAS, None)

        most = self._ido()
        if iat > most + ORAELTERES:
            return Eredmeny(HIBAS, None)   # a jövőben adták ki
        if exp < most - ORAELTERES:
            return Eredmeny(LEJART, sub)
        return Eredmeny(ERVENYES, sub)


def eset(eredmeny, azonosito, osszevetheto):
    """A számláló esete. osszevetheto: a végpont Google-azonosítót kap (a
    RevenueCat-azonosítós végpontoknál nincs mivel összevetni)."""
    if eredmeny.allapot != ERVENYES or not osszevetheto:
        return eredmeny.allapot
    if azonosito is None:
        return AZONOSITO_NELKUL
    return EGYEZIK if eredmeny.sub == azonosito else ELTER


# ==============================================================================
# Számlálás
# ==============================================================================

class Szamlalo:
    """Napi számok végpontonként és esetenként, plusz hogy hány különböző
    felhasználó küldött egyező igazolást, és hány egyáltalán nem küldött (csak
    az azonosítójuk ujjlenyomata, abból a darabszám). A memóriából KIIRAS_MP
    másodpercenként és leálláskor kerül az adatbázisba ($inc), így az
    újraindítás legfeljebb az utolsó néhány perc számait viszi el.

    Egy dokumentum naponta (_id: "ÉÉÉÉ-HH-NN", világidő szerint):
        vegpontok.<végpont>.<eset>: darab
        felhasznalok.<csoport>: [ujjlenyomatok]
    """

    KIIRAS_MP = 300
    MAX_FELHASZNALO = 5000   # naponta és csoportonként (ennyi felhasználónk messze nincs)

    def __init__(self, gyujtemeny, ido=time.time):
        self._gy = gyujtemeny
        self._ido = ido
        self._zar = threading.Lock()
        self._szamok = defaultdict(int)       # (nap, végpont, eset) -> darab
        self._felhasznalok = defaultdict(set)  # (nap, csoport) -> ujjlenyomatok
        self._utolso_kiiras = ido()

    def _nap(self):
        return datetime.fromtimestamp(self._ido(), timezone.utc).strftime("%Y-%m-%d")

    def jegyez(self, vegpont, eset_, felhasznalo=None, csoport=None):
        nap = self._nap()
        with self._zar:
            self._szamok[(nap, vegpont, eset_)] += 1
            if felhasznalo and csoport:
                halmaz = self._felhasznalok[(nap, csoport)]
                if len(halmaz) < self.MAX_FELHASZNALO:
                    halmaz.add(ujjlenyomat(felhasznalo))
            esedekes = self._ido() - self._utolso_kiiras >= self.KIIRAS_MP
            if esedekes:
                # Hogy a közben érkező kérések ne induljanak el ők is kiírni.
                self._utolso_kiiras = self._ido()
        if esedekes:
            self.kiir()

    def kiir(self):
        """A memóriában gyűlt számok az adatbázisba. Hiba esetén a meg nem írt
        napok számai visszakerülnek, a következő kiírás újra próbálja."""
        with self._zar:
            szamok, felhasznalok = self._szamok, self._felhasznalok
            self._szamok, self._felhasznalok = defaultdict(int), defaultdict(set)
            self._utolso_kiiras = self._ido()
        if not szamok and not felhasznalok:
            return

        napok = defaultdict(lambda: ({}, {}))
        for (nap, vegpont, eset_), db in szamok.items():
            napok[nap][0][f"vegpontok.{vegpont}.{eset_}"] = db
        for (nap, csoport), halmaz in felhasznalok.items():
            napok[nap][1][f"felhasznalok.{csoport}"] = {"$each": sorted(halmaz)}

        osszesen = defaultdict(int)
        for nap, (noveles, halmazok) in sorted(napok.items()):
            muvelet = {"$setOnInsert": {"letrejott": datetime.now(timezone.utc)}}
            if noveles:
                muvelet["$inc"] = noveles
            if halmazok:
                muvelet["$addToSet"] = halmazok
            try:
                self._gy.update_one({"_id": nap}, muvelet, upsert=True)
            except Exception as e:
                _naplo(f"🔑 Igazolás: a számok kiírása nem sikerült ({type(e).__name__}), "
                       f"a következő kiírás újra próbálja.")
                with self._zar:
                    for kulcs, db in szamok.items():
                        if kulcs[0] == nap:
                            self._szamok[kulcs] += db
                    for kulcs, halmaz in felhasznalok.items():
                        if kulcs[0] == nap:
                            self._felhasznalok[kulcs] |= halmaz
                continue
            for (n, _, eset_), db in szamok.items():
                if n == nap:
                    osszesen[eset_] += db

        if osszesen:
            _naplo("🔑 Igazolás az előző kiírás óta: "
                   + ", ".join(f"{e} {db}" for e, db in sorted(osszesen.items())))
