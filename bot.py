"""
Bot de inmuebles en venta -> avisos por Telegram.

Revisa Casas y Más, InfoCasas, Gallito, RE/MAX y Punta Ballena Inmobiliaria
en los barrios/zonas elegidos, filtra por precio, tipo, dormitorios y garage,
y manda por Telegram solo los avisos que no vio antes (sin repetir la misma
propiedad publicada en varios portales). Los avisos ya vistos se guardan en seen.json.

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


def tipos_de(cfg: dict, depto: str) -> list[str]:
    """Tipos a buscar en un departamento (tipos_por_depto, o tipos general)."""
    por = {norm(k): v for k, v in (cfg.get("tipos_por_depto") or {}).items()}
    return por.get(norm(depto)) or cfg.get("tipos") or ["casas", "apartamentos"]


def detectar_tipo(item: dict) -> str | None:
    """casas / apartamentos según el link y el título del aviso; si no lo
    dicen, el tipo de la búsqueda donde apareció."""
    from urllib.parse import urlparse
    for fuente in (norm(urlparse(item.get("url", "")).path.replace("-", " ")),
                   norm(item.get("titulo") or "")):
        if re.search(r"\b(apartamentos?|aptos?|departamentos?|monoambientes?|penthouse|duplex)\b", fuente):
            return "apartamentos"
        if re.search(r"\b(casas?|chalets?)\b", fuente):
            return "casas"
    return item.get("tipo_busqueda")


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
            for tipo in tipos_de(cfg, depto):
                out.append({"site": site, "depto": depto, "zona": None, "estricto": True,
                            "tipo": tipo,
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
                for tipo in tipos_de(cfg, inm["depto"]):
                    out.append({"site": site, "depto": inm["depto"], "zona": None,
                                "tipo": tipo,
                                "url": inm["url"].format(tipo=tipo, precio=precio_max),
                                "paginas": True})
            continue
        for depto, zonas in (cfg.get("zonas") or {}).items():
            depto_n = norm(depto)
            for tipo in tipos_de(cfg, depto_n):
                for zona in (zonas or [None]):
                    out.append({
                        "site": site, "depto": depto_n, "zona": zona, "tipo": tipo,
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
# "2 dormitorios", "3 dorms." (número antes, misma línea)
DORM_ANTES = re.compile(r"(\d{1,2})[ \t]*(?:dormitorios?|dorms?)\b", re.I)
# "Dormitorios: 2", "Dormitorios\n2", "Dormitorios2" (etiqueta y valor)
DORM_ETIQUETA = re.compile(r"dormitorios?(?:[ \t]*[:\n][ \t]*|)(\d{1,2})\b", re.I)
# "Dormitorios 2" (etiqueta, espacio y número): ambiguo con "2 dormitorios 1 baño"
DORM_ESPACIO = re.compile(r"dormitorios?[ \t]+(\d{1,2})\b(?![ \t]*[^\W\d_])", re.I)
HAB_ANTES = re.compile(r"(\d{1,2})[ \t]*(?:habitaciones|hab\.)", re.I)
# En la dirección del aviso: ".../venta-apartamento-pocitos-2-dormitorios-..."
DORM_URL = re.compile(r"(?<![\d])(\d{1,2})-dorm(?:itorios?|s)?\b", re.I)
# En letras: "dos dormitorios", "un dormitorio"
NUM_LETRAS = {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
              "cinco": 5, "seis": 6}
DORM_LETRAS = re.compile(r"\b(un|uno|una|dos|tres|cuatro|cinco|seis)[ \t]+dormitorios?\b", re.I)
# Fichas con el número ARRIBA de la palabra ("2\ndormitorios\n1\nbaños") o
# ABAJO ("Dormitorios\n2\nBaños\n1"): se reconoce mirando también los baños.
DORM_ARRIBA = re.compile(r"(?:^|\n)[ \t]*(\d{1,2})[ \t]*\n[ \t]*dormitorios?[ \t]*\n[ \t]*\d{1,2}[ \t]*\n[ \t]*baños?\b", re.I)
DORM_ABAJO = re.compile(r"dormitorios?[ \t]*\n[ \t]*(\d{1,2})[ \t]*\n[ \t]*baños?[ \t]*\n[ \t]*\d{1,2}[ \t]*(?:\n|$)", re.I)


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


def _dorms_de_texto_corto(t: str) -> int | None:
    """Título o frase: "2 dormitorios", "dos dormitorios", "monoambiente"."""
    m = DORM_ANTES.search(t)
    if m:
        return int(m.group(1))
    m = DORM_LETRAS.search(t)
    if m:
        return NUM_LETRAS[m.group(1).lower()]
    if re.search(r"mono\s?ambiente", t, re.I):
        return 0
    return None


def parse_dorms(text: str, titulo: str = "", url: str = "") -> int | None:
    """Dormitorios del aviso. None = no lo dice o es ambiguo (se mira la ficha)."""
    # 1) La dirección del aviso (Gallito, Casas y Más la incluyen)
    if url:
        from urllib.parse import urlparse
        ruta = urlparse(url).path
        m = DORM_URL.search(ruta)
        if m:
            return int(m.group(1))
        if re.search(r"mono-?ambiente", ruta, re.I):
            return 0
    # 2) El título
    if titulo:
        d = _dorms_de_texto_corto(titulo)
        if d is not None:
            return d
    # 3) El texto: primero lo que está en una misma línea ("3 Dorms.", "2 dormitorios")
    antes = {int(x) for x in DORM_ANTES.findall(text)}
    letras = {NUM_LETRAS[x.lower()] for x in DORM_LETRAS.findall(text)}
    pegado = {int(x) for x in re.findall(r"dormitorios?(\d{1,2})(?!\d)", text, re.I)}  # "Dormitorios3"
    espacio = {int(x) for x in DORM_ESPACIO.findall(text)}                         # "Dormitorios 3"
    misma_linea = antes | letras | pegado | espacio
    if len(misma_linea) == 1:
        return misma_linea.pop()
    # 4) Fichas en columnas: número arriba o abajo de la palabra
    arriba = {int(x) for x in DORM_ARRIBA.findall(text)}
    abajo = {int(x) for x in DORM_ABAJO.findall(text)}
    if arriba and not abajo and len(arriba) == 1:
        return arriba.pop()
    if abajo and not arriba and len(abajo) == 1:
        return abajo.pop()
    m = re.search(r"dormitorios?[ \t]*:[ \t]*(\d{1,2})\b", text, re.I)   # "Dormitorios: 2"
    if m:
        return int(m.group(1))
    if misma_linea or arriba or abajo:
        return None                       # datos que no coinciden: ambiguo
    if re.search(r"mono\s?ambiente", text, re.I):
        return 0
    m = HAB_ANTES.search(text)
    return int(m.group(1)) if m else None


def passes_filters(item: dict, f: dict) -> bool:
    price = item.get("precio_usd")
    if price is not None and price > f["precio_max_usd"]:
        return False
    dorms = item.get("dormitorios")
    if dorms is None:
        return True        # se decide después de mirar la ficha
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
                "tipo_busqueda": search.get("tipo"),
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
  const texto = ((m && m.innerText.length > 300 ? m.innerText : document.body.innerText) || '').slice(0, 30000);
  const h1 = document.querySelector('h1');
  return { texto, titulo: ((h1 && h1.innerText) || '') + ' | ' + (document.title || '') };
}
"""


