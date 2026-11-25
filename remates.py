"""
Bot de remates -> avisos por Telegram.

Una vez por día revisa rematadores.com y los remates publicados en el Diario
Oficial (IMPO), y avisa por Telegram de los remates NUEVOS de casas y terrenos
en Maldonado. Si el mismo remate aparece en las dos fuentes, llega una sola vez
(se reconoce por fecha, hora y rematador).
Los remates ya avisados se guardan en remates_seen.json.

Usa los mismos secretos que el bot de inmuebles:
  TELEGRAM_TOKEN    token del bot
  TELEGRAM_CHAT_ID  uno o varios Id separados por coma
"""

import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
SEEN_FILE = BASE / "remates_seen.json"
URL = "https://www.rematadores.com/rematadores/default.asp"
URL_IMPO = "https://www.impo.com.uy/remates"
URL_REMATE = "https://www.rematadores.com/rematadores/idremate_2020.asp?k=1&idremate={id}"
CAT_INMUEBLES = "cat9"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower()).strip()


def contiene(texto_n: str, palabras) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", texto_n) for p in palabras)


# Lugares de Maldonado (en la descripción del bien o en el lugar del remate)
MALDONADO = [
    "maldonado", "punta del este", "punta ballena", "solanas", "solana",
    "sauce de portezuelo", "portezuelo", "san carlos", "piriapolis",
    "pan de azucar", "la barra", "jose ignacio", "manantiales", "ocean park",
    "lausana", "chihuahua", "laguna del sauce", "el tesoro", "la sonrisa",
    "aigua", "garzon", "gregorio aznarez", "playa hermosa", "playa verde",
    "punta colorada", "punta negra", "balneario buenos aires", "el chorro",
    "cerro pelado", "maldonado nuevo", "pinares", "cantegril", "san rafael",
    "gorlero", "peninsula", "las grutas", "lomo de la ballena", "rincon del indio",
]
# Otros departamentos y lugares frecuentes: si la descripción los nombra
# (y no nombra Maldonado), el bien no está en Maldonado.
OTROS = [
    "montevideo", "canelones", "rocha", "colonia", "san jose", "florida",
    "lavalleja", "treinta y tres", "cerro largo", "melo", "rivera", "artigas",
    "salto", "paysandu", "rio negro", "soriano", "mercedes", "flores",
    "durazno", "tacuarembo", "atlantida", "marindia", "el pinar", "las piedras",
    "ciudad de la costa", "solymar", "parque del plata", "la floresta",
    "cabo polonio", "la paloma", "minas", "libertad", "bella union",
]
# Tipos de bien buscados
TIPOS = {
    "terreno": ["terreno", "terrenos", "solar", "solares", "fraccion", "fracciones",
                "predio", "predios", "chacra", "chacras", "lote", "lotes"],
    "casa": ["casa", "casas", "vivienda", "viviendas", "chalet", "cabana",
             "cabanas", "duplex"],
}


def en_maldonado(desc: str, lugar: str) -> bool:
    d, l = norm(desc), norm(lugar)
    if contiene(d, MALDONADO):
        return True
    if contiene(d, OTROS):
        return False
    return contiene(l, MALDONADO)        # la descripción no dice dónde: se usa el lugar del remate


def tipo_bien(desc: str) -> str | None:
    d = norm(desc)
    if re.match(r"(bien )?mueble\b", d):                 # vehículos y otros bienes muebles
        return None
    es_casa = contiene(d, TIPOS["casa"])
    if not es_casa and ("propiedad horizontal" in d or re.search(r"\b(apartamento|local|oficina)\b", d)):
        return None                                        # apartamentos y locales
    d = re.sub(r"\blotes? de (?!terreno)", " ", d)    # "lotes de instrumentos" no es terreno
    hallados = [t for t, palabras in TIPOS.items() if contiene(d, palabras)]
    if not hallados:
        return None
    return " con ".join(sorted(hallados, reverse=True))   # "terreno con casa", "casa", "terreno"


