# 🏠 Bot de inmuebles → Telegram

Revisa **Casas y Más, Mercado Libre, InfoCasas y Gallito** dos veces por día
(9:05 y 19:05 hora de Uruguay) y te manda por Telegram cada casa o apartamento
**nuevo** en venta, hasta **USD 200.000** y con **2 dormitorios o más**, en:

- **Montevideo:** Parque Rodó, Cordón Sur, Barrio Sur, Palermo, Prado,
  Atahualpa, Reducto, Pocitos y Punta Carretas.
- **Maldonado:** Punta del Este, Punta Ballena, Lausana, Chihuahua,
  Portezuelo, Sauce de Portezuelo, Solanas, Laguna del Sauce y Ocean Park.

Corre gratis en GitHub Actions: no necesitás tener la computadora prendida.

---

## Configuración (unos 10 minutos)

### 1. Crear tu bot de Telegram
1. En Telegram, buscá **@BotFather** y mandale `/newbot`.
2. Elegí un nombre y un usuario (tiene que terminar en `bot`).
3. BotFather te da un **token** (algo como `123456:ABC-xyz...`). Guardalo.
4. Abrí el chat con tu bot nuevo y mandale cualquier mensaje (por ejemplo "hola").
   Sin este paso el bot no puede escribirte.

### 2. Conseguir tu Chat ID
1. Buscá **@userinfobot** en Telegram y mandale cualquier mensaje.
2. Te responde con tu **Id** (un número). Ese es tu Chat ID.

### 3. Subir el bot a GitHub
1. Creá una cuenta en [github.com](https://github.com) si no tenés.
2. Creá un repositorio nuevo, por ejemplo `bot-inmuebles`.
   - **Público (recomendado):** GitHub Actions es ilimitado. Tu token y tu chat
     ID igual quedan ocultos como secretos; lo único visible es el código y
     los barrios que buscás.
   - **Privado:** tenés 2.000 minutos gratis por mes. Cada revisión tarda
     unos 10–15 minutos, así que dos por día pueden quedar justo en el límite.
3. Subí todos los archivos de esta carpeta, incluida la carpeta
   `.github/workflows` (botón **Add file → Upload files**; arrastrá la carpeta
   completa).

### 4. Cargar los datos secretos
En el repositorio: **Settings → Secrets and variables → Actions → New repository secret**.
Creá estos dos:

| Nombre             | Valor                      |
|--------------------|----------------------------|
| `TELEGRAM_TOKEN`   | el token de BotFather      |
| `TELEGRAM_CHAT_ID` | tu número de @userinfobot  |

### 5. Darle permiso para guardar su memoria
**Settings → Actions → General → Workflow permissions** → marcá
**Read and write permissions** → **Save**.

### 6. Probarlo
Pestaña **Actions → Bot de inmuebles → Run workflow**.
En unos minutos te llega a Telegram: *"✅ Bot de inmuebles activo"*.

La primera vez solo registra los avisos que ya existen, para no mandarte
cientos de mensajes. Desde la segunda revisión te avisa únicamente de los nuevos.

---

## Cambiar lo que busca

Todo está en **`config.yaml`** (lo podés editar desde la web de GitHub, ícono del lápiz):

- `precio_max_usd`: precio máximo.
- `dormitorios_min`: dormitorios mínimos.
- `tipos`: casas, apartamentos o ambos.
- `zonas`: barrios de cada departamento. Agregá o borrá líneas con el nombre
  normal del barrio. Una lista vacía (`[]`) busca en todo el departamento.
- `portales`: qué portales revisar.

Cómo decide la zona: el bot busca cada barrio en cada portal y, además, lee
la tarjeta del aviso. Si la tarjeta nombra **otro** barrio, lo descarta (por
ejemplo, un aviso que dice "Cordón Norte" o solo "Cordón" no pasa por
"Cordón Sur"). Para Punta del Este también acepta Península, Playa Mansa,
Playa Brava, Aidy Grill, Cantegril, San Rafael, Pinares, Rincón del Indio y
Roosevelt.

**Si un portal no encuentra un barrio** (en Actions aparece "0 avisos" en esa
URL), puede que use otro nombre. Entrá al portal, filtrá por ese barrio,
copiá la URL del navegador y agregala en `busquedas_extra`.

## Cambiar el horario

En `.github/workflows/bot.yml`, la línea `cron: "5 12,22 * * *"` está en hora
UTC (Uruguay es UTC−3). Por ejemplo, `"0 11,15,21 * * *"` revisa a las 8, 12
y 18 hs.

## Si algo falla

- **No llega nada:** revisá en **Actions** el detalle de la última ejecución;
  ahí se ve cuántos avisos leyó de cada portal.
- **"El bot no pudo leer ningún aviso":** algún portal cambió su diseño o
  bloqueó el acceso automático (Mercado Libre a veces lo hace). Probá
  reemplazar la URL por una copiada del navegador.
- **Te avisa de un aviso que no cumple los filtros:** algunos avisos no
  indican los dormitorios en el listado; por defecto el bot avisa igual. Para
  que los descarte, poné `avisar_si_no_indica_dormitorios: false`.

> GitHub pausa las tareas programadas de repositorios sin actividad durante
> 60 días. Como el bot guarda `seen.json` en cada revisión, normalmente se
> mantiene activo solo; si alguna vez se pausa, reactivalo desde la pestaña
> **Actions**.
