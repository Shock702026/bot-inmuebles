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
    "remax": "RE/MAX",
    "pbi": "Punta Ballena Inmobiliaria",
}

# Cómo reconocer el link de un aviso en cada portal (grupo 1 = id del aviso)
LINK_PATTERNS = {
    "casasymas": re.compile(r"casasymas\.com\.uy/propiedad/(\d+)"),
    "mercadolibre": re.compile(r"mercadolibre\.com\.uy/.*?(MLU-?\d{6,})", re.I),
    "infocasas": re.compile(r"infocasas\.com\.uy/[\w\-]+/(\d{5,})(?:[/?#]|$)"),
    "gallito": re.compile(r"gallito\.com\.uy/[\w\-]+-inmuebles-(\d+)"),
    "remax": re.compile(r"remax\.com\.uy/listings/(?!buy\b|rent\b)([a-z0-9][a-z0-9\-]{8,})", re.I),
}

# Portales que solo tienen páginas por ciudad (no por barrio). En estos el
# aviso tiene que nombrar uno de los barrios elegidos para que te llegue.
SOLO_CIUDAD = {"remax"}

# Inmobiliarias con una sola página de ventas (se recorren todas sus páginas).
# El departamento indica dónde están sus propiedades, para el filtro de zonas.
INMOBILIARIAS = {
    "pbi": {
        "depto": "maldonado",
        # {tipo} = casas / apartamentos ; {precio} = precio máximo en USD
        "url": "https://puntaballenainmobiliaria.com/{tipo}-venta/"
               "?periodo=0&precioMaximo={precio}&pagina=1",
    },
}
LINK_PATTERNS["pbi"] = re.compile(
    r"(?:pbiinmobiliaria|puntaballenainmobiliaria)\.com(?::\d+)?/(?:Casa|Apartamento)/(\d+)", re.I)


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
    "punta ballena": ["las grutas", "lomo de la ballena", "club del lago",
                      "tio tom", "rinconada", "sierra de la ballena",
                      "camino lussich", "lussich"],
    "laguna del sauce": ["la laguna del sauce"],
    "solanas": ["solana"],
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


def zona_ok(text: str, depto: str, elegidas: list[str], estricto: bool = False) -> bool:
    """True si la tarjeta corresponde a una zona elegida.
    estricto=False: también acepta tarjetas que no nombran ningún barrio
    (la búsqueda ya era de ese barrio). estricto=True: exige nombrarlo."""
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
    if estricto:
        return False
    otras = [o for o in CONOCIDAS.get(depto, []) if o not in buscadas]
    return not any(_contains(t, o) for o in otras)


def detectar_depto(text: str, deptos: list[str]) -> str:
    """Para búsquedas de todo el país: deduce el departamento del aviso."""
    t = norm(text)
    for d in deptos:
        if _contains(t, d) or (d == "maldonado" and _contains(t, "punta del este")):
            return d
    return ""


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
    if site == "remax":
        return f"https://www.remax.com.uy/{tipo}-en-venta-en-{d}"
    raise ValueError(site)


def _ciudad_searches(site: str, cfg: dict, precio_max: int) -> list[dict]:
    """Búsquedas para portales sin páginas por barrio (RE/MAX)."""
    out = []
    deptos = [norm(d) for d in (cfg.get("zonas") or {})]
    for depto in deptos:
        ciudades = [depto]
        zonas_n = [norm(z) for z in (cfg["zonas"].get(depto) or cfg["zonas"].get(depto.title()) or [])]
        if depto == "maldonado" and (not zonas_n or "punta del este" in zonas_n):
            ciudades.append("punta del este")
        for ciudad in ciudades:
            for tipo in cfg.get("tipos") or ["casas", "apartamentos"]:
                out.append({"site": site, "depto": depto, "zona": None, "estricto": True,
                            "url": build_url(site, tipo, ciudad, None, precio_max)})
    if site == "remax":
        # Las 100 publicaciones de venta más recientes de todo el país.
        out.append({"site": site, "depto": "", "zona": None, "estricto": True,
                    "url": "https://www.remax.com.uy/listings/buy?page=0&pageSize=100"
                           "&sort=-id&in:operationId=1"})
    return out


