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
# La direccion desde la que se descarga este mismo script, para poder
# escribirla en el resumen del final. Se compone del repositorio para que
# apunte al de verdad si alguien instala desde un clon suyo.
REPO_CRUDO="${REPO%.git}"
REPO_CRUDO="${REPO_CRUDO/github.com/raw.githubusercontent.com}/$RAMA/install.sh"
# La ruta NO es configurable, y es a proposito: config.example.yaml y las dos
# unidades de systemd llevan /root/mkbackup escrito dentro. Ofrecer una
# variable que no reescribe nada de eso dejaba el codigo en un sitio y los
# servicios apuntando a otro, o sea un sistema muerto que ademas parecia
# instalado. Para otra ruta, la instalacion manual del README.
DESTINO="/root/mkbackup"
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
if [ -d "$FUENTE/.git" ]; then
  ACTUALIZANDO=si
elif [ -d "$FUENTE" ] && [ -n "$(ls -A "$FUENTE" 2>/dev/null)" ]; then
  # git clone sobre un directorio con cosas dentro falla con un error suyo que
  # no dice como salir del paso. Pasa de verdad: alguien cuya instalacion se
  # corto a medias y lo vuelve a intentar.
  morir "$FUENTE existe y tiene contenido, pero no es un clon de git.
    Si es de una instalacion a medias, borralo y vuelve a lanzar esto:
        rm -rf $FUENTE"
fi

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
  # Refspec explicito: el clon inicial es --depth 1 --branch <rama>, y su
  # refspec solo trae ESA rama. Un `fetch origin otra-rama` a secas "funciona"
  # pero nunca crea refs/remotes/origin/otra-rama, y el reset siguiente muere
  # con "ambiguous argument" dejando los servicios con el codigo viejo.
  git -C "$FUENTE" remote set-url origin "$REPO"
  git -C "$FUENTE" fetch --quiet --depth 1 origin     "+refs/heads/$RAMA:refs/remotes/origin/$RAMA"
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
# Opcionales. Si fallan (por ejemplo sin salida a internet) NO se aborta la
# instalacion: el sistema funciona sin las dos, solo con menos comodidades, y
# quedarse sin panel por no poder leer un Excel seria un mal negocio.
"$DESTINO/.venv/bin/pip" install --quiet openpyxl 2>/dev/null ||
  aviso "openpyxl no se pudo instalar; la importacion sera solo de .csv."
# Ajusta la imagen de fondo del login al subirla: la reduce, la comprime y le
# quita los metadatos. Sin ella la imagen se guarda tal cual y el formulario
# avisa de que hay que subirla ya pequena.
"$DESTINO/.venv/bin/pip" install --quiet Pillow 2>/dev/null ||
  aviso "Pillow no se pudo instalar; el fondo del login se guardara tal cual."

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
  # Se comprueba que la sustitucion OCURRIO. Era un replace silencioso: si la
  # cadena exacta no estuviera en la plantilla, el script seguia adelante e
  # imprimia en verde una clave que no valia para entrar.
  grep -q "clave_hash: \"pbkdf2_" "$DESTINO/config.yaml" ||
    morir "no se pudo escribir la clave en config.yaml; revisa la plantilla."
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

# --- El comando mkbackup ----------------------------------------------------
paso "Instalando el comando mkbackup"

# Un envoltorio de tres lineas en vez de empaquetar el proyecto con pip: el
# codigo se actualiza con un `git reset --hard`, no reinstalando, asi que un
# paquete instalado se quedaria viejo sin que nadie lo note.
#
# Que este comando exista NO es comodidad. El panel, el instalador y la
# configuracion le dicen a quien se queda fuera que ejecute
# `mkbackup --clave-usuario admin`; si el comando no existe, la unica salida
# documentada de "he perdido la clave" termina en `command not found`.
cat > /usr/local/bin/mkbackup <<ENVOLTORIO
#!/bin/sh
# Generado por el instalador de mkbackup. Se puede borrar sin miedo.
cd "$FUENTE" || exit 1
exec "$DESTINO/.venv/bin/python" -m mkbackup.cli -c "$DESTINO/config.yaml" "\$@"
ENVOLTORIO
chmod 755 /usr/local/bin/mkbackup

