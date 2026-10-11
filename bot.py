"""
Bot de inmuebles en venta -> avisos por Telegram.

Revisa Casas y Más, Mercado Libre, InfoCasas y Gallito en los barrios/zonas
elegidos, filtra por precio y dormitorios, y manda por Telegram solo los
avisos que no vio antes. Los avisos ya vistos se guardan en seen.json.

Variables de entorno necesarias:
  TELEGRAM_TOKEN    token del bot (te lo da @BotFather)
  TELEGRAM_CHAT_ID  tu chat id (te lo da @userinfobot)
"""

import asyncio
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.yaml"
SEEN_FILE = BASE / "seen.json"

PAGINAS_EN_PARALELO = 4

SITE_NAMES = {
    "casasymas": "Casas y Más",
    "mercadolibre": "Mercado Libre",
    "infocasas": "InfoCasas",
    "gallito": "Gallito",
}

# Cómo reconocer el link de un aviso en cada portal (grupo 1 = id del aviso)
LINK_PATTERNS = {
    "casasymas": re.compile(r"casasymas\.com\.uy/propiedad/(\d+)"),
    "mercadolibre": re.compile(r"mercadolibre\.com\.uy/.*?(MLU-?\d{6,})", re.I),
    "infocasas": re.compile(r"infocasas\.com\.uy/[\w\-]+/(\d{5,})(?:[/?#]|$)"),
    "gallito": re.compile(r"gallito\.com\.uy/[\w\-]+-inmuebles-(\d+)"),
}


