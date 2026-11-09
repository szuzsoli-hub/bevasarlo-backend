# A szerver naplójának kitakarása (2026.11.09.).
#
# A gunicorn hozzáférési naplója a kérés teljes címét írja ki, a lekérdezéssel
# együtt. A /get_list és a /list_members címében benne van a közös lista
# kódja (family_id) és a felhasználó azonosítója (user_id), aki pedig a kettőt
# ismeri, a listát olvashatja. Ez a modul a naplóban mindkettőből csak az
# első 4 jelet hagyja meg:
#
#   GET /get_list?family_id=ab12cd34ef56&user_id=1098...  ->
#   GET /get_list?family_id=ab12***&user_id=1098***
#
# Csak a naplósor változik: a kérést, a választ és az appot nem érinti.
# A gunicorn a gunicorn.conf.py-on keresztül tölti be (a Render indítási
# parancsa nem változik).

import logging
import re

# Ezeknek a lekérdezési mezőknek az értékét takarjuk ki. A név előtt nem
# állhat betű, szám vagy aláhúzás, így pl. az "app_user_id" nem esik bele.
KITAKART_MEZOK = ("family_id", "user_id")
LATHATO_JELEK = 4
JELOLO = "***"

_MINTA = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(KITAKART_MEZOK) + r")=([^&\s\"'#;]*)")


def _csere(talalat):
    ertek = talalat.group(2)
    if not ertek:
        return talalat.group(0)
    return f"{talalat.group(1)}={ertek[:LATHATO_JELEK]}{JELOLO}"


def takar(szoveg):
    """A szövegben a family_id=… és user_id=… értékéből csak az első 4 jel
    marad. Ami nem szöveg, azt változatlanul adja vissza."""
    if not isinstance(szoveg, str):
        return szoveg
    return _MINTA.sub(_csere, szoveg)


class TakaroSzuro(logging.Filter):
    """Naplószűrő: a kész naplósorból kitakarja az azonosítókat. Soha nem dob
    el sort, és ha bármi hiba van, a sort változatlanul engedi tovább."""

    def filter(self, record):
        try:
            uzenet = record.getMessage()
        except Exception:
            return True
        kitakart = takar(uzenet)
        if kitakart != uzenet:
            record.msg = kitakart
            record.args = None
        return True


def szurot_felrak(logger):
    """A szűrőt egyszer teszi fel a naplóra (újratöltéskor se legyen kettő)."""
    if not any(isinstance(f, TakaroSzuro) for f in logger.filters):
        logger.addFilter(TakaroSzuro())


try:
    from gunicorn.glogging import Logger as _GunicornNaplo
except Exception:  # pl. Windowson nincs gunicorn; a takar() akkor is működik
    _GunicornNaplo = None


if _GunicornNaplo is not None:
    class TakaroNaplo(_GunicornNaplo):
        """A gunicorn naplója, kitakarással a hozzáférési és a hibanaplóban
        (a hibanaplóba is kerülhet cím, pl. "Error handling request …")."""

        def setup(self, cfg):
            super().setup(cfg)
            szurot_felrak(self.access_log)
            szurot_felrak(self.error_log)
