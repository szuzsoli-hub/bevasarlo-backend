# A gunicorn ezt a fájlt magától betölti az indítási mappából, ezért a Render
# indítási parancsához (gunicorn -w 1 --threads 50 --bind 0.0.0.0:$PORT main:app)
# nem kell hozzányúlni. A parancssor és a GUNICORN_CMD_ARGS felülírja az itteni
# beállításokat.
#
# Egyetlen beállítás: a naplóban a family_id és a user_id értékéből csak az
# első 4 jel látszik (naplo_kitakaras.py). Ha a modul valamiért nem töltődne
# be, a szerver akkor is elindul, a régi naplóval.

try:
    from naplo_kitakaras import TakaroNaplo

    logger_class = TakaroNaplo
except Exception as hiba:  # pragma: no cover - csak hibás telepítésnél
    print(f"🚨 A napló kitakarása nem töltődött be, régi napló marad: {hiba}")
