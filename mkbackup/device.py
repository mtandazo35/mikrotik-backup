"""Conexion SSH a un MikroTik y obtencion de sus respaldos.

Por que SSH y no la API (8728/8729): el comando /export de RouterOS esta
disponible SOLO para la CLI. La API expone 'print' por menu, que devuelve datos
estructurados, no el texto de configuracion. Esta documentado por MikroTik en
"Configuration Management". Por eso todas las herramientas del mercado usan SSH
para respaldar configuracion.

Las credenciales pueden venir del equipo (el inventario las admite por fila) o
de config.yaml; se resuelven en _credenciales y en ningun otro sitio. Todo
mensaje que pueda acabar en un log, en estado.json o en Telegram pasa antes por
_censurar. Aviso para quien toque este modulo: no subir el logger 'paramiko' a
DEBUG ni llamar a Transport.set_hexdump(True) en produccion; eso vuelca los
paquetes en crudo, incluido el de autenticacion, y el censor de aqui no lo ve.
"""

from __future__ import annotations

import logging
import re
import socket
import traceback
import unicodedata
from dataclasses import dataclass
from enum import Enum

import paramiko

from .config import Config
from .inventory import NOMBRE_VALIDO, Equipo

log = logging.getLogger("mkbackup.equipo")


class TipoError(str, Enum):
    """Por que fallo. Sirve para que la alerta diga algo util."""

    AUTENTICACION = "autenticacion"
    SIN_CONEXION = "sin_conexion"
    TIMEOUT = "timeout"
    HOST_KEY = "host_key"
    COMANDO = "comando"
    DESCONOCIDO = "desconocido"


class ErrorEquipo(Exception):
    def __init__(self, tipo: TipoError, mensaje: str):
        super().__init__(mensaje)
        self.tipo = tipo
        self.mensaje = mensaje


# --- La excepcion que no se ve venir ----------------------------------------
# Cuando el otro extremo cierra el socket A MITAD de un read(), paramiko no
# lanza SSHException: lanza EOFError pelado desde packet.read_all(). Y EOFError
# NO hereda de SSHException ni de OSError, asi que un bloque que solo mire esas
# dos la deja escapar. Es facil olvidarlo porque es la unica excepcion de red de
# todo paramiko que cuelga directamente de Exception.
#
# Escaparse aqui no es un detalle cosmetico: quien llama (cli) solo sabe
# reintentar lo que llega envuelto en ErrorEquipo, asi que una excepcion cruda
# se traduce en "no se reintenta". Y un enlace WAN que se cae a media sesion es
# JUSTO el fallo que se arregla solo volviendo a intentarlo.
#
# Va como constante y no repetida en cada except para que la proxima excepcion
# de esta familia se anada en un sitio y la vean todos los puntos que hablan por
# la red. ConnectionResetError ya cae en OSError, pero se nombra aqui para que
# el trato de "la conexion se corto" no dependa del orden de los except.
CONEXION_CORTADA = (EOFError, ConnectionResetError, BrokenPipeError)


# --- El saludo que nunca llega ----------------------------------------------
# Un MikroTik con la lista de direcciones puesta ('/ip service set ssh
# address=...') no rechaza la conexion: ACEPTA el TCP y lo cierra acto seguido
# sin mandar su banner. paramiko se encuentra el socket cerrado leyendo el
# saludo y envuelve lo que sea que paso -ConnectionResetError, timeout- en un
# SSHException("Error reading SSH protocol banner..."). Al venir envuelto, ni
# CONEXION_CORTADA ni el except de OSError lo ven: caia en el cajon de
# DESCONOCIDO con el texto "error SSH", que manda a quien lo lee a revisar la
# configuracion de SSH cuando lo que hay que tocar es una lista de acceso.
#
# Merece mensaje propio porque es EL fallo del dia uno: das de alta el servidor
# de respaldos, el router es de un cliente que tiene su ACL puesta, y lo unico
# que se ve es un timeout raro. Con el motivo escrito se arregla en medio minuto.
#
# Se mira el texto y no el tipo porque paramiko no distingue este caso con una
# excepcion propia. Se mira solo 'banner', que es la palabra que pone paramiko
# y no depende de la causa (reset, timeout o cierre limpio dan las tres el mismo
# mensaje). __context__ tampoco sirve de discriminador: cuando el equipo cierra
# sin RST no hay excepcion original que mirar.
def _es_saludo_cortado(exc: Exception) -> bool:
    return "banner" in str(exc).lower()


