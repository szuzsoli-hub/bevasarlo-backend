# vedelem.py – a szerver védelme (2026.11.06., biztonsági átnézés).
#
# 1. Bemenet-ellenőrzés. A kérésből jövő azonosító és kód csak rövid szöveg
#    lehet. Más típus (pl. JSON-objektum) az adatbázis-keresésben feltételként
#    működne, és idegen adatokat érhetne el, írhatna felül vagy törölhetne.
#
# 2. Túl sok kérés. Aki rövid idő alatt szokatlanul sokat kér, ideiglenes
#    tiltást kap (429-es válasz), és ez a naplóba kerül. A számok a SZABALYOK
#    táblában vannak. A rendes használatnak bőven beleférnek (10–20 fotó
#    egymás után, egy család egy otthoni címről, sok közös lista).
#
#    A tiltás csak a szabály területére szól: aki túl sok rossz kódot próbál,
#    attól a kódkeresés áll le, a saját listáinak szinkronja nem. A "minden"
#    terület mindent letilt, ennek a határai a legmagasabbak.
#
#    A számlálók a memóriában vannak, újraindításkor nullázódnak. Ez szándékos:
#    egyszerű és gyors, és a Renderen egy folyamat fut (a Socket.IO miatt
#    egynél több nem is működne). Ha egyszer több lesz, a számlálókat közös
#    tárba (pl. MongoDB) kell tenni.
#
#    A kérő címét (IP) csak a Cloudflare fejlécéből vesszük (CF-Connecting-IP),
#    ezt a kérő nem tudja meghamisítani. Más fejlécbe (X-Forwarded-For) bármit
#    írhatna, és azzal mások címét tiltathatná le. Ha a fejléc hiányzik, a
#    címre vonatkozó szabályok nem futnak (különben mindenki egy címnek látszana).

import hashlib
import ipaddress
import threading
import time
from collections import defaultdict, deque

MAX_AZONOSITO = 128   # Google-fiók (21 számjegy), RevenueCat ($RCAnonymousID:…, 46 jel)
MAX_NEV = 100         # a tag neve, ennyire vágjuk
KOD_MINTA_HOSSZ = (6, 12)


def szoveg(ertek, max_hossz=MAX_AZONOSITO):
    """Az érték, ha nem üres, legfeljebb max_hossz hosszú, nyomtatható szöveg;
    különben None."""
    if isinstance(ertek, str) and 0 < len(ertek) <= max_hossz and ertek.isprintable():
        return ertek
    return None


def kod(ertek):
    """A közös lista kódja: 6–12 nagybetű vagy számjegy (a mostaniak 6 jelű
    hexák). Különben None."""
    if not isinstance(ertek, str):
        return None
    also, felso = KOD_MINTA_HOSSZ
    if also <= len(ertek) <= felso and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in ertek):
        return ertek
    return None


def nev(ertek):
    """A tag neve: szöveg, MAX_NEV hosszra vágva (a régi app hosszabbat is
    küldhet, azt nem utasítjuk el). Nem szöveg vagy üres: None."""
    if not isinstance(ertek, str):
        return None
    ertek = "".join(c for c in ertek if c.isprintable()).strip()[:MAX_NEV]
    return ertek or None


def egesz(ertek):
    """Egész szám (a bool nem az), különben None."""
    if isinstance(ertek, int) and not isinstance(ertek, bool):
        return ertek
    return None


def ujjlenyomat(szoveg_):
    """Rövid, vissza nem fejthető jel a naplóhoz és a számlálókhoz."""
    return hashlib.sha256(("bevasarlo:" + szoveg_).encode("utf-8")).hexdigest()[:16]


def cim_kulcs(ip):
    """A cím ujjlenyomata. IPv6-nál a /64-es előtag számít: egy előfizetés
    ezen belül bármennyi címet kaphat (és a telefon magától is váltogatja)."""
    if not isinstance(ip, str) or not ip:
        return None
    try:
        cim = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if cim.version == 6 and cim.ipv4_mapped:
        alap = str(cim.ipv4_mapped)
    elif cim.version == 6:
        alap = str(ipaddress.ip_network(f"{cim}/64", strict=False).network_address) + "/64"
    else:
        alap = str(cim)
    return "ip:" + ujjlenyomat(alap)


