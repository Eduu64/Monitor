# 🖥️ Monitor de servidor Debian → avisos en Discord

Monitor ligero en Docker que vigila tu servidor Debian y te avisa en Discord
por **dos canales separados**:

| Canal | Qué envía | Formato |
|---|---|---|
| **Informes** (te lo silencias tú) | Arranque del equipo, cambio de IP privada, informe periódico detallado | Texto plano con bloques de código |
| **Alertas** (con notificación) | Temperatura alta 🔥, apagado térmico, contenedor caído / recuperado, Docker caído | Tarjeta embed con color + mención |

Diseñado con tres reglas fijas:

1. **Privacidad**: no consulta ningún servicio externo, no mira IP pública, no
   hay telemetría. Todo se lee del propio equipo (`/proc`, `/sys`, socket de
   Docker, `/` montado en solo lectura).
2. **Salida restringida**: el contenedor **no tiene internet**. Su única vía
   de salida es un proxy interno Tinyproxy que solo deja pasar HTTPS hacia
   `*.discord.com`. Verificado abajo con comandos.
3. **Mínimo consumo**: imágenes Alpine, límites de RAM y de procesos, logs
   rotados. En total ocupa ~60 MB de disco en imágenes y ~35 MB de RAM reales.

---

## 📁 Archivos

| Archivo | Para qué es |
|---|---|
| `monitor.py` | El monitor: lee el equipo y habla con la API REST de Discord (solo librería estándar de Python, sin dependencias) |
| `Dockerfile` | Imagen del monitor (`python:3.12-alpine`, ~50 MB) |
| `Dockerfile.proxy` | Imagen del proxy de salida (`alpine` + Tinyproxy de los repos de Alpine, ~10 MB) |
| `tinyproxy.conf` | Configuración del proxy: quién puede usarlo, a qué puerto se puede salir |
| `permitidos` | **La lista blanca de dominios**. Solo `*.discord.com`; aquí se añadiría otro servicio |
| `docker-compose.yml` | Los dos contenedores, la red interna sin salida y todo el endurecimiento |
| `.env.example.txt` | Plantilla de configuración → copiar a `.env` y rellenar |
| `docker-restart-all.service` | **Opcional, para el host**: unidad de systemd que al arrancar hace un reinicio real (`docker restart`) de los contenedores que estén en marcha |
| `docker-restart-all.sh` | Script que ejecuta la unidad anterior; las exclusiones (`EXCLUIR`) y la espera (`ESPERA`) se ajustan aquí |

---

## 🔐 Cómo está asegurado (arquitectura)