@dataclass
class Resultado:
    equipo: Equipo
    export: str
    version: str = ""
    binario: bytes | None = None
    # Identidad real del router (/system identity), ya saneada para poder usarse
    # como nombre de archivo. Se rellena durante el respaldo normal, sin abrir
    # otra sesion SSH: la lleva el propio /export. Sirve para renombrar despues
    # los equipos que se dieron de alta sin nombre y quedaron con la IP como
    # nombre provisional. Vacia si no se pudo averiguar.
    identidad: str = ""
    # Modelo del aparato tal cual lo reporta RouterOS ('RB4011iGS+'), ya
    # saneado. Se rellena sin comandos de mas: sale del /system resource print
    # que ya se ejecuta para saber la version. Lo guarda hechos.py para que el
    # panel pueda decir a que equipo hay que ir sin abrirle sesion. Vacio si no
    # se pudo averiguar.
    modelo: str = ""


# --- Limpieza del export ----------------------------------------------------
# RouterOS mete en el /export lineas que cambian en cada ejecucion aunque la
# configuracion sea identica. Si no se filtran, cada respaldo parece un cambio
# y el repo se llena de commits vacios.

_FECHA_V6 = re.compile(r"^#\s\w{3}/\d{2}/\d{4}.*$")
_FECHA_V7 = re.compile(r"^#\s\d{4}-\d{2}-\d{2}.*$")

_RUIDO = (
    re.compile(r"^# inactive time.*$"),
    re.compile(r"^# received packet from \S+ bad format.*$"),
    re.compile(r"^# poe-out status: short_circuit.*$"),
    re.compile(r"^# Firmware upgraded successfully.*$"),
    re.compile(r"^# \S+ not ready.*$"),
    re.compile(r"^# .+ please restart the device in order to apply.*$"),
    re.compile(r"^# no more.*$"),
)