def felhasznalo_kulcs(azonosito):
    return "f:" + ujjlenyomat(azonosito)


PERC = 60
ORA = 60 * PERC
NAP = 24 * ORA

# név: (terület, ablak mp-ben, legfeljebb ennyi esemény az ablakban, tiltás mp-ben)
# Ha az ablakban több esemény van, mint a határ, a kulcs az adott területen a
# tiltás idejére 429-et kap.
SZABALYOK = {
    # Egy címről (IP), bármilyen felhasználónévvel:
    "ip_osszes":     ("minden",   1 * PERC, 600, ORA),  # minden kérés
    "ip_ai":         ("ai",      10 * PERC,  60, ORA),  # /analyze, /ai_quota
    "ip_kodkereses": ("kod",     10 * PERC,  60, ORA),  # nem létező kód keresése, csatlakozása
    "ip_uj_lista":   ("uj_lista",       ORA, 20, ORA),  # új közös lista
    "ip_kep_ora":    ("kep",            ORA, 30, ORA),  # képfeltöltés (/upload_image)
    "ip_kep_nap":    ("kep",            NAP, 100, ORA),
    "ip_kupon":      ("kupon",          ORA, 20, ORA),  # kuponbeváltás
    # Egy felhasználótól (Google- vagy RevenueCat-azonosító):
    "f_osszes":      ("minden",   1 * PERC, 300, ORA),  # minden kérés az azonosítójával
    "f_ai_keres":    ("ai",       1 * PERC,  30, ORA),  # /analyze és /ai_quota, az elutasított is
    "f_ai_kep":      ("ai",             ORA, 40, ORA),  # elemzésre beküldött kép
    "f_uj_lista":    ("uj_lista",       ORA, 20, ORA),
    "f_kupon":       ("kupon",          ORA, 10, ORA),
}

# Ugyanaz a felhasználó egyszerre sok különböző címről (pl. kiszivárgott
# azonosítót sokan használnak): (ablak, legfeljebb ennyi különböző cím, tiltás).
# Wifi és mobilnet váltogatása 2–3 cím; az IPv6-os címek /64-esként számítanak.
SOK_CIM = (10 * PERC, 8, ORA)

TERULETEK = {"minden", "ai", "kod", "uj_lista", "kep", "kupon"}