async def _ficha(ctx, sem, item: dict) -> None:
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(item["url"], wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            r = await page.evaluate(FICHA_JS)
            item["ficha_texto"], item["ficha_titulo"] = r["texto"], r["titulo"]
        except Exception as e:
            print(f"  [!] No se pudo abrir la ficha {item['url']}: {e}")
            item["ficha_error"] = True
        finally:
            await page.close()


async def _abrir_fichas(items: list[dict]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="es-UY", user_agent=UA, viewport={"width": 1366, "height": 900})
        sem = asyncio.Semaphore(PAGINAS_EN_PARALELO)
        await asyncio.gather(*(_ficha(ctx, sem, it) for it in items))
        await browser.close()


def revisar_fichas(items: list[dict], con_garage: set[str]) -> None:
    """Abre la ficha de los avisos a los que les falta un dato:
    dormitorios (si la tarjeta no lo dice claro) y garage (en los
    departamentos donde es obligatorio). Completa item['dormitorios'] e
    item['garage'] (True / False / None)."""
    pendientes = []
    for it in items:
        if it["depto"] in con_garage:
            it["garage"] = True if detectar_garage(it["text"]) is True else None
        falta_garage = it["depto"] in con_garage and it["garage"] is not True
        if it.get("dormitorios") is None or falta_garage:
            pendientes.append(it)
    if not pendientes:
        return
    print(f"Revisando {len(pendientes)} fichas...")
    asyncio.run(_abrir_fichas(pendientes))
    for it in pendientes:
        if it.get("ficha_error"):
            continue
        if it.get("dormitorios") is None:
            it["dormitorios"] = parse_dorms(it["ficha_texto"], it["ficha_titulo"], it.get("url", ""))
        if it["depto"] in con_garage and it["garage"] is not True:
            it["garage"] = detectar_garage(it["ficha_texto"])


# ----------------------------------------------------------------------------
# Avisos repetidos en distintos portales
# ----------------------------------------------------------------------------
DIAS_MEMORIA_REPETIDOS = 60


def huella(item: dict) -> str | None:
    """Misma propiedad publicada en varios portales: mismo departamento, tipo,
    precio (en miles) y dormitorios; en Montevideo además el mismo barrio."""
    precio, dorms = item.get("precio_usd"), item.get("dormitorios")
    if not precio or dorms is None:
        return None
    partes = [item["depto"], item.get("tipo") or "", str(round(precio / 1000)), str(dorms)]
    if item["depto"] == "montevideo":
        partes.append(norm(item.get("zona_busqueda") or ""))
    return "|".join(partes)


def leer_huellas(seen: set[str]) -> dict[str, str]:
    """En seen.json las huellas se guardan como 'fp:<huella>@<fecha>'."""
    from datetime import date, timedelta
    limite = (date.today() - timedelta(days=DIAS_MEMORIA_REPETIDOS)).isoformat()
    huellas = {}
    for x in [x for x in seen if x.startswith("fp:")]:
        h, _, fecha = x[3:].rpartition("@")
        if fecha >= limite:
            huellas[h] = fecha
        else:
            seen.discard(x)            # olvida huellas viejas
    return huellas


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
def main() -> int:
    """Revisa los portales y avisa. Devuelve cuántos avisos mandó."""
    from datetime import date
    enviados = 0
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    f = cfg["filtros"]
    zonas = {norm(d): z or [] for d, z in (cfg.get("zonas") or {}).items()}
    first_run = not SEEN_FILE.exists()
    seen: set[str] = set() if first_run else set(json.loads(SEEN_FILE.read_text()))
    huellas = leer_huellas(seen)
    hoy = date.today().isoformat()

    searches = build_searches(cfg)
    # Portales que todavía no tienen nada en la memoria = recién agregados.
    sin_memoria = set() if first_run else {
        s["site"] for s in searches if not any(x.startswith(s["site"] + ":") for x in seen)}
    print(f"Revisando {len(searches)} búsquedas...")
    items = scrape(searches)

    # Si se subió el presupuesto, los avisos ya vistos que antes quedaban
    # afuera por precio se vuelven a evaluar (una sola vez cada uno).
    anterior = f.get("precio_max_anterior_usd")

    def reconsiderar(it) -> bool:
        if not anterior or f"re:{it['id']}" in seen:
            return False
        p = parse_price_usd(it["text"], f["cotizacion_usd_uyu"])
        return p is not None and anterior < p <= f["precio_max_usd"]

    def descartar(it) -> None:
        seen.add(it["id"])
        if it.get("reconsiderado"):
            seen.add(f"re:{it['id']}")

    nuevos: list[dict] = []
    for it in items:
        if it["id"] in seen:
            if not reconsiderar(it):
                continue
            it["reconsiderado"] = True
        it["precio_usd"] = parse_price_usd(it["text"], f["cotizacion_usd_uyu"])
        it["dormitorios"] = parse_dorms(it["text"], it.get("titulo", ""), it.get("url", ""))
        if not it["depto"]:
            it["depto"] = detectar_depto(it["text"], list(zonas))
        it["tipo"] = detectar_tipo(it)
        en_zona = bool(it["depto"]) and zona_ok(
            it["text"], it["depto"], zonas.get(it["depto"], []), it.get("estricto", False))
        tipo_ok = bool(it["depto"]) and it["tipo"] in tipos_de(cfg, it["depto"])
        if en_zona and not it.get("zona_busqueda"):
            t = norm(it["text"])
            it["zona_busqueda"] = next(
                (z for z in zonas.get(it["depto"], [])
                 if any(_contains(t, a) for a in [norm(z), *ALIAS.get(norm(z), [])])), None)
        if passes_filters(it, f) and en_zona and tipo_ok:
            nuevos.append(it)
        else:
            descartar(it)

    por_sitio = {SITE_NAMES[s]: sum(1 for i in items if i["sitio"] == s)
                 for s in {x["site"] for x in searches}}
    print(f"Avisos leídos: {len(items)} {por_sitio} · candidatos: {len(nuevos)}")

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

        # Fichas: dormitorios que la tarjeta no aclara y garage obligatorio.
        con_garage = {norm(d) for d in (f.get("garage_obligatorio_en") or [])}
        revisar_fichas(nuevos, con_garage)
        sin_dorms_ok = f.get("avisar_si_no_indica_dormitorios", False)
        sin_garage_ok = f.get("avisar_si_no_indica_garage", False)
        finales = []
        for n in nuevos:
            if n.get("ficha_error"):
                continue                   # la ficha no abrió: se reintenta luego
            d = n.get("dormitorios")
            if (d is None and not sin_dorms_ok) or (d is not None and d < f["dormitorios_min"]):
                descartar(n)
                continue
            if n["depto"] in con_garage:
                g = n.get("garage")
                if g is False or (g is None and not sin_garage_ok):
                    descartar(n)
                    continue
            finales.append(n)
        print(f"Tras revisar fichas: {len(finales)} de {len(nuevos)}")

        # Repetidos: la misma propiedad publicada en otro portal.
        limite = cfg.get("max_mensajes_por_ejecucion", 25)
        enviados = repetidos = 0
        pendientes = 0
        for it in finales:
            h = huella(it)
            if h and h in huellas:
                repetidos += 1
                descartar(it)
                continue
            if enviados >= limite:
                pendientes += 1
                continue
            send_telegram(format_item(it))
            enviados += 1
            descartar(it)                 # queda registrado como visto
            if h:
                huellas[h] = hoy
        print(f"Enviados: {enviados} · repetidos omitidos: {repetidos}")
        if pendientes:
            send_telegram(f"➕ Hay {pendientes} avisos nuevos más; "
                          "te los mando en la próxima revisión.")

    if not items:
        send_telegram(
            "⚠️ El bot no pudo leer ningún aviso en esta revisión. "
            "Puede que algún portal haya cambiado o bloqueado el acceso."
        )

    seen = {x for x in seen if not x.startswith("fp:")}
    seen.update(f"fp:{h}@{d}" for h, d in huellas.items())
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")
    return enviados


if __name__ == "__main__":
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(var):
            sys.exit(f"Falta la variable de entorno {var}")
    main()
