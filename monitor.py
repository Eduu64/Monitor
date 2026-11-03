#!/usr/bin/env python3
"""Monitor ligero para Debian que escribe en canales de Discord (solo API REST, sin gateway).

Canal de INFORMES (silenciado por ti), texto plano con bloques de código:
  - Arranque del equipo con resumen (uptime, sistema, CPU, RAM, Docker).
  - Aviso cuando cambia la IP privada.
  - Informe periódico detallado: CPU y top de procesos, RAM/swap, discos por
    partición real, temperaturas con tendencia y máximas, tráfico de red y
    ficha completa de cada contenedor (estado, salud, imagen, CPU/RAM, reinicios).

Canal de ALERTAS (con notificación), embeds con color:
  - Temperatura alta (naranja), apagado térmico y contenedor caído (rojo),
    recuperación y Docker restablecido (verde).
  - Cada alerta lleva una instantánea del equipo: temperaturas, carga, RAM,
    disco y estado de Docker en ese momento.

No consulta ningún servicio externo: todo se lee del propio equipo (/proc,
/sys, el socket de Docker y el /proc del PID 1 del host para la red). Lo
único que sale son los mensajes a Discord, y solo pueden salir por el proxy
interno del compose (HTTPS_PROXY → Tinyproxy con lista blanca de dominios).
Solo usa la librería estándar.
"""
import glob
import http.client
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.environ["DISCORD_TOKEN"]
REPORT_CHANNEL = os.environ["REPORT_CHANNEL_ID"]
ALERT_CHANNEL = os.environ["ALERT_CHANNEL_ID"]
MENTION_USER = os.getenv("DISCORD_USER_ID", "")  # opcional: te menciona en las alertas
REPORT_EVERY = max(0, int(os.getenv("REPORT_INTERVAL_MIN", "60"))) * 60  # 0 = sin informes
CHECK_EVERY = int(os.getenv("TEMP_CHECK_SECONDS", "5"))
DOCKER_CHECK_EVERY = int(os.getenv("DOCKER_CHECK_SECONDS", "30"))
DOCKER_IGNORE = {n.strip() for n in os.getenv("DOCKER_IGNORE", "").split(",") if n.strip()}
WARN_TEMP = float(os.getenv("WARN_TEMP", "75"))
SHUTDOWN_TEMP = float(os.getenv("SHUTDOWN_TEMP", "80"))
STRIKES = int(os.getenv("SHUTDOWN_STRIKES", "3"))  # lecturas seguidas >= límite
SHUTDOWN_METHOD = os.getenv("SHUTDOWN_METHOD", "signal")  # signal | systemctl
DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")

# --- presentación -----------------------------------------------------------
# mixto = informes en texto y alertas en embed | embed = todo embeds | text = todo texto
MESSAGE_FORMAT = os.getenv("MESSAGE_FORMAT", "mixto").lower()
DOCKER_STATS = os.getenv("DOCKER_STATS", "1") == "1"  # CPU/RAM/reinicios por contenedor
TOP_N = int(os.getenv("TOP_PROCESSES", "5"))          # 0 = sin top de procesos

HOST_ROOT = "/host" if os.path.isdir("/host/etc") else "/"  # / real del host, si está montada
CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1


def host_hostname():
    """Nombre real del equipo: /etc/hostname del host si está montado.

    Dentro del contenedor uname() daría el nombre del contenedor, no el del
    servidor; /host/etc/hostname es el del propio Debian.
    """
    try:
        with open(os.path.join(HOST_ROOT, "etc/hostname")) as f:
            n = f.read().strip()
            if n:
                return n
    except OSError:
        pass
    return os.uname().nodename


HOST = host_hostname()

API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "DiscordBot (https://localhost, 1.0)",
}

# Salida HTTP solo por el proxy de egreso (compose: Tinyproxy interno que
# filtra por dominio). Sin HTTPS_PROXY funciona normal, útil si lo ejecutas
# directo en Debian en vez de en Docker.
PROXY = os.getenv("HTTPS_PROXY", "")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler(
    {"https": PROXY} if PROXY else None))

C_REPORT = 0x5865F2  # blurple
C_OK = 0x57F287      # verde
C_WARN = 0xE67E22    # naranja
C_ALERT = 0xED4245   # rojo
ICON = {"running": "🟢", "unhealthy": "🟠"}
FENCE = "```"

# estado entre ciclos: tendencia y máximas de temperatura, contadores de red
S = {"temps_prev": None, "temps_max": {}, "net": None}


def log(msg):
    print(time.strftime("%F %T"), msg, flush=True)


