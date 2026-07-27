#!/usr/bin/env bash
#
# Instalador de mkbackup para Debian y derivados.
#
#   curl -fsSL https://raw.githubusercontent.com/mtandazo35/mikrotik-backup/main/install.sh | bash
#
# Es IDEMPOTENTE: se puede volver a lanzar para actualizar. Nunca pisa
# config.yaml, el inventario ni los datos ya guardados; si existen, los deja
# como estan y solo actualiza el codigo y las unidades de systemd.
#
# No pide nada por teclado a proposito. Cuando esto llega por una tuberia
# (`curl | bash`), la entrada estandar ES el propio script: cualquier `read`
# se comeria las lineas que faltan por ejecutar. Por eso la clave del panel se
# genera sola y se imprime al final.

set -euo pipefail

REPO="${MKBACKUP_REPO:-https://github.com/mtandazo35/mikrotik-backup.git}"
RAMA="${MKBACKUP_RAMA:-main}"
DESTINO="${MKBACKUP_DESTINO:-/root/mkbackup}"
FUENTE="$DESTINO/app"

rojo() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
paso() { printf '\n\033[1m==>\033[0m %s\n' "$*"; }
aviso() { printf '    %s\n' "$*"; }

morir() {
  rojo "ERROR: $*"
  exit 1
}

# --- Comprobaciones previas -------------------------------------------------
paso "Comprobando el sistema"

[ "$(id -u)" -eq 0 ] || morir "hay que ejecutarlo como root: todo vive en /root."

command -v apt-get >/dev/null 2>&1 ||
  morir "esto es para Debian y derivados; no encuentro apt-get."

if [ -e "$DESTINO" ] && [ ! -d "$DESTINO" ]; then
  morir "$DESTINO existe y no es un directorio."
fi

ACTUALIZANDO=no
[ -d "$FUENTE/.git" ] && ACTUALIZANDO=si

if [ "$ACTUALIZANDO" = si ]; then
  aviso "Ya hay una instalacion en $DESTINO: se actualiza sin tocar los datos."
else
  aviso "Instalacion nueva en $DESTINO."
fi

# --- Dependencias del sistema ----------------------------------------------
paso "Instalando lo que hace falta (git, python3, venv)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null
aviso "$(python3 --version)"

# --- Codigo -----------------------------------------------------------------
paso "Descargando mkbackup"

mkdir -p "$DESTINO"
chmod 700 "$DESTINO"

if [ "$ACTUALIZANDO" = si ]; then
  git -C "$FUENTE" fetch --quiet origin "$RAMA"
  git -C "$FUENTE" reset --quiet --hard "origin/$RAMA"
else
  git clone --quiet --branch "$RAMA" --depth 1 "$REPO" "$FUENTE"
fi
aviso "version $(git -C "$FUENTE" rev-parse --short HEAD)"

# --- Entorno de Python ------------------------------------------------------
paso "Preparando el entorno de Python"

[ -d "$DESTINO/.venv" ] || python3 -m venv "$DESTINO/.venv"
"$DESTINO/.venv/bin/pip" install --quiet --upgrade pip
"$DESTINO/.venv/bin/pip" install --quiet -r "$FUENTE/requirements.txt"
# Opcional: solo la usa la importacion de .xlsx desde el navegador. Si falla
# (por ejemplo sin salida a internet), no es motivo para abortar la
# instalacion: el resto del sistema funciona igual.
"$DESTINO/.venv/bin/pip" install --quiet openpyxl 2>/dev/null ||
  aviso "openpyxl no se pudo instalar; la importacion sera solo de .csv."

# --- Configuracion ----------------------------------------------------------
paso "Configuracion"

CLAVE=""
if [ -f "$DESTINO/config.yaml" ]; then
  aviso "config.yaml ya existe: no se toca."