```
┌───────────────────────── Servidor Debian ─────────────────────────┐
│                                                                   │
│   ┌────────────┐  red interna (internal: true, SIN internet)      │
│   │  pcmonitor │◄───────────── solo habla con el proxy            │
│   └────────────┘                                                  │
│     lee /proc, /sys, /host:ro y el socket de Docker               │
│     caps: solo KILL · fs inmutable · no puede abrir conexiones    │
│                                                                   │
│   ┌──────────────────┐  bridge con internet (solo este sale)      │
│   │ pcmonitor-proxy  │──────────► *.discord.com:443 ──► Discord   │
│   │    (Tinyproxy)   │  deniega: otros dominios, IPs directas,    │
│   └──────────────────┘  puertos ≠ 443 y clientes ≠ monitor        │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

Capas de seguridad, de fuera hacia dentro:

1. **Red aislada**: `pcmonitor` va en una red Docker `internal: true`. Docker
   no le da ruta a internet: aunque el script estuviera comprometido, no
   puede abrir conexión alguna salvo hacia el proxy.
2. **Proxy con lista blanca** (`permitidos` + `tinyproxy.conf`): solo acepta
   conexiones del monitor (subred fija `10.213.101.0/24`) y solo deja pasar
   `CONNECT` al puerto 443 con destino a los dominios de la lista (regex con
   anclas: `^discord\.com$` y subdominios `\.discord\.com$`, de forma que
   `midiscord.com` NO colaría). Otro dominio → 403. Las IPs directas (sin
   dominio) también se deniegan.
3. **Contenedor endurecido**: `cap_drop: ALL` (solo `KILL` para poder apagar
   el equipo si sobrecalienta), `no-new-privileges`, sistema de archivos
   **solo lectura** con `/tmp` en tmpfs, límite de memoria, de procesos y de
   logs.
4. **Información mínima**: lee solo lo que necesita. La IP privada del
   servidor sale del `/proc` del PID 1 del host (ficheros, sin paquetes ni
   DNS); ni siquiera se hace un `UDP connect` a un DNS externo.
5. **Rotación de logs**: 1 MB × 2 ficheros por contenedor (no llena el disco).

### Riesgos asumidos (y por qué)

| Riesgo | Motivo | Mitigación |
|---|---|---|
| `pid: host` | Ver procesos del host y poder apagar el equipo térmicamente | Solo cap `KILL`; sin `SYS_ADMIN`, `NET_ADMIN`, etc. |
| `/` montado en `/host:ro` | Discos reales, distro, fstab, `/proc` de la red real | Solo lectura; el script solo abre ficheros concretos |
| Socket de Docker | Estado y recursos de los contenedores | El script solo hace `GET` a la API; ver "ir más allá" abajo |
| `apparmor=unconfined` | El perfil por defecto de Docker bloquea la señal a systemd | Es la excepción mínima para el apagado térmico |

### Ir más allá (opcional): proxy del socket de Docker

El socket de Docker equivale a root del host. Si quieres blindarlo, existe el
patrón habitual de poner un filtro delante (`tecnativa/docker-socket-proxy`),
que al monitor solo le deja la parte de consulta de contenedores. Para este
uso (servidor propio, contenedor sin internet y de código conocido) se
considera aceptable el riesgo; si lo quieres, dímelo y te lo integro.

---

## 🚀 Instalación

### 1. Crear el bot de Discord (una sola vez)

1. Entra en <https://discord.com/developers/applications> → **New Application**.
2. Pestaña **Bot** → **Reset Token** → copia el token (se verá una sola vez).
3. Más abajo, en *Privileged Gateway Intents*: este bot **no necesita ninguno**
   (solo envía mensajes, no lee el gateway).
4. Pestaña **OAuth2 → URL Generator**: marcas `bot`, y en permisos
   `Send Messages` + `Embed Links` + `View Channels`. Abre la URL generada y
   añade el bot a tu servidor.
5. En Discord: **Ajustes → Avanzado → Modo desarrollador** activado. Clic
   derecho sobre cada canal → *Copiar ID de canal*; y sobre tu usuario →
   *Copiar ID de usuario* (para las menciones de las alertas).

### 2. Preparar la carpeta en el servidor

Sube `pcmonitor.zip` al servidor y descomprímelo en `/opt/pcmonitor/`:

```bash
sudo mkdir -p /opt/pcmonitor && cd /opt/pcmonitor
sudo unzip ~/pcmonitor.zip && sudo mv pcmonitor/* . && rmdir pcmonitor
cp .env.example.txt .env
chmod 600 .env            # el token solo lo lee root (y tú)
nano .env                 # token, canales y tu ID de usuario
```

### 3. Arrancar

```bash
docker compose up -d --build
docker compose ps          # ambos "Up", el proxy además "(healthy)"
```

La primera vez tarda un poco: compila las dos imágenes (monitor y Tinyproxy)
y arranca el proxy antes que el monitor (por el `depends_on` con healthcheck).

En el canal de informes debería aparecer el mensaje de arranque 🖥️.

---

## 🔁 Arranque automático al encender el servidor

Ya está implementado, no hay que instalar nada más:

- Los dos servicios llevan `restart: unless-stopped`: cuando Docker arranque
  (al encender el equipo), los contenedores se levantan solos. Si tú los
  paras a mano, se quedan parados; en un reinicio del equipo, vuelven.
- Debian normalmente ya tiene Docker habilitado al arranque. Para asegurarte:

  ```bash
  sudo systemctl enable docker
  ```

- Al arrancar, el monitor espera a que haya red y **reintenta el mensaje de
  arranque cada 10 s** hasta que Discord responde: si el mensaje llega, es
  que todo el stack (red interna, proxy y salida a Discord) está operativo.
- El proxy arranca en menos de un segundo y pasa el healthcheck antes de que
  el monitor empiece a enviar.

### Extra opcional: reinicio REAL de todos los contenedores al arrancar

`restart: unless-stopped` solo **levanta** los contenedores; no los
**reinicia**. Si algún otro stack tuyo arranca mejor con un ciclo limpio
(`docker restart`) después de encender el equipo, este proyecto trae una
unidad de systemd que lo hace por ti:

- Espera a que el daemon de Docker responda (máx. 120 s) + un margen de 10 s
  para que Docker termine de auto-arrancar los suyos.
- Reinicia **los contenedores en marcha**; los parados no se tocan (respeta
  quien paró algo a propósito).
- **Excluye este stack** (`pcmonitor` y `pcmonitor-proxy`): ya arranca solo y
  reiniciarlo enviaría el mensaje de arranque dos veces a Discord.

Instalación (una sola vez):

```bash
cd /opt/pcmonitor
sudo cp docker-restart-all.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/docker-restart-all.sh
sudo cp docker-restart-all.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable docker-restart-all
```

Probarlo a mano ahora mismo (sin esperar a un reinicio del equipo):

```bash
sudo systemctl start docker-restart-all
journalctl -u docker-restart-all -n 30   # qué saltó, qué reinició
```

Ajustes: edita `/usr/local/bin/docker-restart-all.sh` → `EXCLUIR` (nombres
que no se reinician; déjalo vacío con `EXCLUIR=""` para reiniciar también
este stack) y `ESPERA` (margen tras detectar el daemon). Tras editarlo, no
hay que recargar nada: la unidad llama al script en cada arranque.

---

## 🐳 ¿Choca con mi otro stack que ya usa el 3128 o el 3000?

**No, en puertos no puede chocar**: ninguno de los dos contenedores publica
puertos en el host. El proxy escucha el 3128 *dentro* de sus redes Docker y
solo es alcanzable por el monitor; tu otro Squid puede seguir usando el 3128
y el 3000 del host tranquilamente.

La única colisión posible sería a nivel de **subred interna de Docker**. Por
eso este stack fija `10.213.101.0/24`, que está **fuera del rango que Docker
reparte automáticamente** (172.16-172.31.x y 192.168.x), donde seguro que ya
vive la red de tu otro stack. Si aun así chocara con algo tuyo, cambia la
subred en `docker-compose.yml` **y** en el `Allow` de `tinyproxy.conf`
(la misma en ambos sitios).

---

## 📊 Recursos por contenedor (ya implementado)

El informe periódico incluye, por cada contenedor en marcha:

```
🟢 nginx-proxy — Up 3 days (healthy)
      imagen: nginx:1.27-alpine
      CPU 12% · RAM 150.0 MB (14% del límite)
      reinicios desde la creación: 3
```

- **CPU %** calculado como `docker stats` (mismo método).
- **RAM usada** descontando la caché de páginas, y **% sobre su límite** de
  memoria (si el contenedor no tiene límite, el del host).
- Compatible con cgroup v1 y v2. Se activa/desactiva con `DOCKER_STATS=1|0`
  en el `.env`.

---

## ⚙️ Configuración (`.env`)

| Variable | Defecto | Qué hace |
|---|---|---|
| `DISCORD_TOKEN` | — | Token del bot (obligatorio) |
| `REPORT_CHANNEL_ID` | — | Canal de informes (obligatorio) |
| `ALERT_CHANNEL_ID` | — | Canal de alertas (obligatorio) |
| `DISCORD_USER_ID` | — | Tu ID: las alertas te mencionan `@tú` |
| `REPORT_INTERVAL_MIN` | `60` | Minutos entre informes; `0` = sin informes periódicos |
| `TEMP_CHECK_SECONDS` | `5` | Cada cuánto lee sensores |
| `DOCKER_CHECK_SECONDS` | `30` | Cada cuánto revisa contenedores |
| `WARN_TEMP` | `75` | °C para alerta naranja (máx. 1 aviso / 10 min) |
| `SHUTDOWN_TEMP` | `80` | °C para el apagado térmico |
| `SHUTDOWN_STRIKES` | `3` | Lecturas seguidas por encima antes de apagar |
| `SHUTDOWN_METHOD` | `signal` | `signal` = dentro de Docker (señal a systemd); `systemctl` = si lo ejecutas directo en Debian |
| `DOCKER_IGNORE` | — | Contenedores que no alertan (nombres, separados por comas) |
| `MESSAGE_FORMAT` | `mixto` | `mixto` = informes en texto y alertas en embed · `embed` = todo embeds · `text` = todo texto |
| `DOCKER_STATS` | `1` | CPU/RAM/reinicios por contenedor en el informe |
| `TOP_PROCESSES` | `5` | Procesos en el top del informe (`0` = sin top) |

Tras cambiar `.env`: `docker compose up -d` (recrea el contenedor).

---

## ✅ Verificar que la seguridad funciona

Dentro del propio monitor (usa el proxy por la variable `HTTPS_PROXY`):

```bash
cd /opt/pcmonitor

# 1) Discord debe funcionar → imprime 200
docker compose exec pcmonitor python3 -c "import urllib.request; \
print(urllib.request.urlopen('https://discord.com/api/v10/gateway', timeout=10).status)"

# 2) Cualquier otro dominio → HTTP 403 del proxy
docker compose exec pcmonitor python3 -c "import urllib.request
try: urllib.request.urlopen('https://www.google.com', timeout=10)
except Exception as e: print('Bloqueado por el proxy:', e)"

# 3) Conexión directa sin proxy → debe quedarse en timeout (red interna)
docker compose exec pcmonitor python3 -c "import socket
try: socket.create_connection(('1.1.1.1', 443), timeout=5)
except Exception as e: print('Sin salida directa:', e)"

# 4) Endurecimiento aplicado
docker inspect pcmonitor --format 'caps fuera={{.HostConfig.CapDrop}} · fs ro={{.HostConfig.ReadonlyRootfs}} · sinPriv={{.HostConfig.SecurityOpt}}'
```

Salida esperada: `200` / `Bloqueado…` / `Sin salida directa…` / caps y `ro=true`.

Y para ver cada conexión que deja pasar (o bloquea) el proxy:

```bash
docker compose logs -f proxy
```

---

## 📨 Qué mensajes envía

- **Arranque** (informes): temperaturas, equipo (uptime, distro/kernel, carga,
  modelo de CPU), memoria, discos y ficha de cada contenedor (estado, imagen,
  CPU/RAM y reinicios), además de la IP privada. Se reintenta cada 10 s hasta
  que sale, así que en un reinicio del equipo te llega en cuanto hay red: es
  la confirmación de que el stack se levantó solo y cómo quedó todo.
- **Cambio de IP privada** (informes): `192.168.1.10` → `192.168.1.42`. Detecta la IP
  de la interfaz con ruta por defecto leyendo ficheros del host (sin paquetes).
- **Informe periódico** (informes): uso real de CPU y por núcleo, top de
  procesos por CPU y RAM, RAM/swap/caché, cada partición real del fstab,
  temperaturas con tendencia ▲▼ y máximas del periodo, tráfico de red por
  interfaz, y **ficha completa de cada contenedor**: estado, salud, imagen,
  CPU, RAM con % de su límite y reinicios.
- **Alertas** (embeds): temperatura alta (naranja), apagado térmico (rojo),
  contenedor caído/desaparecido (rojo), unhealthy (naranja), recuperación y
  Docker restablecido (verde). Cada alerta lleva una instantánea del equipo.

Mensajes demasiado largos se trocean respetando el límite de 2000 caracteres
de Discord sin romper los bloques de código.

---

## 🪶 Optimización de recursos (lo que ocupa esto)

| Concepto | Valor |
|---|---|
| Imagen del monitor (`python:3.12-alpine`) | ~50 MB en disco |
| Imagen del proxy (`alpine` + Tinyproxy) | ~10 MB en disco |
| RAM real del monitor en reposo | ~15-25 MB (tope duro 64 MB) |
| RAM real de Tinyproxy en reposo | ~1-3 MB (tope duro 32 MB, 10 conexiones máx.) |
| Logs | 1 MB × 2 ficheros por contenedor |
| Escritura en disco | ninguna (contenedores de solo lectura, `/tmp` en tmpfs) |

Detalles: sin caché de bytecode de Python, sin dependencias externas en el
monitor, Tinyproxy baja al usuario `nobody` y solo tiene las caps `SETUID`/
`SETGID`, y ambos contenedores tienen `pids_limit`.

---

## 🔧 Mantenimiento

```bash
docker compose logs -f pcmonitor   # ver qué hace el monitor
docker compose logs -f proxy       # cada conexión que pasa por el proxy
docker compose restart             # reiniciar ambos
docker compose build --pull && docker compose up -d   # actualizar bases y código
docker compose down                # parar y quitar contenedores/red
```

### Añadir otro dominio permitido

Edita `permitidos` (está comentado en el propio archivo) y añade las dos
líneas del servicio:

```
^servicio\.com$
\.servicio\.com$
```

y aplica con `docker compose restart proxy` (así Tinyproxy recarga la lista).

### Si la subred interna choca con otra red

El compose fija `10.213.101.0/24` para la red `monitor` (fuera del rango
automático de Docker). Si aun así te chocara con una red tuya, cámbiala en
`docker-compose.yml` **y** en el `Allow` de `tinyproxy.conf` (la misma en
ambos sitios).

---

## 🩺 Solución de problemas

| Síntoma | Causa probable | Qué mirar |
|---|---|---|
| No aparece nada en Discord | Token/canales mal puestos, o el bot no está en el servidor | `docker compose logs pcmonitor` |
| El proxy no arranca: `Syntax error on line N` en sus logs | Tinyproxy **no admite comentarios al final de línea** en su config | Comentarios siempre en línea propia (formato de `tinyproxy.conf`) |
| El proxy arranca pero nunca pasa a `(healthy)` | Falta `^tinyproxy\.stats$` en `permitidos`: el healthcheck recibe 403 | Restaurar esa línea en `permitidos` y `docker compose up -d --build proxy` |
| `dependency failed to start` al hacer `up` | Lo anterior: el monitor no arranca si el proxy está caído o unhealthy | `docker compose logs proxy` para ver el motivo real |
| Las alertas no mencionan | Falta `DISCORD_USER_ID` o el bot no puede mencionarte | Revisa `.env` |
| `403` en los logs del proxy | Un dominio no permitido (normal si no es Discord) | Es el filtro funcionando |
| `Pool overlaps with other one on this address space` | La subred interna ya la usa otra red Docker | Cambiarla en compose + `tinyproxy.conf` |
| Sin temperaturas | El equipo no expone sensores en `/sys/class/thermal` ni `hwmon` | Comprueba con `sensors` (lm-sensors) |
| `IP privada: sin red` | La interfaz no tiene ruta por defecto en ese momento | Se recupera solo; si no, mira `/proc/1/net/route` |
| El apagado térmico no apaga | Falta `pid: host`, o PID 1 no es systemd | `docker compose logs pcmonitor` lo dice |
| `Error: response from daemon: ...` al crear la red | Docker con `live-restore` y redes viejas a medias | `docker compose down && docker compose up -d` |

---

## 🔒 Privacidad, por escrito

- **Lee**: `/proc` y `/sys` (CPU, RAM, temperaturas, red del host vía el
  `/proc` del PID 1), `/` montado en solo lectura (discos, distro, fstab) y
  el socket de Docker (contenedores y sus recursos).
- **Envía**: únicamente los mensajes a `discord.com` (a través del proxy
  interno), con el token del bot en la cabecera.
- **No hace**: ninguna consulta a servicios externos, ninguna resolución DNS
  propia, ninguna medición de IP pública, ninguna telemetría, no escribe
  ficheros (contenedor inmutable), no guarda historial (el estado vive en
  memoria; al reiniciar vuelve a informar de todo).
- Los mensajes van por TLS hasta Discord; el proxy solo tunela el CONNECT,
  no puede leer el contenido (no hace MITM, no hay CA instalada).

---

## 🧹 Desinstalación

```bash
cd /opt/pcmonitor
docker compose down --rmi local --volumes   # contenedores, red e imágenes locales
sudo rm -rf /opt/pcmonitor
```

Nada queda en el sistema fuera de esa carpeta (los logs rotados de Docker se
van con `down`).
