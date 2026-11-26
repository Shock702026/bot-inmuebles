"""
Revisión diaria: corre el bot de inmuebles y el de remates, uno después del
otro, y manda todo junto por Telegram. Se ejecuta una sola vez por día.

- Ejecución normal (a las 9, la dispara el "despertador" externo o a mano):
  revisa siempre.
- Ejecución de respaldo (la programa GitHub): solo revisa si ese día todavía
  no se hizo la revisión, por si el despertador externo falló.
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import bot
import remates

MARCA = Path(__file__).parent / "ultima_revision.txt"


def hoy() -> str:
    return datetime.now(ZoneInfo("America/Montevideo")).date().isoformat()


def main() -> None:
    es_respaldo = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    if es_respaldo and MARCA.exists() and MARCA.read_text().strip() == hoy():
        print("La revisión de hoy ya se hizo: el respaldo no hace nada.")
        return

    total = 0
    try:
        total += bot.main() or 0
    except Exception as e:
        print(f"[!] Falló el bot de inmuebles: {e}")
        bot.send_telegram("⚠️ Hoy falló la revisión de inmuebles. Avisale a Claude.")
    try:
        total += remates.main() or 0
    except Exception as e:
        print(f"[!] Falló el bot de remates: {e}")
        bot.send_telegram("⚠️ Hoy falló la revisión de remates. Avisale a Claude.")

    if total == 0:
        bot.send_telegram("☀️ Revisión diaria hecha: hoy no hay inmuebles ni remates nuevos.")
    MARCA.write_text(hoy() + "\n", encoding="utf-8")


if __name__ == "__main__":
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(var):
            sys.exit(f"Falta la variable de entorno {var}")
    main()
