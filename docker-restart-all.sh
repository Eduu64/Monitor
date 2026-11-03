#!/bin/sh
# ============================================================================
#  docker-restart-all.sh — REINICIA los contenedores Docker que estén en marcha
# ============================================================================
#  Lo llama docker-restart-all.service en cada arranque del sistema, cuando el
#  daemon de Docker ya responde. También puedes lanzarlo a mano:
#      sudo systemctl start docker-restart-all
#
#  Nota: reinicia lo que está EN MARCHA (docker ps), no toca los contenedores
#  parados, para respetar los que alguien paró a propósito.
# ============================================================================

# Contenedores que NO se reinician (nombres, separados por espacios).
# El stack de monitorización se queda fuera: ya arranca solo con systemd y
# reiniciarlo haría que mandara DOS veces el mensaje de arranque a Discord.
# Déjalo vacío (EXCLUIR="") si también quieres reiniciarlos a ellos.
EXCLUIR="pcmonitor pcmonitor-proxy"

# Segundos de margen tras detectar el daemon, para que Docker termine de
# auto-arrancar los contenedores con política de reinicio antes de reiniciarlos.
ESPERA=10

# --- 1. Esperar a que el daemon conteste (máx. 120 s) ------------------------
i=0
until docker info >/dev/null 2>&1; do
    i=$((i + 2))
    if [ "$i" -ge 120 ]; then
        echo "docker no responde tras 120 s; no se reinicia nada" >&2
        exit 1
    fi
    sleep 2
done

# --- 2. Margen para que Docker acabe de levantar los suyos -------------------
sleep "$ESPERA"

# --- 3. Reiniciar los que estén en marcha, salvo los excluidos ---------------
ids=$(docker ps -q)
if [ -z "$ids" ]; then
    echo "No hay contenedores en marcha; nada que reiniciar"
    exit 0
fi

lista=""
for id in $ids; do
    nombre=$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's|^/||')
    if [ -z "$nombre" ]; then          # desapareció a mitad de la pasada
        lista="$lista $id"
        continue
    fi
    case " $EXCLUIR " in
        *" $nombre "*)
            echo "Saltado (excluido): $nombre"
            continue
            ;;
    esac
    lista="$lista $id"
done

if [ -z "$lista" ]; then
    echo "Los contenedores en marcha están todos excluidos; nada que reiniciar"
    exit 0
fi

echo "Reiniciando:$lista"
# $lista SIN comillas a propósito: docker restart admite varios contenedores
# y nos imprime el nombre de cada uno según termina.
docker restart $lista
