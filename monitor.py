#!/usr/bin/env python3
"""Monitor ligero para Debian que escribe en canales de Discord (solo API REST, sin gateway).

Canal de INFORMES (silenciado por ti): IP privada al arrancar, informe periódico.
Canal de ALERTAS (con notificación):   temperatura alta, apagado, contenedores caídos.
Solo usa la librería estándar.
"""
import glob
import http.client
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
REPORT_EVERY = int(os.getenv("REPORT_INTERVAL_MIN", "60")) * 60
CHECK_EVERY = int(os.getenv("TEMP_CHECK_SECONDS", "5"))
DOCKER_CHECK_EVERY = int(os.getenv("DOCKER_CHECK_SECONDS", "30"))
DOCKER_IGNORE = {n.strip() for n in os.getenv("DOCKER_IGNORE", "").split(",") if n.strip()}
WARN_TEMP = float(os.getenv("WARN_TEMP", "75"))
SHUTDOWN_TEMP = float(os.getenv("SHUTDOWN_TEMP", "80"))
STRIKES = int(os.getenv("SHUTDOWN_STRIKES", "3"))  # lecturas seguidas >= límite
SHUTDOWN_METHOD = os.getenv("SHUTDOWN_METHOD", "signal")  # signal | systemctl
DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")
HOST = os.uname().nodename

API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "DiscordBot (https://localhost, 1.0)",
}


def log(msg):
    print(time.strftime("%F %T"), msg, flush=True)