def leer_remates(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for card in soup.find_all("card"):
        clases = card.get("class") or []
        if CAT_INMUEBLES not in clases:
            continue
        link = card.find("a", href=re.compile(r"idremate=\d+"))
        if not link:
            continue
        rid = re.search(r"idremate=(\d+)", link["href"]).group(1)
        fecha = " ".join(d.get_text(" ", strip=True)
                         for d in card.select(".cuadro_fecha div"))
        f = next((c for c in clases if re.fullmatch(r"f\d{8}", c)), "")
        hora = card.select_one(".cuadro_fecha .hora")
        lugar = card.select_one("p.title")
        desc = card.select_one("p.desc")
        rematador = card.select_one(".inforematador")
        texto = card.get_text(" ", strip=True)
        estado = ("suspendido" if "S U S P E N D I D O" in texto
                  else "prorrogado" if re.search(r"P\s?R\s?O\s?R\s?R\s?O\s?G", texto)
                  else "")
        out.append({
            "id": rid,
            "fuente": "rematadores",
            "fecha_iso": f"{f[1:5]}-{f[5:7]}-{f[7:9]}" if f else "",
            "hora": hora.get_text(strip=True) if hora else "",
            "fecha": fecha.title(),
            "lugar": lugar.get_text(" ", strip=True) if lugar else "",
            "desc": desc.get_text(" ", strip=True) if desc else "",
            "rematador": rematador.get_text(" ", strip=True) if rematador else "",
            "estado": estado,
            "url": URL_REMATE.format(id=rid),
        })
    return out


def leer_impo(html_text: str, depto: str = "MALDONADO") -> list[dict]:
    """Remates del Diario Oficial (IMPO) de un departamento."""
    soup = BeautifulSoup(html_text, "html.parser")
    out, actual = [], ""
    for item in soup.select("div.item_info"):
        titulo = item.select_one(".ctitulo")
        if titulo:
            actual = titulo.get_text(" ", strip=True).upper()
            continue
        if norm(actual) != norm(depto):
            continue
        cab = item.select_one(".rfecha")
        cab_t = cab.get_text(" ", strip=True) if cab else ""
        m_f = re.search(r"Fecha:\s*(\d{1,2})/(\d{1,2})/(\d{4})", cab_t)
        m_h = re.search(r"Hora:\s*(\d{1,2}:\d{2})", cab_t)
        m_l = re.search(r"Lugar:\s*(.+)$", cab_t)
        campos = {}
        for c in item.select(".cinfo"):
            t = c.get_text(" ", strip=True)
            k, _, v = t.partition(" - ")
            campos[norm(k)] = v.strip()
        link = item.select_one(".cfecha a[href]")
        if not link:
            continue
        aviso = link["href"].replace("http://", "https://")
        rid = re.search(r"([\d]+-\d{4})", aviso)
        out.append({
            "id": "impo:" + (rid.group(1) if rid else aviso),
            "fuente": "impo",
            "fecha_iso": f"{m_f.group(3)}-{int(m_f.group(2)):02d}-{int(m_f.group(1)):02d}" if m_f else "",
            "hora": m_h.group(1) if m_h else "",
            "fecha": (f"{int(m_f.group(1))}/{int(m_f.group(2))}/{m_f.group(3)}" if m_f else "")
                     + (f" {m_h.group(1)}" if m_h else ""),
            "lugar": m_l.group(1).strip() if m_l else "",
            "desc": campos.get("bien a rematar", ""),
            "condiciones": campos.get("condiciones", ""),
            "rematador": campos.get("rematador", ""),
            "estado": "",
            "url": aviso,
            "en_maldonado": True,           # ya viene de la sección del departamento
        })
    return out


# ----------------------------------------------------------------------------
# Mismo remate en las dos fuentes
# ----------------------------------------------------------------------------
NO_APELLIDO = {"remates", "remate", "subastas", "hijos", "casa", "online", "local",
               "matricula", "varios", "rematadores"}


def apellidos(rematador: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", norm(rematador)) if w not in NO_APELLIDO}


def clave(r: dict) -> dict:
    return {"f": r.get("fecha_iso", ""), "h": r.get("hora", ""),
            "a": sorted(apellidos(r.get("rematador", ""))), "t": r.get("tipo", "")}


def mismo_remate(a: dict, b: dict) -> bool:
    if not a["f"] or a["f"] != b["f"] or a["h"] != b["h"]:
        return False
    if a["a"] and b["a"]:
        return bool(set(a["a"]) & set(b["a"]))
    return a["t"] == b["t"]               # sin rematador en alguna: misma fecha, hora y tipo


def filtrar(remates: list[dict]) -> list[dict]:
    elegidos = []
    for r in remates:
        t = tipo_bien(r["desc"])
        if t and (r.get("en_maldonado") or en_maldonado(r["desc"], r["lugar"])):
            r["tipo"] = t
            elegidos.append(r)
    return elegidos


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    for cid in [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]:
        for _ in range(3):
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": cid, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": False}, timeout=30)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
                continue
            if not r.ok:
                print(f"  [!] No se pudo enviar a {cid}: {r.status_code} {r.text[:200]}")
            break
        time.sleep(1.1)