else
  cp "$FUENTE/config.example.yaml" "$DESTINO/config.yaml"

  # Una clave larga al azar. Se imprime UNA vez al final y no se guarda en
  # ningun sitio en claro: en el archivo solo va su hash.
  CLAVE="$("$DESTINO/.venv/bin/python" - <<'PY'
import secrets, string
alfabeto = string.ascii_letters + string.digits
print("".join(secrets.choice(alfabeto) for _ in range(20)))
PY
)"
  HASH="$(cd "$FUENTE" && "$DESTINO/.venv/bin/python" - "$CLAVE" <<'PY'
import sys
from mkbackup.sesion import hashear
print(hashear(sys.argv[1]))
PY
)"
  # El hash lleva $ y / (es base64): se sustituye con python y no con sed para
  # no pelearse con el escapado.
  "$DESTINO/.venv/bin/python" - "$DESTINO/config.yaml" "$HASH" <<'PY'
import sys
from pathlib import Path

ruta, hash_clave = Path(sys.argv[1]), sys.argv[2]
texto = ruta.read_text(encoding="utf-8")
ruta.write_text(texto.replace('clave_hash: ""', f'clave_hash: "{hash_clave}"', 1),
                encoding="utf-8")
PY
  aviso "config.yaml creado con una clave nueva para el panel."
fi

if [ -f "$DESTINO/inventory.csv" ]; then
  aviso "inventory.csv ya existe: no se toca."
else
  # Solo la cabecera, NO los equipos de ejemplo. Sembrarlos hace que el primer
  # ciclo se pase minutos intentando conectar con IPs que no existen y que el
  # panel abra con una pantalla llena de fallos que no son de nadie. Los
  # ejemplos estan en examples/inventory.csv para copiar de ahi.
  head -n 1 "$FUENTE/examples/inventory.csv" > "$DESTINO/inventory.csv"
  aviso "inventory.csv creado vacio: anade tus equipos desde el panel."
fi

# Llevan credenciales de la red: que no los lea nadie mas.
chmod 600 "$DESTINO/config.yaml" "$DESTINO/inventory.csv"

# --- Servicios --------------------------------------------------------------
paso "Instalando los servicios"

install -m 644 "$FUENTE/systemd/mkbackup.service" /etc/systemd/system/
install -m 644 "$FUENTE/systemd/mkbackup-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet --now mkbackup.service mkbackup-web.service
sleep 1

for unidad in mkbackup mkbackup-web; do
  if systemctl is-active --quiet "$unidad"; then
    aviso "$unidad: en marcha"
  else
    rojo "    $unidad: NO arranco. Mira: journalctl -u $unidad -n 30"
  fi
done

# --- Resumen ----------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
paso "Listo"

verde "  Panel:  http://127.0.0.1:8080/"
if [ -n "$CLAVE" ]; then
  verde "  Usuario: admin"
  verde "  Clave:   $CLAVE"
  aviso ""
  aviso "APUNTALA AHORA: no se guarda en ningun sitio, solo su hash."
  aviso "Se cambia desde el panel, o con: mkbackup --clave-usuario admin"
fi

cat <<FIN

  Lo que falta por hacer, a mano:

    1. Pon las credenciales SSH de tus MikroTik en $DESTINO/config.yaml
       (seccion ssh:) o desde el panel, en Ajustes.
    2. Carga tus equipos en $DESTINO/inventory.csv, o desde el panel.
    3. El panel escucha solo en 127.0.0.1. Para verlo desde otra maquina,
       ponle delante un proxy con TLS: habla HTTP en claro y la clave
       viajaria legible. No lo abras con 0.0.0.0 sin eso.

  Comprobar que va:   systemctl status mkbackup mkbackup-web
  Ver el registro:    journalctl -u mkbackup -f
  Actualizar:         vuelve a lanzar este mismo instalador

FIN

[ -n "${IP:-}" ] && aviso "IP de esta maquina: $IP (recuerda el proxy TLS)"
exit 0