# ---------------------------------------------------------------- Discord
def _post(path, payload, timeout=10, retries=3):
    data = json.dumps(payload).encode()
    for _ in range(retries):
        req = urllib.request.Request(API + path, data=data, headers=HEADERS, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
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


def send(channel, text, mention=False, timeout=10, retries=3):
    """Envía un mensaje a un canal. Devuelve True si se entregó."""
    payload = {"allowed_mentions": {"parse": []}}
    if mention and MENTION_USER:
        text = f"<@{MENTION_USER}> {text}"
        payload["allowed_mentions"] = {"users": [MENTION_USER]}
    payload["content"] = text[:1990]
    return _post(f"/channels/{channel}/messages", payload, timeout, retries) is not None


def report(text):
    return send(REPORT_CHANNEL, text)


def alert(text, timeout=10, retries=3):
    return send(ALERT_CHANNEL, text, mention=True, timeout=timeout, retries=retries)


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
                with open(os.path.join(os.path.dirname(p), "name")) as f:
                    name = f.read().strip()
                if 0 < t < 150:
                    temps[f"{name}/{os.path.basename(p)}"] = t
            except (OSError, ValueError):
                pass
    return temps


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


def docker_status():
    try:
        data = docker_list()
    except Exception as e:
        return f"⚠️ Docker no accesible ({e})"
    if not data:
        return "Sin contenedores"
    lines = []
    for ct in sorted(data, key=ct_name):
        st = ct_state(ct)
        icon = {"running": "🟢", "unhealthy": "🟠"}.get(st, "🔴")
        lines.append(f"{icon} {ct_name(ct)} — {ct['Status']}")
    return "\n".join(lines)


# ----------------------------------------------------------------- Sistema
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP: no envía nada, solo elige ruta
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def uptime_secs():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def fmt_uptime(s):
    s = int(s)
    return f"{s // 86400}d {s % 86400 // 3600}h {s % 3600 // 60}m"


def device_status():
    l1, l5, l15 = os.getloadavg()
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith(("MemTotal", "MemAvailable")):
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
    total = mem["MemTotal"] / 1048576
    used = total - mem["MemAvailable"] / 1048576
    d = shutil.disk_usage("/")
    return (
        f"Uptime: {fmt_uptime(uptime_secs())}\n"
        f"Carga: {l1:.2f} {l5:.2f} {l15:.2f}\n"
        f"RAM: {used:.1f}/{total:.1f} GB\n"
        f"Disco: {d.used / 1e9:.1f}/{d.total / 1e9:.1f} GB ({d.used * 100 // d.total}%)"
    )


def fmt_temps(temps):
    if not temps:
        return "Sin sensores de temperatura"
    return "\n".join(f"{k}: {v:.0f}°C" for k, v in temps.items())


def build_report(temps):
    return (
        f"📊 **{HOST}** — IP privada: `{local_ip() or 'sin red'}`\n"
        f"**Temperaturas**\n```\n{fmt_temps(temps)}\n```"
        f"**Equipo**\n```\n{device_status()}\n```"
        f"**Docker**\n{docker_status()}"
    )


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
        cts = {ct_name(c): ct_state(c) for c in docker_list() if ct_name(c) not in DOCKER_IGNORE}
        if state["docker_err"]:
            state["docker_err"] = False
            alert(f"✅ **{HOST}**: Docker vuelve a estar accesible")
    except Exception as e:
        if not state["docker_err"]:
            state["docker_err"] = True
            alert(f"⚠️ **{HOST}**: Docker no accesible ({e})")
        return seen

    for name, st in cts.items():
        prev = seen.get(name)
        if st != "running" and prev == "running" and name not in down:
            down[name] = st
            icon = "🟠" if st == "unhealthy" else "🔴"
            alert(f"{icon} **{HOST}**: contenedor `{name}` caído (estado: {st})")
        elif st == "running" and name in down:
            del down[name]
            alert(f"✅ **{HOST}**: contenedor `{name}` vuelve a estar en marcha")

    for name, prev in seen.items():
        if name not in cts and prev == "running" and name not in down:
            down[name] = "eliminado"
            alert(f"🔴 **{HOST}**: contenedor `{name}` ha desaparecido (eliminado)")

    return cts


# -------------------------------------------------------------------- Main
def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log(f"Iniciado. Aviso {WARN_TEMP}°C, apagado {SHUTDOWN_TEMP}°C, informe cada {REPORT_EVERY // 60} min")

    boot_pending = True
    last_ip = None
    next_boot_try = 0
    next_ip_check = 0
    next_docker = 0
    next_report = time.monotonic() + REPORT_EVERY
    warned_at = -1e9
    hot = 0
    seen, down, state = {}, {}, {"docker_err": False}

    while True:
        now = time.monotonic()
        temps = read_temps()
        hottest = max(temps.values()) if temps else None

        # --- protección térmica (siempre lo primero)
        if hottest is not None:
            hot = hot + 1 if hottest >= SHUTDOWN_TEMP else 0
            if hot >= STRIKES:
                log(f"{hottest:.0f}°C >= {SHUTDOWN_TEMP}°C: apagando")
                alert(f"🔥 **{HOST}** a {hottest:.0f}°C (límite {SHUTDOWN_TEMP:.0f}°C). **Apagando el equipo.**\n"
                      f"```\n{fmt_temps(temps)}\n```", timeout=5, retries=1)
                poweroff()
                time.sleep(120)
                hot = 0
            elif hottest >= WARN_TEMP and now - warned_at > 600:
                warned_at = now
                alert(f"⚠️ **{HOST}** a {hottest:.0f}°C (aviso {WARN_TEMP:.0f}°C, apagado {SHUTDOWN_TEMP:.0f}°C)")

        # --- contenedores caídos
        if now >= next_docker:
            next_docker = now + DOCKER_CHECK_EVERY
            seen = check_containers(seen, down, state)

        # --- IP en arranque (reintenta hasta que haya red y Discord responda)
        if boot_pending and now >= next_boot_try:
            ip = local_ip()
            ok = False
            if ip:
                kind = "arranque del equipo" if uptime_secs() < 300 else "reinicio del servicio"
                ok = report(f"🖥️ **{HOST}** iniciado ({kind})\nIP privada: `{ip}`")
            if ok:
                boot_pending = False
                last_ip = ip
            else:
                next_boot_try = now + 10

        # --- cambio de IP privada mientras está encendido
        elif not boot_pending and now >= next_ip_check:
            next_ip_check = now + 60
            ip = local_ip()
            if ip and ip != last_ip:
                if report(f"🔄 **{HOST}** cambió de IP privada: `{last_ip}` → `{ip}`"):
                    last_ip = ip

        # --- informe periódico
        if now >= next_report:
            next_report = now + REPORT_EVERY
            report(build_report(temps))

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