def formatear(r: dict) -> str:
    e = html.escape
    emoji = "🏡" if "casa" in r["tipo"] else "🌳"
    aviso = "⚠️ <b>PRORROGADO</b>\n" if r["estado"] == "prorrogado" else ""
    desc = r["desc"] if len(r["desc"]) <= 350 else r["desc"][:347].rsplit(" ", 1)[0] + "…"
    fuente = "Diario Oficial (IMPO)" if r["fuente"] == "impo" else "rematadores.com"
    return (
        f"🔨 <b>Remate</b> · {emoji} {e(r['tipo'].capitalize())}\n"
        f"{aviso}"
        f"<b>{e(desc)}</b>\n"
        f"📅 {e(r['fecha'])}\n"
        f"📍 Remate en: {e(r['lugar'])}\n"
        + (f"👤 {e(r['rematador'])}\n" if r["rematador"] else "")
        + (f"💵 {e(r['condiciones'])}\n" if r.get("condiciones") else "")
        + f"🌐 {fuente} · <a href=\"{e(r['url'], quote=True)}\">Ver remate</a>"
    )


def bajar(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
        resp.encoding = "iso-8859-1"
    return resp.text


def cargar_memoria() -> dict:
    if not SEEN_FILE.exists():
        return {"ids": [], "enviados": []}
    data = json.loads(SEEN_FILE.read_text())
    if isinstance(data, list):                 # formato viejo: solo ids
        data = {"ids": data, "enviados": []}
    return data


def main() -> None:
    from datetime import date, timedelta
    primera_vez = not SEEN_FILE.exists()
    mem = cargar_memoria()
    seen = set(mem["ids"])
    hace_un_mes = (date.today() - timedelta(days=30)).isoformat()
    enviados = [k for k in mem["enviados"] if k.get("f", "") >= hace_un_mes]

    remates, errores = [], []
    for nombre, url, lector in (("rematadores.com", URL, leer_remates),
                                ("IMPO", URL_IMPO, leer_impo)):
        try:
            lista = lector(bajar(url))
            print(f"{nombre}: {len(lista)} remates leídos")
            if not lista:
                errores.append(nombre)
            remates += lista
        except Exception as ex:
            print(f"  [!] {nombre}: {ex}")
            errores.append(nombre)
    if errores:
        send_telegram("⚠️ El bot de remates no pudo leer: " + ", ".join(errores)
                      + ". Puede que la página haya cambiado.")

    elegidos = filtrar(remates)
    nuevos = 0
    if primera_vez:
        cuantos = sum(1 for r in elegidos if r["estado"] != "suspendido")
        send_telegram("✅ <b>Bot de remates activo</b>\nTe aviso de remates de casas y "
                      "terrenos en Maldonado (rematadores.com e IMPO). Estos son los publicados hoy:")
    for r in elegidos:
        if r["id"] in seen or r["estado"] == "suspendido":
            continue
        k = clave(r)
        if any(mismo_remate(k, e) for e in enviados):
            print(f"  Repetido en otra fuente, se omite: {r['id']} {r['desc'][:60]}")
            seen.add(r["id"])
            continue
        send_telegram(formatear(r))
        seen.add(r["id"])
        enviados.append(k)
        nuevos += 1
    print(f"Casas/terrenos en Maldonado: {len(elegidos)} · avisados ahora: {nuevos}")
    SEEN_FILE.write_text(json.dumps({"ids": sorted(seen), "enviados": enviados},
                                    indent=0, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(var):
            sys.exit(f"Falta la variable de entorno {var}")
    main()