class Vedelem:
    """Csúszó ablakos számlálók, ideiglenes tiltások, és egyszerre futó
    kérések száma. Szálbiztos."""

    def __init__(self, naplo=None, ido=time.time, szabalyok=None, sok_cim=None):
        self._zar = threading.Lock()
        self._naplo = naplo                    # függvény(dict), a tiltások naplója
        self._ido = ido
        self._szabalyok = dict(szabalyok or SZABALYOK)
        self._sok_cim = sok_cim or SOK_CIM
        self.torol()

    def torol(self):
        """Minden számláló és tiltás nullázása (tesztekhez)."""
        with self._zar:
            self._esemenyek = defaultdict(deque)   # (szabály, kulcs) -> időpontok
            self._cimek = defaultdict(dict)        # felhasználó-kulcs -> {cím-kulcs: utolsó idő}
            self._tiltasok = {}                    # (terület, kulcs) -> lejárat
            self._futo = defaultdict(int)          # kulcs -> egyszerre futó kérések
            self._hivasok = 0

    # -- tiltás lekérdezése --------------------------------------------------

    def tiltva(self, kulcs, terulet):
        """Hány másodperc van még hátra a tiltásból ezen a területen
        (a "minden" tiltás is számít). 0: nincs tiltva."""
        with self._zar:
            return self._hatra(kulcs, terulet, self._ido())

    def _hatra(self, kulcs, terulet, most):
        hatra = 0
        for t in {terulet, "minden"}:
            lejar = self._tiltasok.get((t, kulcs))
            if lejar is None:
                continue
            if lejar <= most:
                del self._tiltasok[(t, kulcs)]
                continue
            hatra = max(hatra, int(lejar - most) + 1)
        return hatra

    # -- események -----------------------------------------------------------

    def esemeny(self, szabaly, kulcs):
        """Rögzít egy eseményt. Visszaadja, hány másodperc tiltás van még hátra
        (0: mehet). Ha ez az esemény lépi túl a határt, most indul a tiltás.
        Tiltás alatt az esemény nem számít (a tiltás lejárta után újra
        számolunk, de a még ablakon belüli régiek megmaradnak)."""
        terulet, ablak, hatar, tiltas = self._szabalyok[szabaly]
        with self._zar:
            most = self._ido()
            self._takarit(most)
            hatra = self._hatra(kulcs, terulet, most)
            if hatra:
                return hatra
            sor = self._esemenyek[(szabaly, kulcs)]
            while sor and sor[0] <= most - ablak:
                sor.popleft()
            sor.append(most)
            if len(sor) > hatar:
                return self._tilt(kulcs, terulet, szabaly, most, tiltas)
            return 0

    def cim(self, felhasznalo, cim):
        """Ugyanaz a felhasználó hány különböző címről kér. Ha túl sokról,
        a felhasználó mindenre tiltást kap. Visszatérés: mint az esemeny()."""
        ablak, hatar, tiltas = self._sok_cim
        with self._zar:
            most = self._ido()
            hatra = self._hatra(felhasznalo, "minden", most)
            if hatra:
                return hatra
            cimek = self._cimek[felhasznalo]
            cimek[cim] = most
            for regi in [c for c, t in cimek.items() if t <= most - ablak]:
                del cimek[regi]
            if len(cimek) > hatar:
                cimek.clear()
                return self._tilt(felhasznalo, "minden", "f_sok_cim", most, tiltas)
            return 0

    # -- egyszerre futó kérések ----------------------------------------------

    def elkezd(self, kulcs, legfeljebb=1):
        """Foglal egy helyet; False, ha már legfeljebb ennyi fut ezzel a
        kulccsal. A foglalást a befejez() szabadítja fel."""
        with self._zar:
            if self._futo[kulcs] >= legfeljebb:
                return False
            self._futo[kulcs] += 1
            return True

    def befejez(self, kulcs):
        with self._zar:
            self._futo[kulcs] -= 1
            if self._futo[kulcs] <= 0:
                del self._futo[kulcs]

    # -- belső ---------------------------------------------------------------

    def _tilt(self, kulcs, terulet, szabaly, most, tiltas):
        self._tiltasok[(terulet, kulcs)] = most + tiltas
        if self._naplo:
            try:
                self._naplo({
                    "tipus": "cim" if kulcs.startswith("ip:") else "felhasznalo",
                    "kulcs": kulcs,
                    "terulet": terulet,
                    "szabaly": szabaly,
                    "tiltas_mp": tiltas,
                })
            except Exception as e:  # a napló hibája ne állítsa meg a kérést
                print(f"🚨 Védelem: a tiltást nem sikerült naplózni: {type(e).__name__}")
        return int(tiltas)

    def _takarit(self, most):
        """Időnként kidobja a lejárt bejegyzéseket, hogy a memória ne nőjön."""
        self._hivasok += 1
        if self._hivasok % 5000:
            return
        # Minden szabály a saját ablakával: a percesek kulcsai egy perc után
        # eltűnnek, így sok kitalált azonosító sem tölti meg a memóriát.
        for k in [k for k, sor in self._esemenyek.items()
                  if not sor or sor[-1] <= most - self._szabalyok[k[0]][1]]:
            del self._esemenyek[k]
        for k in [k for k, lejar in self._tiltasok.items() if lejar <= most]:
            del self._tiltasok[k]
        for k in [k for k, c in self._cimek.items() if not c or max(c.values()) <= most - self._sok_cim[0]]:
            del self._cimek[k]