def build_searches(cfg: dict) -> list[dict]:
    """Lista de búsquedas: {site, url, depto, zona}."""
    precio_max = int(cfg["filtros"]["precio_max_usd"])
    out = []
    for site in cfg.get("portales") or list(SITE_NAMES):
        if site not in LINK_PATTERNS:
            print(f"[!] Portal desconocido en config.yaml: {site}")
            continue
        if site in SOLO_CIUDAD:
            out.extend(_ciudad_searches(site, cfg, precio_max))
            continue
        if site in INMOBILIARIAS:
            inm = INMOBILIARIAS[site]
            if inm["depto"] in [norm(d) for d in (cfg.get("zonas") or {})]:
                for tipo in cfg.get("tipos") or ["casas", "apartamentos"]:
                    out.append({"site": site, "depto": inm["depto"], "zona": None,
                                "url": inm["url"].format(tipo=tipo, precio=precio_max),
                                "paginas": True})
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
    // La tarjeta termina antes de abarcar el link de OTRO aviso.
    const id = (href.match(rx) || [])[1];
    let card = a, best = a;
    for (let i = 0; i < 7 && card.parentElement; i++) {
      card = card.parentElement;
      const t = (card.innerText || '').trim();
      if (t.length > 2000) break;
      const otro = [...card.querySelectorAll('a[href]')].some(x => {
        const m = x.href.match(rx);
        return m && m[1] !== id;
      });
      if (otro) break;
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

MAX_PAGINAS = 20

# Links a las páginas siguientes de un listado (2, 3, ..., "Siguiente").
PAGINAS_JS = r"""
(base) => {
  const b = new URL(base);
  const sinBarra = p => p.toLowerCase().replace(/\/+$/, '');
  const out = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    let u;
    try { u = new URL(a.href); } catch (e) { continue; }
    // (el host puede variar: el sitio usa dos dominios)
    if (!sinBarra(u.pathname).startsWith(sinBarra(b.pathname))) continue;
    const txt = (a.innerText || '').trim().toLowerCase();
    const pag = /(pagina|pag|page|p)[=\/_-]?\d+/i.test(u.pathname + u.search);
    if (pag || /^\d{1,3}$/.test(txt) || /siguiente|next|›|»/.test(txt)) {
      u.hash = '';
      if (u.href !== b.href) out.add(u.href);
    }
  }
  return [...out];
}
"""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


async def _scrape_one(ctx, sem, search: dict) -> list[dict]:
    site = search["site"]
    pattern = LINK_PATTERNS[site]
    async with sem:
        page = await ctx.new_page()
        raw = []
        pendientes, visitadas = [search["url"]], set()
        try:
            while pendientes and len(visitadas) < MAX_PAGINAS:
                url = pendientes.pop(0)
                if url in visitadas:
                    continue
                visitadas.add(url)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                for _ in range(4):  # scroll para cargar avisos perezosos
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(500)
                raw += await page.evaluate(EXTRACT_JS, pattern.pattern)
                if search.get("paginas"):
                    for u in await page.evaluate(PAGINAS_JS, search["url"]):
                        if u not in visitadas and u not in pendientes:
                            pendientes.append(u)
        except Exception as e:
            print(f"  [!] {SITE_NAMES[site]}: no se pudo leer {search['url']}: {e}")
            if not raw:
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
                "estricto": search.get("estricto", False),
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
# Garage / cochera (se mira la ficha de cada aviso nuevo)
# ----------------------------------------------------------------------------
_G = r"(?:garages?|garajes?|cocheras?|estacionamientos?|parkings?)"
GARAGE_NO = [
    re.compile(rf"\bsin {_G}"),
    re.compile(rf"\bno (?:tiene|posee|cuenta con|incluye) {_G}"),
    re.compile(rf"\b{_G}\s*:?\s*(?:no\b|0\b|-)"),
]
GARAGE_SI = [
    re.compile(rf"\b([1-9])\s*{_G}"),                       # "1 garage", "2 cocheras"
    re.compile(rf"\b{_G}\s*:?\s*(?:[1-9]\b|si\b|[1-9](?!\d))"),  # "Garage: 1", "Garage1", "Cochera: Sí"
    re.compile(rf"\bcon {_G}"),                              # "con garage"
    re.compile(rf"\b{_G} (?:propio|cerrado|techado|cubierto|privado|doble|"
               r"para \w+|incluido|individual|subterraneo|en (?:el )?edificio)"),
]


def detectar_garage(texto: str) -> bool | None:
    """True = tiene garage/cochera, False = dice que no, None = no lo menciona."""
    t = norm(texto)
    if any(r.search(t) for r in GARAGE_SI):
        return True
    if any(r.search(t) for r in GARAGE_NO):
        return False
    return None


FICHA_JS = r"""
() => {
  const m = document.querySelector('main, article, [class*=detail], [class*=Detail], [class*=propiedad]');
  return ((m && m.innerText.length > 300 ? m.innerText : document.body.innerText) || '').slice(0, 30000);
}
"""


async def _ficha_garage(ctx, sem, item: dict) -> None:
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(item["url"], wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            item["garage"] = detectar_garage(await page.evaluate(FICHA_JS))
        except Exception as e:
            print(f"  [!] No se pudo abrir la ficha {item['url']}: {e}")
            item["garage"] = "error"
        finally:
            await page.close()


async def _revisar_garages(items: list[dict]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="es-UY", user_agent=UA, viewport={"width": 1366, "height": 900})
        sem = asyncio.Semaphore(PAGINAS_EN_PARALELO)
        await asyncio.gather(*(_ficha_garage(ctx, sem, it) for it in items))
        await browser.close()


def revisar_garages(items: list[dict]) -> None:
    """Completa item['garage'] (True/False/None/'error') de cada aviso."""
    pendientes = []
    for it in items:
        if detectar_garage(it["text"]) is True:   # la tarjeta ya lo dice
            it["garage"] = True
        else:
            pendientes.append(it)
    if pendientes:
        print(f"Revisando garage en {len(pendientes)} fichas...")
        asyncio.run(_revisar_garages(pendientes))


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def chat_ids() -> list[str]:
    """TELEGRAM_CHAT_ID admite varios Id separados por coma: 123,456"""
    return [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]


def _send_one(token: str, chat_id: str, text: str) -> None:
    for _ in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=30,
        )
        if r.status_code == 429:  # demasiados mensajes: esperar y reintentar
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print(f"  [!] No se pudo enviar a {chat_id}: {r.status_code} {r.text[:200]}")
        return


def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    for cid in chat_ids():
        _send_one(token, cid, text)
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
        + ("🚗 Con garage\n" if item.get("garage") is True
           else "🚗 Garage no indicado\n" if "garage" in item and item["garage"] is None
           else "")
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
    # Portales que todavía no tienen nada en la memoria = recién agregados.
    sin_memoria = set() if first_run else {
        s["site"] for s in searches if not any(x.startswith(s["site"] + ":") for x in seen)}
    print(f"Revisando {len(searches)} búsquedas...")
    items = scrape(searches)

    nuevos: list[dict] = []
    for it in items:
        if it["id"] in seen:
            continue
        it["precio_usd"] = parse_price_usd(it["text"], f["cotizacion_usd_uyu"])
        it["dormitorios"] = parse_dorms(it["text"])
        if not it["depto"]:
            it["depto"] = detectar_depto(it["text"], list(zonas))
        en_zona = bool(it["depto"]) and zona_ok(
            it["text"], it["depto"], zonas.get(it["depto"], []), it.get("estricto", False))
        if en_zona and not it.get("zona_busqueda"):
            t = norm(it["text"])
            it["zona_busqueda"] = next(
                (z for z in zonas.get(it["depto"], [])
                 if any(_contains(t, a) for a in [norm(z), *ALIAS.get(norm(z), [])])), None)
        if passes_filters(it, f) and en_zona:
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
        # Portal recién agregado: registra sus avisos actuales sin mandarlos.
        sitios_nuevos = sin_memoria & {i["sitio"] for i in items}
        for s in sorted(sitios_nuevos):
            de_s = [n for n in nuevos if n["sitio"] == s]
            seen.update(n["id"] for n in de_s)
            seen.update(i["id"] for i in items if i["sitio"] == s)
            send_telegram(f"➕ Sumé <b>{html.escape(SITE_NAMES[s])}</b>: registré "
                          f"{len(de_s)} avisos que ya cumplen tus filtros. "
                          "Desde ahora te aviso de los nuevos.")
        nuevos = [n for n in nuevos if n["sitio"] not in sitios_nuevos]

        # Garage obligatorio en los departamentos indicados en config.yaml.
        con_garage = {norm(d) for d in (f.get("garage_obligatorio_en") or [])}
        revisar = [n for n in nuevos if n["depto"] in con_garage]
        if revisar:
            revisar_garages(revisar)
            si_garage_sin_dato = f.get("avisar_si_no_indica_garage", False)
            antes = len(nuevos)
            for n in revisar:
                g = n.get("garage")
                if g is True or (g is None and si_garage_sin_dato):
                    continue
                nuevos.remove(n)
                if g != "error":          # si la ficha no abrió, se reintenta luego
                    seen.add(n["id"])
            print(f"Garage: {antes - len(nuevos)} descartados de {len(revisar)} revisados")

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