def limpiar_export(texto: str) -> str:
    """Quita el ruido variable para que solo queden cambios reales."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    # RouterOS parte comandos largos con '\' + salto: se reunen para que el
    # diff muestre la linea completa y no fragmentos movidos.
    texto = re.sub(r"\\\n\s+", "", texto)

    salida = []
    for linea in texto.split("\n"):
        if _FECHA_V6.match(linea) or _FECHA_V7.match(linea):
            continue
        if any(patron.match(linea) for patron in _RUIDO):
            continue
        salida.append(linea.rstrip())

    # Quitar lineas en blanco del final, dejando un unico salto
    while salida and not salida[-1]:
        salida.pop()
    return "\n".join(salida) + "\n"


def _version_desde(texto: str) -> str:
    m = re.search(r"version:\s*(\S+)", texto)
    return m.group(1).strip('"') if m else ""


# --- Modelo del equipo ------------------------------------------------------
# El modelo se puede leer de dos sitios que YA estan en el flujo del respaldo, y
# no se ejecuta ni un comando ni una sesion SSH de mas por el:
#
#   1. el '/system resource print' que ya se hace para saber la version, que
#      trae 'board-name: RB4011iGS+';
#   2. la cabecera del /export, que en RouterOS trae '# model = RB4011iGS+'.
#
# Manda board-name, y la cabecera queda como respaldo. El motivo es la fiabilidad
# entre versiones: board-name es un campo del menu /system resource, existe en v6
# y en v7 y RouterOS lo respeta porque hay scripts de medio mundo leyendolo; la
# linea '# model =' es un COMENTARIO del export, se anadio en las v7 (los equipos
# viejos no la tienen) y ya ha cambiado de forma entre versiones. Ademas el
# export pasa por limpiar_export, que descarta lineas de comentario: cualquier
# patron de ruido nuevo podria llevarse el modelo por delante sin que nadie lo
# note. Aun asi se conserva el segundo camino porque cuesta cero y cubre el caso
# de que el resource print venga recortado o denegado por permisos.

# Un modelo de MikroTik son ~20 caracteres ('CCR2004-1G-12S+2XS'). El tope es
# generoso a proposito y solo esta para que un equipo que conteste una parrafada
# no acabe pintando una tabla del panel de un kilometro de ancho.
LARGO_MAX_MODELO = 48

_BOARD_NAME = re.compile(r"^\s*board-name:\s*(.+?)\s*$", re.MULTILINE)
_MODELO_EXPORT = re.compile(r"^#\s*model\s*=\s*(.+?)\s*$", re.MULTILINE)


def sanear_modelo(texto: str) -> str:
    """Deja el modelo apto para guardarlo y pintarlo. '' si no queda nada.

    No se reutiliza sanear_nombre: aquel recorta a lo que admite NOMBRE_VALIDO
    porque su resultado acaba siendo una RUTA dentro del repo git, y ahi un
    caracter raro es un problema de verdad. El modelo no viaja a ningun sistema
    de archivos, solo a un JSON y a una celda del panel, y pasarlo por ese filtro
    destrozaria modelos legitimos ('CRS328-24P-4S+RM' sobrevive, pero cualquier
    equipo futuro con otro simbolo se quedaria en blanco). Aqui basta con quitar
    los caracteres de control (el texto viene del equipo y puede traer lo que
    quiera, incluidos saltos de linea o secuencias de escape de terminal) y
    recortar la longitud.
    """
    limpio = "".join(c for c in (texto or "") if c.isprintable()).strip()
    # Las comillas las pone el propio RouterOS cuando el valor lleva espacios.
    limpio = limpio.strip('"').strip()
    if _TEXTO_DE_ERROR.search(limpio):
        # El equipo contesto una queja, no un modelo (ver _valor_unico).
        return ""
    return limpio[:LARGO_MAX_MODELO]


def _modelo_desde(recursos: str) -> str:
    """Modelo segun '/system resource print'. '' si no aparece."""
    m = _BOARD_NAME.search(recursos or "")
    return sanear_modelo(m.group(1)) if m else ""


def modelo_desde_export(export: str) -> str:
    """Modelo segun la cabecera '# model = ...' del /export. '' si no esta."""
    m = _MODELO_EXPORT.search(export or "")
    return sanear_modelo(m.group(1)) if m else ""


# --- Identidad del equipo ---------------------------------------------------
# El panel web permite dar de alta un router sin nombre: entonces se le pregunta
# al propio equipo como se llama. Ese texto acaba siendo un archivo dentro del
# repo git, asi que hay que sanearlo antes de usarlo.

# Un nombre de archivo largo no rompe nada en Linux (limite 255), pero una
# identidad absurda no deberia decidir el ancho de las rutas del repo.
LARGO_MAX_NOMBRE = 64

# '/system identity print' contesta '  name: MikroTik-Simulado'.
_NOMBRE_PRINT = re.compile(r"^\s*name:\s*(.+?)\s*$", re.MULTILINE)

# En el /export la identidad aparece como '/system identity' + 'set name=X'
# (dos lineas) o, con terse, como '/system identity set name=X' en una sola.
# \s+ cubre ambos casos porque tambien traga el salto de linea.
_NOMBRE_EXPORT = re.compile(
    r"^/system identity\s+set name=(\"[^\"]*\"|\S+)", re.MULTILINE
)

# RouterOS no siempre marca los errores por stderr: a veces contesta por stdout
# con una frase. Sin este filtro, 'expected end of command' pasaria el saneado
# convertido en 'expected-end-of-command' y se guardaria como nombre del equipo.
_TEXTO_DE_ERROR = re.compile(
    r"(syntax error|expected|no such item|bad command|invalid|failure|"
    r"not enough permissions|input does not match)",
    re.IGNORECASE,
)


def sanear_nombre(texto: str) -> str:
    """Deja la identidad de un MikroTik apta para NOMBRE_VALIDO. '' si no queda nada.

    Se aplica el MISMO criterio que sanear_empresa (NFKD para separar la tilde
    de su letra, separadores a guion, se descarta lo demas) porque el problema
    es identico: texto escrito por una persona que va a viajar a una ruta de
    git. Pero no se reutiliza aquella funcion tal cual por dos motivos:

    - sanear_empresa pasa a minusculas. Ahi tiene sentido, porque el slug de la
      empresa es una carpeta que se compara entre clientes y Windows y macOS no
      distinguen mayusculas. Aqui no: el resto del proyecto guarda los nombres
      de equipo como se escribieron ('BTS-Norte-01.rsc') y NOMBRE_VALIDO admite
      mayusculas. Bajarlas cambiaria el aspecto de todo el inventario y ademas
      partiria en dos el historico del equipo que ya estuviera guardado con
      mayusculas en un sistema de archivos que si las distingue.
    - sanear_empresa devuelve EMPRESA_DEFECTO cuando no queda nada usable. Un
      valor por defecto aqui seria peor que nada: todos los routers con
      identidad en cirilico acabarian compartiendo archivo. Devolver '' deja
      que quien llama decida (y el alta se queda con la IP como provisional).
    """
    base = unicodedata.normalize("NFKD", (texto or "").strip())
    base = "".join(c for c in base if not unicodedata.combining(c))

    # 'Router Principal Quito' -> 'Router-Principal-Quito': sin esto se quedaria
    # en 'RouterPrincipalQuito' y se perderia la frontera entre palabras.
    base = re.sub(r"[\s/\\:,;|]+", "-", base)
    base = re.sub(r"[^A-Za-z0-9._-]", "", base)
    base = re.sub(r"-{2,}", "-", base)
    # Windows se come los puntos finales de un archivo, y '.' o '..' como nombre
    # no significan lo que parece.
    base = base.strip("-.")
    base = base[:LARGO_MAX_NOMBRE].strip("-.")

    # Ultima red: lo que salga de aqui se usa como archivo del repo, asi que se
    # comprueba contra la misma expresion que valida el inventario.
    return base if NOMBRE_VALIDO.match(base or "") else ""


def _valor_unico(salida: str) -> str:
    """Devuelve la salida si es UNA sola linea con pinta de valor, si no ''."""
    lineas = [
        linea.strip()
        for linea in salida.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if linea.strip()
    ]
    # Una identidad es una linea. Si vinieron varias, lo que contesto el equipo
    # es una queja, no un nombre.
    if len(lineas) != 1 or _TEXTO_DE_ERROR.search(lineas[0]):
        return ""
    return lineas[0]


def _nombre_desde_print(salida: str) -> str:
    m = _NOMBRE_PRINT.search(salida)
    return m.group(1) if m else ""


def identidad_desde_export(export: str) -> str:
    """Identidad tal cual aparece en el /export (sin sanear). '' si no esta."""
    m = _NOMBRE_EXPORT.search(export)
    return m.group(1).strip('"') if m else ""


# --- Credenciales -----------------------------------------------------------
# Un ISP no entra a todos sus equipos con el mismo usuario: hay clientes que
# imponen sus propias credenciales en sus routers. Por eso el inventario puede
# traerlas por equipo (Equipo.usuario / Equipo.clave) y config.yaml queda como
# el valor heredado por todos los que no declaran nada.


def _credenciales(equipo: Equipo, cfg: Config) -> tuple[str, str, str]:
    """Con que entrar a ESTE equipo: (usuario, password, ruta_clave_privada).

    UN SOLO sitio donde se decide. Antes cada punto que abria sesion leia
    cfg.ssh directamente; con credenciales por equipo eso invitaba a que
    quedara un camino usando las generales y un router contestara "acceso
    denegado" sin que nadie entendiera por que.

    Cada campo se hereda por separado a proposito: un equipo puede traer solo
    el usuario (y compartir la clave del resto) o solo la clave. Un campo vacio
    en el inventario significa "lo que diga config.yaml", no "sin usuario".

    Orden de precedencia:

      usuario  ->  equipo.usuario  si no esta vacio, si no cfg.ssh.usuario
      password ->  equipo.clave    si no esta vacia, si no cfg.ssh.password
      clave privada -> cfg.ssh.clave_privada, PERO se descarta si el equipo
                       trae clave propia.

    Lo ultimo es la parte que hay que pensar. Equipo.clave es una CONTRASENA, y
    cfg.ssh.clave_privada es una llave global. Si se pasaran las dos, paramiko
    intentaria primero la publickey (asi ordena connect los metodos de auth) y
    la contrasena del equipo solo se usaria si la llave falla. En el caso malo
    -la llave global TAMBIEN vale para ese router- entrariamos con una
    identidad distinta de la que pidio el cliente, y la credencial declarada en
    el inventario no se usaria nunca: quedaria puesta pero muerta, y nadie se
    enteraria hasta que el cliente rotara la llave. Una credencial escrita a
    mano para un equipo concreto es la intencion mas explicita que hay, asi que
    gana sobre el ajuste global y para ese equipo se entra SOLO por contrasena.
    """
    usuario = (equipo.usuario or "").strip() or cfg.ssh.usuario
    clave_equipo = equipo.clave or ""

    if clave_equipo:
        return usuario, clave_equipo, ""
    return usuario, cfg.ssh.password, cfg.ssh.clave_privada


def _censurar(texto: str, *secretos: str) -> str:
    """Tapa con '***' cualquier secreto que se haya colado en un texto.

    Todo lo que sale de aqui acaba en sitios que se leen y se reenvian: el log
    del servicio, estado.json (que pinta el panel web) y los mensajes de
    Telegram. Una contrasena de cliente en cualquiera de los tres es un
    incidente, no una molestia.

    Se censura por si acaso, no porque se sepa de un mensaje concreto que la
    lleve: los textos de error de paramiko y las respuestas del propio RouterOS
    no los escribimos nosotros, y basta con que una version futura haga eco del
    comando recibido para que la contrasena del backup binario acabe en el log.
    Es mas barato tapar siempre que auditar cada version de cada dependencia.

    No se exige una longitud minima: si alguien pone una contrasena de un
    caracter, este reemplazo dejara los mensajes ilegibles. Se prefiere un
    mensaje ilegible a una contrasena en el log.
    """
    for secreto in secretos:
        if secreto:
            texto = texto.replace(secreto, "***")
    return texto


class Conexion:
    """Sesion SSH con un MikroTik. Usar como context manager.

    Es el unico punto del proyecto que abre sesion (respaldar e identidad pasan
    por aqui), asi que tambien es el unico que resuelve credenciales y el unico
    que tiene que preocuparse de no escupirlas.
    """

    def __init__(self, equipo: Equipo, cfg: Config):
        self.equipo = equipo
        self.cfg = cfg
        self._cliente: paramiko.SSHClient | None = None

        # Resueltas una sola vez, al construir: si se recalcularan en cada uso
        # habria otra vez varios sitios decidiendo lo mismo.
        self.usuario, self._password, self._clave_privada = _credenciales(equipo, cfg)
        # Credenciales propias del equipo o heredadas: se dice en los mensajes
        # de autenticacion porque es justo el dato que hace falta para
        # diagnosticar, y no revela nada.
        self._propias = bool((equipo.usuario or "").strip() or equipo.clave)

        # Todo lo que NUNCA puede aparecer en un log ni en una excepcion. La
        # del backup binario va aqui tambien: viaja dentro del comando
        # '/system backup save password=...' y ese comando se cita literalmente
        # en los mensajes de error de ejecutar().
        #
        # Va la contrasena RESUELTA, no las dos candidatas: si el equipo trae
        # la suya, la general no llega a salir de este proceso y no hay nada
        # que tapar. Censurar de mas no es gratis, porque _censurar reemplaza
        # sin mirar y destrozaria cualquier mensaje que contenga ese texto por
        # casualidad.
        self._secretos = tuple(
            s for s in (self._password, cfg.binario.password) if s
        )

    def _limpio(self, texto: str) -> str:
        """Texto listo para ir a un mensaje de error o a un log."""
        return _censurar(texto, *self._secretos)

    def __enter__(self) -> "Conexion":
        cliente = paramiko.SSHClient()
        try:
            cliente.load_system_host_keys()
        except (OSError, paramiko.SSHException) as exc:
            # Estaba fuera de todo try: un known_hosts ilegible o con una linea
            # corrupta lanzaba aqui y la excepcion salia cruda de Conexion. Eso
            # no afecta a un equipo, afecta a la flota ENTERA (el archivo es el
            # mismo para todos), asi que conviene que salga clasificado y con el
            # nombre del archivo en el mensaje en vez de como un traceback
            # repetido 300 veces.
            raise ErrorEquipo(
                TipoError.HOST_KEY,
                self._limpio(f"no se pudo leer el known_hosts del sistema: {exc}"),
            ) from exc

        if self.cfg.ssh.host_key_desconocida == "aceptar":
            cliente.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            cliente.set_missing_host_key_policy(paramiko.RejectPolicy())

        # Ojo: la clave privada es la que resolvio _credenciales, no
        # cfg.ssh.clave_privada. Un equipo con contrasena propia llega aqui con
        # la ruta vacia y entra solo por contrasena (ver _credenciales).
        clave = None
        if self._clave_privada:
            try:
                clave = paramiko.Ed25519Key.from_private_key_file(self._clave_privada)
            except paramiko.SSHException:
                try:
                    clave = paramiko.RSAKey.from_private_key_file(self._clave_privada)
                except Exception as exc:  # noqa: BLE001
                    raise ErrorEquipo(
                        TipoError.AUTENTICACION,
                        self._limpio(f"no se pudo leer la clave privada: {exc}"),
                    ) from exc

        origen = "propias del equipo" if self._propias else "generales"
        try:
            cliente.connect(
                hostname=self.equipo.ip,
                port=self.equipo.puerto,
                username=self.usuario,
                password=self._password or None,
                pkey=clave,
                timeout=self.cfg.ssh.timeout,
                banner_timeout=self.cfg.ssh.timeout,
                auth_timeout=self.cfg.ssh.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.AuthenticationException as exc:
            raise ErrorEquipo(
                TipoError.AUTENTICACION,
                f"credenciales rechazadas para el usuario '{self.usuario}' "
                f"({origen})",
            ) from exc
        except paramiko.BadHostKeyException as exc:
            raise ErrorEquipo(
                TipoError.HOST_KEY,
                self._limpio(f"la host key no coincide con la conocida: {exc}"),
            ) from exc
        except socket.timeout as exc:
            raise ErrorEquipo(
                TipoError.TIMEOUT,
                f"sin respuesta en {self.cfg.ssh.timeout}s",
            ) from exc
        except CONEXION_CORTADA as exc:
            # Aqui la red de seguridad de mas abajo ya la tapaba, pero la
            # etiquetaba DESCONOCIDO. Un equipo que corta durante el saludo o el
            # intercambio de claves (un firewall que mata la sesion, un router
            # saturado) es SIN_CONEXION, y eso es lo que hay que leer en la
            # alerta para saber por donde empezar a mirar.
            raise ErrorEquipo(
                TipoError.SIN_CONEXION,
                self._limpio(
                    f"el equipo corto la conexion durante el saludo SSH: "
                    f"{type(exc).__name__}"
                ),
            ) from exc
        except (socket.error, OSError) as exc:
            raise ErrorEquipo(
                TipoError.SIN_CONEXION, self._limpio(f"no se pudo conectar: {exc}")
            ) from exc
        except paramiko.SSHException as exc:
            if _es_saludo_cortado(exc):
                raise ErrorEquipo(
                    TipoError.SIN_CONEXION,
                    self._limpio(
                        f"el equipo acepto la conexion al puerto "
                        f"{self.equipo.puerto} pero la cerro sin identificarse. "
                        f"Lo normal es que solo admita SSH desde ciertas "
                        f"direcciones: revisa en el router "
                        f"'/ip service print' (la columna ADDRESS del servicio "
                        f"ssh) y las reglas de '/ip firewall filter', y anade "
                        f"la IP de este servidor"
                    ),
                ) from exc
            raise ErrorEquipo(
                TipoError.DESCONOCIDO, self._limpio(f"error SSH: {exc}")
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Red de seguridad, y no es teorica: quien llama a respaldar()
            # registra con log.exception() lo que NO sea ErrorEquipo, y eso
            # vuelca el traceback entero con el texto tal cual de la excepcion.
            # connect() es la unica llamada del modulo que recibe la
            # contrasena, asi que ninguna excepcion suya puede salir de aqui
            # sin pasar por el censor. ErrorEquipo, en cambio, se registra
            # siempre por .mensaje y nunca con traceback.
            raise ErrorEquipo(
                TipoError.DESCONOCIDO,
                self._limpio(f"error inesperado al conectar: {exc!r}"),
            ) from exc

        self._cliente = cliente
        return self

    def __exit__(self, *_exc) -> None:
        if self._cliente:
            self._cliente.close()
            self._cliente = None

    def ejecutar(self, comando: str, timeout: int | None = None) -> str:
        if not self._cliente:
            raise ErrorEquipo(TipoError.DESCONOCIDO, "conexion no abierta")

        # El comando se cita en los tres mensajes de error de abajo y uno de
        # los comandos que ejecuta este modulo lleva la contrasena del backup
        # binario dentro ('/system backup save password=...'). Se censura una
        # vez aqui para no depender de acordarse en cada raise.
        visible = self._limpio(comando)
        try:
            _in, out, err = self._cliente.exec_command(
                comando, timeout=timeout or self.cfg.ssh.timeout
            )
            salida = out.read().decode("utf-8", errors="replace")
            error = err.read().decode("utf-8", errors="replace").strip()
        except socket.timeout as exc:
            # Antes que OSError a proposito: socket.timeout ES TimeoutError, que
            # hereda de OSError, asi que el orden inverso se lo tragaria.
            raise ErrorEquipo(
                TipoError.TIMEOUT, f"'{visible}' no respondio a tiempo"
            ) from exc
        except CONEXION_CORTADA as exc:
            # SIN_CONEXION y no COMANDO: el comando no fallo, ni el equipo se
            # quejo de el; lo que se fue fue el enlace. Etiquetarlo como COMANDO
            # mandaria a quien lee la alerta a revisar permisos y sintaxis de
            # RouterOS cuando el problema esta en la red. Y no TIMEOUT: un
            # timeout es "esta ahi pero no contesta", esto es "ya no esta".
            raise ErrorEquipo(
                TipoError.SIN_CONEXION,
                self._limpio(
                    f"la conexion se corto ejecutando '{visible}': "
                    f"{type(exc).__name__}"
                ),
            ) from exc
        except paramiko.SSHException as exc:
            raise ErrorEquipo(
                TipoError.COMANDO, self._limpio(f"'{visible}' fallo: {exc}")
            ) from exc
        except OSError as exc:
            # Un reset o un "host unreachable" a media sesion llega como OSError
            # y sin esto se colaba igual que EOFError.
            raise ErrorEquipo(
                TipoError.SIN_CONEXION,
                self._limpio(f"se perdio la conexion ejecutando '{visible}': {exc}"),
            ) from exc

        if error and "bad command name" in error.lower():
            # 'error' lo escribe el router: si eco el comando, trae el secreto.
            raise ErrorEquipo(
                TipoError.COMANDO, self._limpio(f"'{visible}' no existe: {error}")
            )
        return salida

    # --- Operaciones de respaldo -------------------------------------------

    def obtener_recursos(self) -> str:
        """Salida cruda de '/system resource print'.

        Se expone el texto entero, y no solo la version, porque de esa misma
        respuesta salen dos datos que quiere el panel (version y board-name) y
        pedirla dos veces seria un comando de mas por equipo y por ciclo.
        """
        return self.ejecutar("/system resource print")

    def obtener_version(self) -> str:
        return _version_desde(self.obtener_recursos())

    def obtener_identidad(self, export: str = "") -> str:
        """Nombre del router ya saneado, o '' si no se pudo averiguar.

        Si quien llama ya tiene el /export a mano, que lo pase: la identidad
        viene dentro y sale gratis, sin un comando mas.

        El comando elegido para preguntar es ':put [/system identity get name]'
        y no '/system identity print'. print pinta una tabla pensada para leerla
        una persona ('  name: Router Principal'), con el ancho y las etiquetas
        cambiando entre v6 y v7; :put escribe el valor pelado, sin etiqueta ni
        alineacion, que es justo lo que hay que parsear. Se deja print como
        segundo intento porque ':put' necesita permiso de scripting y hay
        instalaciones que se lo quitan al usuario de solo lectura.
        """
        if export:
            nombre = sanear_nombre(identidad_desde_export(export))
            if nombre:
                return nombre

        for comando, extraer in (
            (":put [/system identity get name]", _valor_unico),
            ("/system identity print", _nombre_desde_print),
        ):
            try:
                crudo = self.ejecutar(comando)
            except ErrorEquipo:
                continue  # se prueba el siguiente; el fallo se decide al final
            nombre = sanear_nombre(extraer(crudo))
            if nombre:
                return nombre

        # Ultimo recurso: el /export siempre trae la identidad si no es la de
        # fabrica. Cuesta lo que un respaldo, por eso va al final y solo si no
        # nos dieron uno ya hecho. Se pide sin version para que no arrastre
        # show-sensitive: para leer un nombre no hacen falta los secretos.
        if not export:
            try:
                return sanear_nombre(identidad_desde_export(self.obtener_export()))
            except ErrorEquipo:
                return ""
        return ""

    def obtener_export(self, version: str = "") -> str:
        """Texto plano de la configuracion, ya normalizado."""
        partes = ["/export"]

        # show-sensitive existe desde RouterOS 7; en v6 el export ya incluye los
        # secretos si el usuario tiene la policy "sensitive".
        if self.cfg.export.mostrar_secretos and version.startswith("7"):
            partes.append("show-sensitive")

        # terse: una linea por comando. Hace los diffs mucho mas legibles.
        if self.cfg.export.terse:
            partes.append("terse")

        crudo = self.ejecutar(" ".join(partes), timeout=max(self.cfg.ssh.timeout, 120))

        if not crudo.strip():
            raise ErrorEquipo(
                TipoError.COMANDO,
                "el export vino vacio (revisa que el usuario tenga policy 'read' "
                "y, para los secretos, 'sensitive')",
            )
        return limpiar_export(crudo)

    def obtener_binario(self) -> bytes:
        """Genera /system backup save y lo descarga por SFTP.

        El .backup SI incluye certificados, claves SSH y passwords de usuarios,
        que /export omite. Es lo que permite restaurar un equipo tal cual.
        """
        if not self._cliente:
            raise ErrorEquipo(TipoError.DESCONOCIDO, "conexion no abierta")

        nombre = "mkbackup"
        remoto = f"{nombre}.backup"

        comando = f"/system backup save name={nombre}"
        if self.cfg.binario.password:
            comando += f" password={self.cfg.binario.password}"
        else:
            # Sin esto RouterOS 7 se queda esperando confirmacion.
            comando += " dont-encrypt=yes"

        salida = self.ejecutar(comando, timeout=max(self.cfg.ssh.timeout, 180))
        if "failure" in salida.lower():
            # La respuesta viene del router y puede repetir el comando entero,
            # con el password= incluido.
            raise ErrorEquipo(
                TipoError.COMANDO,
                self._limpio(f"backup save fallo: {salida.strip()}"),
            )

        try:
            sftp = self._cliente.open_sftp()
        except CONEXION_CORTADA as exc:
            # Abrir el subsistema SFTP es otro viaje de ida y vuelta por la red:
            # si el enlace se cayo mientras se generaba el .backup (que tarda
            # bastante en un equipo cargado), se entera aqui. SIN_CONEXION, no
            # COMANDO: mandar a nadie a revisar la policy 'ftp' cuando lo que
            # paso es que se corto el enlace es hacerle perder la tarde.
            raise ErrorEquipo(
                TipoError.SIN_CONEXION,
                f"la conexion se corto al abrir SFTP: {type(exc).__name__}",
            ) from exc
        except (paramiko.SSHException, OSError) as exc:
            raise ErrorEquipo(
                TipoError.COMANDO,
                f"no se pudo abrir SFTP (revisa que el usuario tenga policy 'ftp'): {exc}",
            ) from exc

        try:
            with sftp.open(remoto, "rb") as fh:
                datos = fh.read()
        except CONEXION_CORTADA as exc:
            # Descargar el .backup es la transferencia mas larga de toda la
            # sesion, o sea la que mas tiempo pasa expuesta a que se caiga el
            # enlace. Sin esta rama, EOFError salia cruda justo en el punto donde
            # mas probable era.
            raise ErrorEquipo(
                TipoError.SIN_CONEXION,
                f"la conexion se corto descargando {remoto}: {type(exc).__name__}",
            ) from exc
        except (IOError, paramiko.SSHException, paramiko.SFTPError) as exc:
            # SFTPError va explicito porque NO hereda de SSHException ni de
            # OSError: cuelga de Exception, igual que EOFError.
            raise ErrorEquipo(
                TipoError.COMANDO,
                f"no se pudo descargar {remoto}: {exc}",
            ) from exc
        finally:
            # Nunca dejar el backup en el equipo: ocupa espacio y es sensible.
            #
            # Se traga CUALQUIER excepcion, no solo IOError: esto corre en un
            # finally, asi que si el enlace ya se corto (EOFError) tanto remove()
            # como close() vuelven a fallar, y una excepcion lanzada desde aqui
            # SUSTITUIRIA al ErrorEquipo bien clasificado que estaba subiendo.
            # Se perderia el diagnostico real y, de paso, el reintento.
            try:
                sftp.remove(remoto)
            except Exception:  # noqa: BLE001
                log.debug("%s: no se pudo borrar %s del equipo", self.equipo, remoto)
            try:
                sftp.close()
            except Exception:  # noqa: BLE001
                pass

        if not datos:
            raise ErrorEquipo(TipoError.COMANDO, "el archivo .backup vino vacio")
        return datos


def identidad(equipo: Equipo, cfg: Config) -> str:
    """Le pregunta al equipo como se llama. Devuelve '' si no se pudo, NUNCA lanza.

    La usa el alta del panel web cuando la persona deja el nombre vacio. Por eso
    no propaga nada: un router apagado, con otra clave o que tarda mas de
    cfg.ssh.timeout no puede tumbar un formulario con un traceback. El alta
    sigue adelante con la IP como nombre provisional y el primer respaldo con
    exito ya trae la identidad en Resultado.identidad para renombrarlo.

    No duplica el codigo de conexion: usa la misma clase Conexion que respaldar,
    que ya aplica cfg.ssh.timeout al connect, al banner, a la autenticacion y a
    cada comando, y que resuelve las credenciales de ESTE equipo (el panel da
    de alta routers de clientes que traen usuario y clave propios; si aqui se
    usaran las generales, el alta sin nombre no averiguaria la identidad de
    justo esos equipos).
    """
    try:
        with Conexion(equipo, cfg) as con:
            nombre = con.obtener_identidad()
    except ErrorEquipo as exc:
        log.warning("%s: no se pudo leer la identidad: %s", equipo, exc.mensaje)
        return ""
    except Exception as exc:  # noqa: BLE001
        # Paranoia deliberada: cualquier rareza de paramiko o del socket que no
        # este envuelta en ErrorEquipo tampoco puede reventar el alta.
        #
        # El traceback se formatea a mano en vez de usar log.exception porque
        # este es el unico camino que vuelca texto de excepciones ajenas, y ese
        # texto no lo controlamos. Formatearlo permite pasarlo por el censor y
        # seguir teniendo el rastro completo para depurar.
        detalle = _censurar(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            *(s for s in (_credenciales(equipo, cfg)[1], cfg.binario.password) if s),
        )
        log.error("%s: error inesperado leyendo la identidad:\n%s", equipo, detalle)
        return ""

    if not nombre:
        log.info("%s: el equipo no dio una identidad usable", equipo)
    return nombre


def respaldar(equipo: Equipo, cfg: Config, con_binario: bool = True) -> Resultado:
    """Obtiene el respaldo completo de un equipo. Lanza ErrorEquipo si falla."""
    with Conexion(equipo, cfg) as con:
        # Una sola llamada para las dos cosas que trae: la version manda en como
        # se pide el export (show-sensitive solo existe en v7) y el modelo va al
        # archivo de hechos que lee el panel.
        recursos = con.obtener_recursos()
        version = _version_desde(recursos)
        modelo = _modelo_desde(recursos)
        export = con.obtener_export(version)
        # Segundo camino para el modelo, gratis: si el resource print no traia
        # board-name, la cabecera del export que ya tenemos puede traerlo.
        if not modelo:
            modelo = modelo_desde_export(export)
        # La identidad sale del export que ya tenemos: cero comandos extra en el
        # caso normal. Asi el renombrado de un equipo dado de alta sin nombre no
        # necesita abrir una segunda sesion SSH contra un equipo que igual esta
        # al otro lado de un enlace lento.
        nombre_real = con.obtener_identidad(export=export)
        binario = None
        if con_binario and cfg.binario.activo:
            binario = con.obtener_binario()
        return Resultado(
            equipo=equipo,
            export=export,
            version=version,
            binario=binario,
            identidad=nombre_real,
            modelo=modelo,
        )