# Se comprueba que responde de verdad, no solo que el archivo esta escrito.
if mkbackup --version >/dev/null 2>&1; then
  aviso "mkbackup $(mkbackup --version 2>&1 | awk '{print $NF}')"
else
  morir "el comando mkbackup no funciona; revisa $DESTINO"
fi

# --- Servicios --------------------------------------------------------------
paso "Instalando los servicios"

install -m 644 "$FUENTE/systemd/mkbackup.service" /etc/systemd/system/
install -m 644 "$FUENTE/systemd/mkbackup-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet mkbackup.service mkbackup-web.service

# RESTART, no `enable --now`. Esto estaba mal y era grave: `--now` solo arranca
# lo que esta parado, asi que en una maquina que ya tenia mkbackup corriendo no
# hacia absolutamente nada. El instalador se traia el codigo nuevo, decia
# "Listo" con la version nueva escrita en pantalla, y los dos servicios seguian
# ejecutando el codigo viejo hasta el proximo reinicio de la maquina.
#
# Y es el camino documentado para actualizar ("Actualizar: vuelve a lanzar este
# mismo instalador"), o sea que la unica forma de aplicar un arreglo era la que
# no lo aplicaba. Lo peor: no fallaba, mentia. Se vio al desplegar un arreglo
# del panel y comprobar que la pagina servida seguia siendo la de antes.
systemctl restart mkbackup.service mkbackup-web.service

# Se espera un poco antes de mirar: con Type=simple, un servicio que muere a
# los dos segundos todavia figura como activo en el primero, y el instalador
# daria por bueno algo que esta entrando en bucle de reinicios.
sleep 4
CAIDOS=""
for unidad in mkbackup mkbackup-web; do
  if systemctl is-active --quiet "$unidad"; then
    aviso "$unidad: en marcha"
  else
    CAIDOS="$CAIDOS $unidad"
    rojo "    $unidad: NO arranco."
  fi
done