# ---------------------------------------------------------------- Discord
def _post(path, payload, timeout=10, retries=3):
    data = json.dumps(payload).encode()
    for _ in range(retries):
        req = urllib.request.Request(API + path, data=data, headers=HEADERS, method="POST")
        try:
            with OPENER.open(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 1))
                except Exception:
                    wait = 1
                time.sleep(min(wait, 30))
                continue
            log(f"Discord HTTP {e.code} en {path}: {body[:200]!r}")
            return None
        except (urllib.error.URLError, OSError) as e:
            log(f"Discord sin conexión: {e}")
            time.sleep(2)
    return None


def _chunks(text, limit=1990):
    """Divide texto largo por líneas sin dejar bloques de código abiertos."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # línea desproporcionada: trocear a lo bruto
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        if buf and len(buf) + 1 + len(line) > limit:
            if buf.count(FENCE) % 2:  # dejamos un bloque abierto: cerrarlo
                buf += "\n" + FENCE
                line = FENCE + "\n" + line
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


def send(channel, text, mention=False, timeout=10, retries=3):
    """Envía texto (troceado si supera el límite). True si todo llegó."""
    ok = True
    limit = 1960 if mention else 1990  # margen para el prefijo de mención
    for i, part in enumerate(_chunks(text, limit)):
        payload = {"allowed_mentions": {"parse": []}}
        if mention and MENTION_USER and i == 0:
            part = f"<@{MENTION_USER}> {part}"
            payload["allowed_mentions"] = {"users": [MENTION_USER]}
        payload["content"] = part[:2000]
        if _post(f"/channels/{channel}/messages", payload, timeout, retries) is None:
            ok = False
    return ok


def _field_values(value, limit=1000):
    """Trocea el valor de un campo de embed por líneas (límite Discord: 1024)."""
    if len(value) <= limit:
        return [value]
    parts, buf = [], ""
    for line in value.split("\n"):
        while len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        if buf and len(buf) + 1 + len(line) > limit:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


def _pack_embeds(title, desc, color, fields, footer):
    """Reparte campos en embeds válidos (valor ≤1024, ≤25 campos, ≤6000 chars)."""
    expanded = []
    for name, value in fields:
        pcs = _field_values(value)
        for i, p in enumerate(pcs):
            fname = name if len(pcs) == 1 else f"{name} ({i + 1}/{len(pcs)})"
            expanded.append({"name": fname[:256], "value": p, "inline": False})
    groups, cur, size = [], [], len(title) + len(desc or "") + 60
    for fl in expanded:
        if cur and (size + len(fl["name"]) + len(fl["value"]) > 5600 or len(cur) >= 25):
            groups.append(cur)
            cur, size = [], len(title) + 60
        cur.append(fl)
        size += len(fl["name"]) + len(fl["value"])
    if cur or not groups:
        groups.append(cur)
    out = []
    for i, fl in enumerate(groups):
        e = {"title": (title if i == 0 else f"{title} (cont.)")[:256], "color": color, "fields": fl}
        if i == 0 and desc:
            e["description"] = desc[:4096]
        if i == len(groups) - 1 and footer:
            e["footer"] = {"text": footer[:2048]}
        e["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out.append(e)
    return out


def send_embed(channel, title, desc=None, color=C_REPORT, fields=None,
               footer=None, mention=False, timeout=10, retries=3):
    ok = True
    for i, emb in enumerate(_pack_embeds(title, desc, color, fields or [], footer)):
        payload = {"embeds": [emb], "allowed_mentions": {"parse": []}}
        if mention and MENTION_USER and i == 0:
            payload["content"] = f"<@{MENTION_USER}>"
            payload["allowed_mentions"] = {"users": [MENTION_USER]}
        if _post(f"/channels/{channel}/messages", payload, timeout, retries) is None:
            ok = False
    return ok


def _fence(v):
    return FENCE + "\n" + v + "\n" + FENCE


def send_out(channel, header, desc, sections, color=C_REPORT, is_alert=False,
             timeout=10, retries=3):
    """Mensaje compuesto: texto con bloques (informes) o embed con color (alertas)."""
    sections = [(t, v) for t, v in sections if v and v.strip()]
    use_embed = MESSAGE_FORMAT == "embed" or (MESSAGE_FORMAT == "mixto" and is_alert)
    if use_embed:
        fields = [(t, _fence(v)) for t, v in sections]
        return send_embed(channel, header, desc, color, fields,
                          footer=f"{HOST} · monitor", mention=is_alert,
                          timeout=timeout, retries=retries)
    parts = [header + (f"\n{desc}" if desc else "")]
    parts += [f"**{t}**\n{_fence(v)}" for t, v in sections]
    return send(channel, "\n\n".join(parts), mention=is_alert,
                timeout=timeout, retries=retries)


def notify(header, desc, sections=(), color=C_WARN, timeout=10, retries=3):
    """Alerta al canal de ALERTAS con mención + instantánea del equipo."""
    snap = ("Estado del equipo", snapshot(read_temps()))
    return send_out(ALERT_CHANNEL, header, desc, list(sections) + [snap], color,
                    is_alert=True, timeout=timeout, retries=retries)


# ------------------------------------------------------------ Temperaturas
def read_temps():
    """Devuelve {nombre: °C} leyendo thermal_zone y, si no hay, hwmon."""
    temps = {}
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            with open(z + "/temp") as f:
                t = int(f.read()) / 1000
            with open(z + "/type") as f:
                name = f.read().strip()
            if 0 < t < 150:
                temps[f"{os.path.basename(z)} ({name})"] = t
        except (OSError, ValueError):
            pass
    if not temps:
        for p in sorted(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")):
            try:
                with open(p) as f:
                    t = int(f.read()) / 1000
                base = os.path.dirname(p)
                try:
                    with open(base + "/name") as f:
                        name = f.read().strip()
                except OSError:
                    name = os.path.basename(base)
                try:  # etiqueta más concreta si existe (p. ej. "Core 0")
                    with open(p.replace("_input", "_label")) as f:
                        name += "/" + f.read().strip()
                except OSError:
                    pass
                if 0 < t < 150:
                    temps[f"{name}/{os.path.basename(p)}"] = t
            except (OSError, ValueError):
                pass
    return temps


def fmt_temps(temps, prev=None):
    """Lista ordenada de más a menos caliente, con tendencia vs la lectura anterior."""
    if not temps:
        return "Sin sensores de temperatura"
    lines = []
    for k, v in sorted(temps.items(), key=lambda kv: -kv[1]):
        mark = ""
        if prev and k in prev:
            d = v - prev[k]
            if abs(d) < 0.25:
                mark = " →"
            else:
                mark = f" ▲ +{d:.1f}" if d > 0 else f" ▼ {d:.1f}"
        lines.append(f"{k}: {v:.1f}°C{mark}")
    return "\n".join(lines)


def maxima_line():
    """Máximas alcanzadas desde el último informe periódico."""
    mx = S.get("temps_max") or {}
    if not mx:
        return ""
    items = ", ".join(f"{k} {v:.1f}°" for k, v in sorted(mx.items(), key=lambda kv: -kv[1])[:3])
    return "\nMáx desde el último informe — " + items


# ------------------------------------------------------------------ Docker
class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self._path)


def docker_list():
    c = _UnixConn(DOCKER_SOCK)
    try:
        c.request("GET", "/containers/json?all=1")
        return json.load(c.getresponse())
    finally:
        c.close()


def ct_name(ct):
    return ct["Names"][0].lstrip("/")


def ct_state(ct):
    """running | unhealthy | exited | restarting | dead ..."""
    if ct["State"] == "running":
        return "unhealthy" if "unhealthy" in ct["Status"] else "running"
    return ct["State"]


def ct_inspect(cid):
    try:
        c = _UnixConn(DOCKER_SOCK)
        c.request("GET", f"/containers/{cid}/json")
        d = json.load(c.getresponse())
        c.close()
        return d
    except Exception:
        return None


def ct_stats(cid):
    """(cpu%, RAM, % de su límite de RAM) de un contenedor en marcha."""
    try:
        c = _UnixConn(DOCKER_SOCK)
        c.request("GET", f"/containers/{cid}/stats?stream=false")
        s = json.load(c.getresponse())
        c.close()
    except Exception:
        return None, None, None
    return _parse_stats(s)


def _parse_stats(s):
    """Traduce el JSON de /stats a (cpu%, RAM usada, % sobre su límite).

    La CPU se calcula como en 'docker stats' (delta de ticks del
    contenedor / delta del sistema × núcleos). La RAM descarta la caché
    de páginas: cgroup v2 la llama inactive_file y el v1 cache.
    """
    try:
        cpu_d = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_d = s["cpu_stats"]["system_cpu_usage"] - s["precpu_stats"]["system_cpu_usage"]
        ncpu = s["cpu_stats"].get("online_cpus") or len(s["cpu_stats"]["cpu_usage"].get("percpu_usage") or []) or 1
        cpu = max(0.0, cpu_d / sys_d * ncpu * 100) if sys_d > 0 and cpu_d > 0 else 0.0
        st = s["memory_stats"].get("stats", {})
        mem = max(s["memory_stats"].get("usage", 0) - st.get("inactive_file", st.get("cache", 0)), 0)
        lim = s["memory_stats"].get("limit", 0)
        pct = min(int(mem * 100 / lim), 100) if lim > 0 and mem > 0 else None
        return cpu, mem, pct
    except (KeyError, TypeError, ZeroDivisionError):
        return None, None, None


def docker_head():
    """Resumen en una línea, para alertas e instantáneas."""
    try:
        data = docker_list()
    except Exception:
        return "no accesible"
    if not data:
        return "sin contenedores"
    n = sum(1 for c in data if ct_state(c) == "running")
    unh = sum(1 for c in data if ct_state(c) == "unhealthy")
    extra = f" · {unh} unhealthy" if unh else ""
    return f"{n}/{len(data)} en marcha{extra}"


def docker_section():
    """Ficha completa. Devuelve (resumen, líneas) o lanza si Docker no responde."""
    data = docker_list()
    if not data:
        return "Sin contenedores", []
    lines = []
    for ct in sorted(data, key=ct_name):
        name, st = ct_name(ct), ct_state(ct)
        icon = ICON.get(st, "🔴")
        line = f"{icon} {name} — {ct.get('Status', st)}"
        img = ct.get("Image", "")
        if img:
            line += f"\n      imagen: {img}"
        if st == "running" and DOCKER_STATS:
            cpu, ram, pct = ct_stats(ct["Id"])
            if cpu is not None:
                extra = f" ({pct}% del límite)" if pct is not None else ""
                line += f"\n      CPU {cpu:.0f}% · RAM {fmt_size(ram)}{extra}"
        if DOCKER_STATS:
            ins = ct_inspect(ct["Id"])
            if ins and ins.get("RestartCount"):
                line += f"\n      reinicios desde la creación: {ins['RestartCount']}"
        lines.append(line)
    n_run = sum(1 for c in data if ct_state(c) == "running")
    n_unh = sum(1 for c in data if ct_state(c) == "unhealthy")
    n_stop = len(data) - n_run - n_unh
    head = f"{n_run}/{len(data)} en marcha"
    if n_unh:
        head += f" · {n_unh} con salud fallando"
    if n_stop:
        head += f" · {n_stop} parado(s)"
    if DOCKER_IGNORE:
        head += f"\nIgnorados: {', '.join(sorted(DOCKER_IGNORE))}"
    return head, lines


# ----------------------------------------------------------------- Sistema
def _abrir_proc1(nombre):
    """Abre un fichero del /proc del PID 1 del host: la red REAL del equipo.

    El contenedor va en una red interna de Docker, así que su propia pila de
    red no vale; con pid: host y /host montado, /proc/1 refleja la del
    servidor. Si /proc/1 no está accesible, cae al /proc propio.
    """
    for p in (os.path.join(HOST_ROOT, "proc/1", nombre), "/proc/" + nombre):
        try:
            return open(p)
        except OSError:
            continue
    return None


def _hex_ip(h):
    """IP en hex little-endian de /proc/net/route → '192.168.1.5'."""
    try:
        return socket.inet_ntoa(bytes.fromhex(h)[::-1])
    except (ValueError, TypeError):
        return None


def host_ips():
    """IPs privadas (IPv4) del equipo, sin tocar la red ni mandar un paquete.

    Prefiere las IPs de las interfaces con ruta por defecto (la LAN real);
    si no hay, devuelve todas las locales no loopback. [] = sin red.
    """
    filas, ifaces_def, subs = [], set(), []
    f = _abrir_proc1("net/route")
    if f:
        with f:
            filas = [l.split() for l in f.readlines()[1:]]
        for p in filas:  # 1ª pasada: interfaces con puerta de enlace
            if len(p) < 8:
                continue
            dest, gw, mask = _hex_ip(p[1]), _hex_ip(p[2]), _hex_ip(p[7])
            if dest == "0.0.0.0" and mask == "0.0.0.0" and gw not in (None, "0.0.0.0"):
                ifaces_def.add(p[0])
        for p in filas:  # 2ª pasada: subred directa de esas interfaces
            if len(p) < 8 or p[0] not in ifaces_def:
                continue
            dest, gw, mask = _hex_ip(p[1]), _hex_ip(p[2]), _hex_ip(p[7])
            if gw == "0.0.0.0" and dest not in (None, "0.0.0.0") and mask not in (None, "0.0.0.0"):
                subs.append((dest, mask))

    def en_sub(ip, red, mask):
        try:
            return ipaddress.ip_address(ip) in ipaddress.ip_network(
                f"{red}/{mask}", strict=False)
        except ValueError:
            return False

    ips, cand = [], None
    f = _abrir_proc1("net/fib_trie")
    if f:
        with f:
            for linea in f:  # direcciones propias: '/32 link LOCAL'
                linea = linea.strip()
                if linea.startswith("|--"):
                    cand = linea[3:].strip()
                elif cand is not None and "/32" in linea and "LOCAL" in linea:
                    if (cand not in ips and not cand.startswith("127.")
                            and not cand.startswith("169.254.")):
                        ips.append(cand)
                    cand = None
                else:
                    cand = None
    en_lan = [ip for ip in ips if any(en_sub(ip, d, m) for d, m in subs)]
    return en_lan or ips


def uptime_secs():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def fmt_uptime(s):
    s = int(s)
    return f"{s // 86400}d {s % 86400 // 3600}h {s % 3600 // 60}m"


def fmt_dur(s):
    """Duración corta para 'estuvo caído X'."""
    s = int(s)
    if s < 3600:
        return f"{max(s // 60, 1)} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def fmt_size(n):
    """Bytes → texto corto: 512 B, 12.3 MB, 4.5 GB..."""
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024


def distro_name():
    try:
        with open(os.path.join(HOST_ROOT, "etc/os-release")) as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name") and ":" in line:
                    return line.split(":", 1)[1].strip()
            f.seek(0)
            for line in f:  # ARM/RISC-V: "model" o "Hardware"
                if line.lower().startswith(("model", "hardware")) and ":" in line:
                    v = line.split(":", 1)[1].strip()
                    if v and not v.replace(".", "").isdigit():
                        return v
    except OSError:
        pass
    try:  # SBC tipo Raspberry: árbol de dispositivos
        with open("/proc/device-tree/model") as f:
            return f.read().replace("\x00", "").strip() or None
    except OSError:
        return None


def mem_info():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            try:
                k, v = line.split(":", 1)
                m[k] = int(v.split()[0]) * 1024
            except (ValueError, IndexError):
                continue
    total = m.get("MemTotal", 0)
    cache = m.get("Cached", 0) + m.get("SReclaimable", 0) - m.get("Shmem", 0)
    swap_t = m.get("SwapTotal", 0)
    return {
        "total": total,
        "used": total - m.get("MemAvailable", 0),
        "avail": m.get("MemAvailable", 0),
        "cache": max(cache, 0),
        "swap_t": swap_t,
        "swap_u": max(swap_t - m.get("SwapFree", 0), 0),
    }


def mem_section():
    mem = mem_info()
    lines = [f"RAM: {fmt_size(mem['used'])} / {fmt_size(mem['total'])} "
             f"({mem['used'] * 100 // max(mem['total'], 1)}%)"
             f" · libre {fmt_size(mem['avail'])} · caché {fmt_size(mem['cache'])}"]
    if mem["swap_t"]:
        lines.append(f"Swap: {fmt_size(mem['swap_u'])} / {fmt_size(mem['swap_t'])} "
                     f"({mem['swap_u'] * 100 // max(mem['swap_t'], 1)})")
    return "\n".join(lines)


def _cpu_lines():
    out = []
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu"):
                p = line.split()
                try:
                    vals = [int(x) for x in p[1:]]
                except ValueError:
                    continue
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                out.append((p[0], sum(vals), sum(vals) - idle))
    return out


def snap_procs():
    """{pid: (nombre, ticks_cpu, rss_bytes)} de los procesos visibles."""
    out = {}
    for p in glob.glob("/proc/[0-9]*"):
        try:
            with open(p + "/stat", "rb") as f:
                s = f.read().decode("utf-8", "replace")
            r = s.rindex(")")
            name = s[s.index("(") + 1:r][:15]
            fld = s[r + 2:].split()
            ticks = int(fld[11]) + int(fld[12])
            try:
                with open(p + "/statm") as f:
                    rss = int(f.read().split()[1]) * PAGE
            except (OSError, ValueError, IndexError):
                rss = 0
            out[int(p.rsplit("/", 1)[1])] = (name, ticks, rss)
        except (OSError, ValueError, IndexError):
            pass
    return out


def sample_cpu(dt=1.0):
    """Uso de CPU total y por núcleo + tops de procesos, muestreando dt segundos."""
    a, pa = _cpu_lines(), snap_procs()
    time.sleep(dt)
    b, pb = _cpu_lines(), snap_procs()

    def pct(i):
        tot = b[i][1] - a[i][1]
        busy = b[i][2] - a[i][2]
        return max(0.0, min(100.0, 100.0 * busy / tot)) if tot > 0 else 0.0

    cpu = pct(0)
    cores = [pct(i) for i in range(1, len(a))] if len(a) > 1 else []
    rows = []
    for pid, (name, ticks, rss) in pb.items():
        if pid in pa:
            dc = (ticks - pa[pid][1]) / (dt * CLK) * 100
            rows.append((name, max(0.0, dc), rss))
    top_cpu = sorted([r for r in rows if r[1] >= 1.0], key=lambda r: -r[1])[:TOP_N]
    top_ram = sorted([r for r in rows if r[2] > 0], key=lambda r: -r[2])[:TOP_N]
    return cpu, cores, top_cpu, top_ram, len(pb)


def disk_list():
    """Montajes reales del host: {'/montaje': disk_usage}. Necesita /:/host:ro."""
    base = HOST_ROOT
    cands = {"/"}
    try:  # montajes declarados en el fstab del host
        with open(os.path.join(base, "etc/fstab")) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    p = line.split()
                    if len(p) >= 2 and p[1].startswith("/"):
                        cands.add(p[1].rstrip("/") or "/")
    except OSError:
        pass
    try:  # carpetas de primer nivel (detecta montajes por st_dev distinto)
        for e in os.scandir(base):
            if e.is_dir():
                cands.add("/" + e.name)
    except OSError:
        pass
    skip = {"/proc", "/sys", "/dev", "/run", "/snap", "/boot/efi", "/efi", "/tmp", "/var/tmp"}
    out, devs, root_dev = {}, set(), os.stat(base).st_dev
    for m in sorted(cands, key=len):
        if m in skip:
            continue
        path = base if m == "/" else base + m
        try:
            st = os.stat(path)
            if m != "/" and st.st_dev == root_dev:
                continue  # mismo fs que /: no es montaje aparte
            if st.st_dev in devs:
                continue  # mismo fs ya mostrado
            d = shutil.disk_usage(path)
            if d.total < (1 << 28):  # <256 MB: ruido
                continue
            devs.add(st.st_dev)
            out[m] = d
        except OSError:
            continue
    return out


def disk_section():
    rows = list(disk_list().items())
    if not rows:
        return "Sin datos de disco"
    rows.sort(key=lambda md: -(md[1].used * 100 // md[1].total))
    return "\n".join(f"{m}: {fmt_size(d.used)} / {fmt_size(d.total)} ({d.used * 100 // d.total}%)"
                     for m, d in rows)


def net_counters():
    """Bytes RX/TX acumulados por interfaz del HOST (el /proc del PID 1)."""
    out = {}
    f = _abrir_proc1("net/dev")
    if not f:
        return out
    with f:
        for line in f.readlines()[2:]:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            c = rest.split()
            try:
                out[name.strip()] = (int(c[0]), int(c[8]))
            except (ValueError, IndexError):
                continue
    return out


def net_section():
    cur = net_counters()
    prev = S.get("net")
    S["net"] = cur
    ifaces = [(k, v) for k, v in cur.items()
              if k != "lo" and not k.startswith("veth") and (v[0] or v[1])]
    if not ifaces:
        return ""
    lines = [f"Total desde el arranque: ↓{fmt_size(sum(v[0] for _, v in ifaces))} · "
             f"↑{fmt_size(sum(v[1] for _, v in ifaces))}"]
    if prev:
        keys = cur.keys() & prev.keys()
        drx = max(0, sum(cur[k][0] - prev[k][0] for k in keys))
        dtx = max(0, sum(cur[k][1] - prev[k][1] for k in keys))
        lines.append(f"Desde el último informe: ↓{fmt_size(drx)} · ↑{fmt_size(dtx)}")
    for k, v in sorted(ifaces, key=lambda kv: -(kv[1][0] + kv[1][1]))[:4]:
        lines.append(f"{k}: ↓{fmt_size(v[0])} · ↑{fmt_size(v[1])}")
    return "\n".join(lines)


def equipo_section():
    l1, l5, l15 = os.getloadavg()
    return (f"Uptime: {fmt_uptime(uptime_secs())}\n"
            f"Sistema: {distro_name() or 'desconocido'} · kernel {os.uname().release} · {os.uname().machine}\n"
            f"Carga (1/5/15 min): {l1:.2f} {l5:.2f} {l15:.2f}")


def snapshot(temps):
    """Instantánea compacta del equipo, adjunta a todas las alertas."""
    mem = mem_info()
    l1, l5, l15 = os.getloadavg()
    lines = [f"Uptime: {fmt_uptime(uptime_secs())}",
             f"RAM: {fmt_size(mem['used'])} / {fmt_size(mem['total'])} "
             f"({mem['used'] * 100 // max(mem['total'], 1)}%)"]
    if mem["swap_t"]:
        lines.append(f"Swap en uso: {fmt_size(mem['swap_u'])}")
    lines.append(f"Carga: {l1:.2f} {l5:.2f} {l15:.2f}")
    hot = max(temps.values()) if temps else None
    if hot is not None:
        lines.append(f"Temp máx: {hot:.1f}°C")
    d = disk_list().get("/")
    if d:
        lines.append(f"Disco /: {fmt_size(d.used)} / {fmt_size(d.total)} ({d.used * 100 // d.total}%)")
    lines.append(f"Docker: {docker_head()}")
    return "\n".join(lines)


# ------------------------------------------------- Mensajes compuestos
def build_report(temps):
    """(cabecera, desc, secciones) del informe periódico detallado."""
    cpu, cores, top_cpu, top_ram, nproc = sample_cpu(1.0)
    l1, l5, l15 = os.getloadavg()
    desc = f"IP privada: `{', '.join(host_ips()) or 'sin red'}`"

    secs = [("Equipo", equipo_section())]

    cpu_lines = [f"Modelo: {cpu_model() or 'desconocida'} ({NCPU} núcleos)",
                 f"Uso total: {cpu:.1f}% · carga {l1:.2f} {l5:.2f} {l15:.2f}"]
    if 1 < len(cores) <= 12:
        cpu_lines.append("Por núcleo: " + " ".join(f"{c:.0f}%" for c in cores))
    if TOP_N:
        if top_cpu:
            cpu_lines.append("Top CPU: " + " · ".join(f"{n} {c:.0f}%" for n, c, _ in top_cpu))
        if top_ram:
            total = mem_info()["total"]
            cpu_lines.append("Top RAM: " + " · ".join(
                f"{n} {fmt_size(r)} ({r * 100 // max(total, 1)}%)" for n, _, r in top_ram))
    cpu_lines.append(f"{nproc} procesos visibles")
    secs.append(("CPU y procesos", "\n".join(cpu_lines)))

    secs.append(("Memoria", mem_section()))
    secs.append(("Discos", disk_section()))
    secs.append(("Temperaturas", fmt_temps(temps, S.get("temps_prev")) + maxima_line()))
    red = net_section()
    if red:
        secs.append(("Red", red))
    try:
        head, lines = docker_section()
        secs.append(("Docker", head + ("\n" + "\n".join(lines) if lines else "")))
    except Exception as e:
        secs.append(("Docker", f"no accesible ({e})"))
    return f"📊 {HOST} — informe periódico", desc, secs


def build_boot(kind, temps=None):
    """(cabecera, desc, secciones) del mensaje de arranque.

    Resumen completo del equipo al arrancar: temperaturas, equipo (uptime,
    distro/kernel, carga, modelo de CPU), memoria, discos y la ficha detallada
    de cada contenedor (estado, imagen, CPU/RAM y reinicios). Con solo este
    mensaje ves si el stack y el resto de contenedores quedaron bien tras un
    reinicio, sin esperar al informe periódico.
    """
    if temps is None:
        temps = read_temps()
    secs = [("Temperaturas", fmt_temps(temps)),
            ("Equipo", equipo_section() + f"\nCPU: {cpu_model() or '?'} ({NCPU} núcleos)"),
            ("Memoria", mem_section()),
            ("Discos", disk_section())]
    try:
        head, lines = docker_section()
        secs.append(("Docker", head + ("\n" + "\n".join(lines) if lines else "")))
    except Exception as e:
        secs.append(("Docker", f"no accesible ({e})"))
    return (f"🖥️ {HOST} iniciado ({kind})",
            f"IP privada: `{', '.join(host_ips()) or 'sin red'}`", secs)


def build_ip_change(old, new):
    return (f"🔄 {HOST} cambió de IP privada", f"`{old or '?'}` → `{new}`",
            [("Equipo", f"Uptime: {fmt_uptime(uptime_secs())}")])


# ----------------------------------------------------------------- Apagado
def poweroff():
    os.sync()
    if SHUTDOWN_METHOD == "signal":
        # Con `pid: host`, el PID 1 es systemd; SIGRTMIN+4 = poweroff limpio.
        try:
            with open("/proc/1/comm") as f:
                init = f.read().strip()
        except OSError:
            init = "?"
        if init != "systemd":
            log(f"PID 1 es '{init}', no systemd: ¿falta `pid: host`?")
            return
        os.kill(1, signal.SIGRTMIN + 4)
    else:
        subprocess.run(["systemctl", "poweroff"], check=False)


# ------------------------------------------------- Vigilancia de contenedores
def check_containers(seen, down, state):
    """Compara con la lectura anterior y manda alertas. Devuelve el nuevo `seen`."""
    try:
        cts = {}
        for c in docker_list():
            n = ct_name(c)
            if n not in DOCKER_IGNORE:
                cts[n] = {"state": ct_state(c), "img": c.get("Image", "")}
    except Exception as e:
        if not state["docker_err"]:
            state["docker_err"] = True
            notify(f"⚠️ {HOST} — Docker no accesible", str(e)[:300], [], color=C_WARN)
        return seen
    if state["docker_err"]:
        state["docker_err"] = False
        n_run = sum(1 for v in cts.values() if v["state"] == "running")
        notify(f"✅ {HOST} — Docker vuelve a estar accesible",
               f"{n_run}/{len(cts)} contenedores en marcha", [], color=C_OK)

    for name, info in cts.items():
        st = info["state"]
        prev = seen.get(name)
        if st != "running" and prev and prev["state"] == "running" and name not in down:
            down[name] = {"t": time.time()}
            icon = "🟠" if st == "unhealthy" else "🔴"
            notify(f"{icon} {HOST} — contenedor caído",
                   f"`{name}` — estado: **{st}**\nImagen: `{info['img'] or '?'}`",
                   [], color=C_ALERT if st != "unhealthy" else C_WARN)
        elif st == "running" and name in down:
            d = down.pop(name)
            notify(f"✅ {HOST} — contenedor recuperado",
                   f"`{name}` está en marcha de nuevo (estuvo caído {fmt_dur(time.time() - d['t'])})",
                   [], color=C_OK)

    for name, prev in seen.items():
        if name not in cts and prev["state"] == "running" and name not in down:
            down[name] = {"t": time.time()}
            notify(f"🔴 {HOST} — contenedor eliminado",
                   f"`{name}` ha desaparecido de Docker (estaba en marcha)", [], color=C_ALERT)
    return cts


# -------------------------------------------------------------------- Main
def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    extra = f"informe cada {REPORT_EVERY // 60} min" if REPORT_EVERY else "sin informe periódico"
    log(f"Iniciado. Aviso {WARN_TEMP}°C, apagado {SHUTDOWN_TEMP}°C, {extra}, formato {MESSAGE_FORMAT}")

    boot_pending = True
    last_ip = None
    next_boot_try = 0
    next_ip_check = 0
    next_docker = 0
    next_report = time.monotonic() + REPORT_EVERY if REPORT_EVERY else float("inf")
    warned_at = -1e9
    hot = 0
    seen, down, state = {}, {}, {"docker_err": False}

    while True:
        now = time.monotonic()
        temps = read_temps()
        hottest = max(temps.values()) if temps else None

        for k, v in temps.items():  # máxima de cada zona desde el último informe
            if v > S["temps_max"].get(k, 0):
                S["temps_max"][k] = v

        # --- protección térmica (siempre lo primero)
        if hottest is not None:
            hot = hot + 1 if hottest >= SHUTDOWN_TEMP else 0
            if hot >= STRIKES:
                log(f"{hottest:.1f}°C >= {SHUTDOWN_TEMP:.0f}°C: apagando")
                notify(f"🔥 {HOST} — APAGADO TÉRMICO",
                       f"**{hottest:.1f}°C** ≥ {SHUTDOWN_TEMP:.0f}°C en {STRIKES} lecturas "
                       f"seguidas. Apagando el equipo.",
                       [("Temperaturas", fmt_temps(temps))],
                       color=C_ALERT, timeout=5, retries=1)
                poweroff()
                time.sleep(120)
                hot = 0
            elif hottest >= WARN_TEMP and now - warned_at > 600:
                warned_at = now
                notify(f"⚠️ {HOST} — temperatura alta",
                       f"**{hottest:.1f}°C** ahora mismo "
                       f"(aviso {WARN_TEMP:.0f}°C · apagado {SHUTDOWN_TEMP:.0f}°C)",
                       [("Temperaturas", fmt_temps(temps, S.get("temps_prev")))], color=C_WARN)

        # --- contenedores caídos
        if now >= next_docker:
            next_docker = now + DOCKER_CHECK_EVERY
            seen = check_containers(seen, down, state)

        # --- IP en arranque (reintenta hasta que haya red y Discord responda)
        if boot_pending and now >= next_boot_try:
            ip = ", ".join(host_ips())
            ok = False
            if ip:
                kind = "arranque del equipo" if uptime_secs() < 300 else "reinicio del servicio"
                try:
                    ok = send_out(REPORT_CHANNEL, *build_boot(kind, temps))
                except Exception as e:
                    log(f"Error en el mensaje de arranque: {e}")
            if ok:
                boot_pending = False
                last_ip = ip
            else:
                next_boot_try = now + 10

        # --- cambio de IP privada mientras está encendido
        elif not boot_pending and now >= next_ip_check:
            next_ip_check = now + 60
            ip = ", ".join(host_ips())
            if ip and ip != last_ip:
                if send_out(REPORT_CHANNEL, *build_ip_change(last_ip, ip)):
                    last_ip = ip

        # --- informe periódico
        if now >= next_report:
            next_report = now + REPORT_EVERY
            try:
                send_out(REPORT_CHANNEL, *build_report(temps))
                S["temps_max"] = {}  # reinicia las máximas del periodo
            except Exception as e:
                log(f"Error generando el informe: {e}")

        S["temps_prev"] = temps
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