# ----------------------------------------------------------------------------
# Zonas
# ----------------------------------------------------------------------------
def norm(text: str) -> str:
    """minúsculas, sin tildes ni ñ, espacios simples."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower()).strip()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(text)).strip("-")


# Nombres alternativos con los que los avisos mencionan una zona elegida.
ALIAS = {
    "punta del este": [
        "peninsula", "playa mansa", "playa brava", "aidy grill", "cantegril",
        "san rafael", "pinares", "rincon del indio", "roosevelt",
        "beverly hills", "el jaguel", "lugano",
    ],
    "pocitos": ["pocitos nuevo"],
    "punta ballena": ["las grutas"],
    "laguna del sauce": ["la laguna del sauce"],
}

# Barrios/zonas conocidos de cada departamento. Si la tarjeta de un aviso
# menciona uno de estos y ninguno de los elegidos, el aviso se descarta.
CONOCIDAS = {
    "montevideo": [
        "aguada", "aires puros", "arroyo seco", "atahualpa", "banados de carrasco",
        "barra de carrasco", "barrio sur", "bella vista", "belvedere",
        "brazo oriental", "buceo", "capurro", "carrasco", "carrasco norte",
        "casabo", "casavalle", "centro", "cerrito", "cerro", "ciudad vieja",
        "colinas de carrasco", "colon", "cordon", "cordon norte", "cordon sur",
        "flor de maronas", "goes", "ituzaingo", "jacinto vera",
        "jardines del hipodromo", "la blanqueada", "la comercial", "la figurita",
        "la teja", "larranaga", "las acacias", "las canteras", "lezica", "malvin",
        "malvin norte", "manga", "maronas", "melilla", "mercado modelo",
        "nuevo paris", "palermo", "parque batlle", "parque rodo",
        "paso de la arena", "paso molino", "penarol", "piedras blancas",
        "pocitos", "pocitos nuevo", "prado", "punta carretas", "punta gorda",
        "punta rieles", "reducto", "sayago", "tres cruces", "tres ombues",
        "union", "villa biarritz", "villa dolores", "villa espanola",
        "villa garcia", "villa munoz",
    ],
    "maldonado": [
        "punta del este", "punta ballena", "lausana", "chihuahua", "portezuelo",
        "sauce de portezuelo", "solanas", "laguna del sauce", "ocean park",
        "san carlos", "piriapolis", "la barra", "manantiales", "jose ignacio",
        "el chorro", "pan de azucar", "aigua", "punta colorada", "playa hermosa",
        "playa verde", "playa grande", "las flores", "solis",
        "balneario buenos aires", "el tesoro", "la juanita", "punta piedras",
        "garzon", "punta negra", "san francisco", "cerro pelado",
        "maldonado nuevo", "centro",
    ],
}


def _contains(text_n: str, name_n: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(name_n)}(?![a-z])", text_n) is not None


def zona_ok(text: str, depto: str, elegidas: list[str]) -> bool:
    """True si la tarjeta corresponde a una zona elegida (o no dice zona)."""
    if not elegidas:
        return True
    t = norm(text)
    buscadas = set()
    for z in elegidas:
        zn = norm(z)
        buscadas.add(zn)
        buscadas.update(ALIAS.get(zn, []))
    if any(_contains(t, b) for b in buscadas):
        return True
    otras = [o for o in CONOCIDAS.get(depto, []) if o not in buscadas]
    return not any(_contains(t, o) for o in otras)


# ----------------------------------------------------------------------------
# Armado de URLs de búsqueda por portal
# ----------------------------------------------------------------------------
TIPO_SINGULAR = {"casas": "casa", "apartamentos": "apartamento"}


def build_url(site: str, tipo: str, depto: str, zona: str | None, precio_max: int) -> str:
    z = f"/{slug(zona)}" if zona else ""
    d = slug(depto)
    if site == "casasymas":
        return f"https://www.casasymas.com.uy/propiedades/venta/{TIPO_SINGULAR[tipo]}/{d}{z}"
    if site == "mercadolibre":
        return (f"https://listado.mercadolibre.com.uy/inmuebles/{tipo}/venta/{d}{z}/"
                f"_PriceRange_0USD-{precio_max}USD")
    if site == "infocasas":
        return f"https://www.infocasas.com.uy/venta/{tipo}/{d}{z}"
    if site == "gallito":
        return f"https://www.gallito.com.uy/inmuebles/{tipo}/venta/{d}{z}"
    raise ValueError(site)


def build_searches(cfg: dict) -> list[dict]:
    """Lista de búsquedas: {site, url, depto, zona}."""
    precio_max = int(cfg["filtros"]["precio_max_usd"])
    out = []
    for site in cfg.get("portales") or list(SITE_NAMES):
        if site not in LINK_PATTERNS:
            print(f"[!] Portal desconocido en config.yaml: {site}")
            continue
        for depto, zonas in (cfg.get("zonas") or {}).items():
            depto_n = norm(depto)
            for tipo in cfg.get("tipos") or ["casas", "apartamentos"]:
                for zona in (zonas or [None]):
                    out.append({
                        "site": site, "depto": depto_n, "zona": zona,
                        "url": build_url(site, tipo, depto_n, zona, precio_max),
                    })
    for site, urls in (cfg.get("busquedas_extra") or {}).items():
        for url in urls or []:
            depto = next((d for d in CONOCIDAS if d in url.lower()), "")
            out.append({"site": site, "depto": depto, "zona": None, "url": url})
    return out


# ----------------------------------------------------------------------------
# Lectura de precio y dormitorios desde el texto de la tarjeta del aviso
# ----------------------------------------------------------------------------
_NUM = r"(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:[.,]\d+)?)\s*(mil|k)?"
USD_RE = re.compile(r"(?:U\$S|US\$|U\$D|USD|U\$|u\$s|US\s?\$)\s*" + _NUM, re.I)
UYU_RE = re.compile(r"(?<![A-Za-z$])(?:\$U|UYU|\$)\s*" + _NUM)
DORM_RES = [
    re.compile(r"(\d+)\s*(?:dormitorios?|dorms?\b\.?)", re.I),
    re.compile(r"dormitorios?\s*:?\s*(\d+)", re.I),
    re.compile(r"(\d+)\s*(?:habitaciones|hab\.)", re.I),
]


def _to_number(raw: str, suffix: str | None) -> float:
    raw = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:[.,\s]\d{3})+", raw):
        value = float(re.sub(r"[.,\s]", "", raw))
    else:
        value = float(raw.replace(",", "."))
    if suffix and suffix.lower() in ("mil", "k"):
        value *= 1000
    return value


def parse_price_usd(text: str, cotizacion: float) -> float | None:
    """Precio de venta en USD. Si hay varios montos (ej. venta y alquiler),
    toma el mayor, que es el de venta."""
    usd = [_to_number(m.group(1), m.group(2)) for m in USD_RE.finditer(text)]
    usd = [v for v in usd if v >= 1000]
    if usd:
        return max(usd)
    uyu = [_to_number(m.group(1), m.group(2)) for m in UYU_RE.finditer(text)]
    uyu = [v for v in uyu if v >= 100000]  # descarta gastos comunes, etc.
    if uyu:
        return max(uyu) / cotizacion
    return None


def parse_dorms(text: str) -> int | None:
    if re.search(r"mono\s?ambiente", text, re.I):
        return 0
    for rx in DORM_RES:
        m = rx.search(text)
        if m:
            n = int(m.group(1))
            if n < 30:
                return n
    return None


def passes_filters(item: dict, f: dict) -> bool:
    price = item.get("precio_usd")
    if price is not None and price > f["precio_max_usd"]:
        return False
    dorms = item.get("dormitorios")
    if dorms is None:
        return f.get("avisar_si_no_indica_dormitorios", True)
    return dorms >= f["dormitorios_min"]


# ----------------------------------------------------------------------------
# Scraping con navegador (las páginas cargan avisos con JavaScript)
# ----------------------------------------------------------------------------
EXTRACT_JS = r"""
(pattern) => {
  const rx = new RegExp(pattern, 'i');
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!rx.test(href)) continue;
    // Subir hasta la "tarjeta" del aviso para leer precio, dormitorios, zona.
    let card = a, best = a;
    for (let i = 0; i < 7 && card.parentElement; i++) {
      card = card.parentElement;
      const t = (card.innerText || '').trim();
      if (t.length > 2000) break;
      best = card;
      if (/(U\$S|US\$|USD|\$)/.test(t) && t.length > 60) break;
    }
    const h = best.querySelector('h2, h3, h4');
    const title = (h && h.innerText) || a.title || a.innerText || '';
    out.push({ href, title: title.trim(), text: (best.innerText || '').trim() });
  }
  return out;
}
"""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


async def _scrape_one(ctx, sem, search: dict) -> list[dict]:
    site = search["site"]
    pattern = LINK_PATTERNS[site]
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(search["url"], wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            for _ in range(4):  # scroll para cargar avisos perezosos
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(500)
            raw = await page.evaluate(EXTRACT_JS, pattern.pattern)
        except Exception as e:
            print(f"  [!] {SITE_NAMES[site]}: no se pudo leer {search['url']}: {e}")
            return []
        finally:
            await page.close()
    items = []
    for r in raw:
        m = pattern.search(r["href"])
        if m:
            items.append({
                "id": f"{site}:{m.group(1).upper().replace('-', '')}",
                "sitio": site,
                "url": r["href"].split("#")[0],
                "titulo": r["title"][:150],
                "text": r["text"],
                "depto": search["depto"],
                "zona_busqueda": search["zona"],
            })
    print(f"  {SITE_NAMES[site]}: {len(items)} avisos en {search['url']}")
    return items


async def _scrape_all(searches: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="es-UY", user_agent=UA, viewport={"width": 1366, "height": 900}
        )
        sem = asyncio.Semaphore(PAGINAS_EN_PARALELO)
        batches = await asyncio.gather(*(_scrape_one(ctx, sem, s) for s in searches))
        await browser.close()
    return [it for b in batches for it in b]


def scrape(searches: list[dict]) -> list[dict]:
    """Devuelve los avisos encontrados, sin duplicados."""
    unicos: dict[str, dict] = {}
    for it in asyncio.run(_scrape_all(searches)):
        prev = unicos.get(it["id"])
        if prev is None or len(it["text"]) > len(prev["text"]):
            unicos[it["id"]] = it
    return list(unicos.values())


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=30,
    )
    if r.status_code == 429:  # demasiados mensajes: esperar y reintentar
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        return send_telegram(text)
    r.raise_for_status()
    time.sleep(1.1)


def format_item(item: dict) -> str:
    e = html.escape
    precio = (
        f"US$ {item['precio_usd']:,.0f}".replace(",", ".")
        if item.get("precio_usd") else "Precio no indicado"
    )
    dorms = item.get("dormitorios")
    dorm_txt = (
        "Monoambiente" if dorms == 0
        else f"{dorms} dormitorios" if dorms is not None
        else "Dormitorios no indicados"
    )
    lugar = ", ".join(x for x in (item.get("zona_busqueda"),
                                  (item.get("depto") or "").title()) if x)
    titulo = item.get("titulo") or "Nuevo inmueble"
    return (
        f"🏠 <b>{e(titulo)}</b>\n"
        f"💰 {e(precio)}  ·  🛏 {e(dorm_txt)}\n"
        + (f"📍 {e(lugar)}\n" if lugar else "")
        + f"🌐 {e(SITE_NAMES[item['sitio']])}\n"
        f"<a href=\"{e(item['url'], quote=True)}\">Ver aviso</a>"
    )


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main() -> None:
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    f = cfg["filtros"]
    zonas = {norm(d): z or [] for d, z in (cfg.get("zonas") or {}).items()}
    first_run = not SEEN_FILE.exists()
    seen: set[str] = set() if first_run else set(json.loads(SEEN_FILE.read_text()))

    searches = build_searches(cfg)
    print(f"Revisando {len(searches)} búsquedas...")
    items = scrape(searches)

    nuevos: list[dict] = []
    for it in items:
        if it["id"] in seen:
            continue
        it["precio_usd"] = parse_price_usd(it["text"], f["cotizacion_usd_uyu"])
        it["dormitorios"] = parse_dorms(it["text"])
        if passes_filters(it, f) and zona_ok(it["text"], it["depto"], zonas.get(it["depto"], [])):
            nuevos.append(it)
        else:
            seen.add(it["id"])

    por_sitio = {SITE_NAMES[s]: sum(1 for i in items if i["sitio"] == s)
                 for s in {x["site"] for x in searches}}
    print(f"Avisos leídos: {len(items)} {por_sitio} · nuevos que cumplen filtros: {len(nuevos)}")

    if first_run:
        # Primera vez: registra lo que ya existe sin inundarte de mensajes.
        seen.update(n["id"] for n in nuevos)
        detalle = "\n".join(f"• {html.escape(k)}: {v} avisos leídos" for k, v in por_sitio.items())
        send_telegram(
            "✅ <b>Bot de inmuebles activo</b>\n"
            f"Registré {len(nuevos)} avisos que ya cumplen tus filtros.\n"
            f"{detalle}\n"
            "Desde ahora te aviso solo de los nuevos."
        )
    else:
        limite = cfg.get("max_mensajes_por_ejecucion", 25)
        for it in nuevos[:limite]:
            send_telegram(format_item(it))
            seen.add(it["id"])
        if len(nuevos) > limite:
            send_telegram(
                f"➕ Hay {len(nuevos) - limite} avisos nuevos más; "
                "te los mando en la próxima revisión."
            )

    if not items:
        send_telegram(
            "⚠️ El bot no pudo leer ningún aviso en esta revisión. "
            "Puede que algún portal haya cambiado o bloqueado el acceso."
        )

    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")


if __name__ == "__main__":
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(var):
            sys.exit(f"Falta la variable de entorno {var}")
    main()