# --- Proxy con TLS, si se pide ----------------------------------------------
#
# Se activa con MKBACKUP_TLS=1 y NO por defecto:
#
#   curl -fsSL .../install.sh | MKBACKUP_TLS=1 bash
#
# Es opt-in porque instala nginx, escribe en /etc/nginx y apaga el sitio de
# ejemplo. Hacer eso sin avisar en una maquina que ya sirve otra cosa seria
# tumbarsela, y este instalador se lanza con una tuberia desde internet: tiene
# que hacer lo que dice y nada mas.
#
# El certificado se firma a si mismo, asi que el navegador avisara la primera
# vez. Es lo que hay sin un dominio, y la comparacion honesta no es contra un
# certificado de verdad sino contra HTTP en claro, que es la alternativa: por
# aqui viajan la clave del panel y el inventario de toda la flota.
TLS_PUESTO=""
if [ "${MKBACKUP_TLS:-0}" = "1" ]; then
  paso "Poniendo nginx con TLS por delante"

  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx >/dev/null \
    || morir "no se pudo instalar nginx"

  # El certificado solo se crea si NO hay uno. Regenerarlo en cada actualizacion
  # cambiaria la huella y el navegador lo leeria como si alguien se estuviera
  # metiendo en medio, que es justo el aviso que no debe perder significado.
  mkdir -p /etc/nginx/tls
  if [ ! -s /etc/nginx/tls/mkbackup.crt ] || [ ! -s /etc/nginx/tls/mkbackup.key ]; then
    aviso "Generando un certificado firmado por si mismo (10 anos)"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout /etc/nginx/tls/mkbackup.key \
      -out /etc/nginx/tls/mkbackup.crt \
      -subj "/CN=$(hostname -f 2>/dev/null || hostname)" >/dev/null 2>&1 \
      || morir "no se pudo generar el certificado"
    chmod 600 /etc/nginx/tls/mkbackup.key
  else
    aviso "Ya habia certificado en /etc/nginx/tls: se deja el que hay"
  fi

  # Se guarda lo que hubiera para poder devolverlo si la configuracion nueva no
  # valida. Dejar a alguien sin panel por una actualizacion no es aceptable.
  RESPALDO_NGINX=""
  if [ -f /etc/nginx/sites-available/mkbackup ]; then
    RESPALDO_NGINX="/etc/nginx/sites-available/mkbackup.antes-$(date +%Y%m%d-%H%M%S)"
    cp -a /etc/nginx/sites-available/mkbackup "$RESPALDO_NGINX"
  fi

  install -m 644 "$FUENTE/nginx/mkbackup.conf" /etc/nginx/sites-available/mkbackup
  ln -sf /etc/nginx/sites-available/mkbackup /etc/nginx/sites-enabled/mkbackup
  # El sitio de ejemplo de Debian escucha en el 80 como default_server, asi que
  # se queda con todo lo que no case por nombre: con el puesto, el panel no se
  # ve y parece que el proxy no funciona.
  rm -f /etc/nginx/sites-enabled/default

  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx 2>/dev/null || systemctl restart nginx
    TLS_PUESTO="si"
    aviso "nginx: en marcha"
  else
    rojo "    La configuracion de nginx no valida. Se deja como estaba."
    nginx -t 2>&1 | sed 's/^/    /'
    if [ -n "$RESPALDO_NGINX" ]; then
      cp -a "$RESPALDO_NGINX" /etc/nginx/sites-available/mkbackup
    else
      rm -f /etc/nginx/sites-enabled/mkbackup
    fi
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
  fi

  # Sin esto, el freno a la fuerza bruta y el registro de accesos dejan de
  # servir: todas las peticiones llegan desde el proxy, asi que mkbackup veria
  # a todo el mundo como 127.0.0.1 -cinco fallos de cualquiera bloquearian a la
  # empresa entera- y cada linea del registro diria esa misma direccion. Se toca
  # solo si sigue con el valor de fabrica, para no pisar una lista puesta a mano.
  if grep -q '^  proxies_de_confianza: \[\]$' "$DESTINO/config.yaml" 2>/dev/null; then
    sed -i 's/^  proxies_de_confianza: \[\]$/  proxies_de_confianza: ["127.0.0.1"]/' \
      "$DESTINO/config.yaml"
    aviso "config.yaml: se declara 127.0.0.1 como proxy de confianza"
    systemctl restart mkbackup-web.service
  fi
fi

# --- Resumen ----------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

# Si algo no arranco, NO se dice "Listo" ni se da la direccion del panel: eso
# manda a la gente a un navegador en vez de al log, que es donde esta la
# respuesta. Se sale con error para que se note tambien desde un script.
if [ -n "$CAIDOS" ]; then
  paso "Instalado, pero NO esta corriendo"
  rojo "  No arrancaron:$CAIDOS"
  cat <<MAL

  El codigo y la configuracion estan puestos; lo que falla es el arranque.
  Mira el motivo con:

MAL
  for unidad in $CAIDOS; do echo "    journalctl -u $unidad -n 30 --no-pager"; done
  echo
  echo "  Lo mas comun: config.yaml a medias o sin clave del panel."
  echo "  Se comprueba con:  mkbackup --probar-config"
  echo
  exit 1
fi

paso "Listo"

if [ -n "$TLS_PUESTO" ]; then
  verde "  Panel:  https://${IP:-la-ip-de-esta-maquina}/"
  aviso "El certificado se firma a si mismo: el navegador avisara la primera"
  aviso "vez y hay que aceptarlo. Es normal."
else
  verde "  Panel:  http://127.0.0.1:8080/"
fi
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
       vuelve a lanzar este instalador asi:

         curl -fsSL $REPO_CRUDO | MKBACKUP_TLS=1 bash

       Pone nginx delante con TLS. Sin eso el panel habla HTTP en claro y la
       clave viajaria legible: no lo abras con 0.0.0.0 a pelo.

  Comprobar que va:   systemctl status mkbackup mkbackup-web
  Ver el registro:    journalctl -u mkbackup -f
  Actualizar:         vuelve a lanzar este mismo instalador

FIN

[ -n "${IP:-}" ] && aviso "IP de esta maquina: $IP (recuerda el proxy TLS)"
exit 0
