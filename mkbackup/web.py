"""Panel web: ver como va el respaldo, gestionar la flota y revisar cambios.

Empezo siendo una pantalla de solo lectura para responder "esta corriendo o
no". Ahora tambien da de alta equipos y ensena el historial, asi que conviene
dejar por escrito que sigue sin hacer y por que:

  - NO lanza respaldos. Un boton "respaldar ahora" convierte un panel de
    consulta en algo que toca 300 equipos de golpe. El disparo es del
    programador (planificador.py), y desde aqui solo se cambia cada cuanto.
  - NO edita la configuracion general ni las rutas. Eso vive en config.yaml,
    que se edita a mano con root. El panel escribe solo tres cosas, todas bajo
    /root/mkbackup: el inventario, los ajustes del programador y las cuentas.
    Si algun dia lo comprometen, no puede apuntar los respaldos a otro sitio.
  - NO muestra los secretos de las configuraciones. El diff tapa passwords,
    PSK y secrets (ver historial.py). Se ve QUE cambio, no lo que dice.

Todo pide login menos /salud (ver sesion.py). Aunque no sirviera ninguna
configuracion, la lista de nombres, IPs y empresas de la flota es un mapa de
la red.

Sobre los permisos: cada peticion resuelve su cuenta contra el archivo de
usuarios, no contra lo que se guardo en el token. Es mas trabajo por peticion,
pero significa que bajarle el rol a alguien surte efecto en la siguiente
pagina que pida, y no cuando le caduque la sesion. La navegacion esconde lo
que un rol no puede usar, y ademas cada ruta lo comprueba: esconder el boton
no es proteger nada, cualquiera puede escribir la URL.

Sobre HTTPS: esto habla HTTP en claro. En 127.0.0.1 da igual; si lo abres a la
LAN, pon un proxy con TLS delante o la clave viaja legible.

Usa http.server de la libreria estandar: ni una dependencia mas en un servicio
desatendido, igual que notify.py con urllib.
"""

from __future__ import annotations

import json
import logging
import ipaddress
import os
import secrets
import shutil
import threading
import time
import unicodedata
from functools import partial
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import historial as hist
from . import identidades
from . import importar as imp
from . import mudanza
from . import paginas
from .config import Config, zona_horaria
from .estado import resumen
from .hechos import Hechos
from .inventory import (
    ErrorInventario,
    cargar,
    guardar,
    validar_equipo,
)
from . import imagen
from .planificador import estado_programador, pedir_ciclo
from .sesion import COOKIE, Sesiones
from .store import Almacen, ErrorAlmacen, leer_replica
from .auditoria import Auditoria
from .usuarios import (
    EDITAR,
    ETIQUETAS_PERMISO,
    PERMISOS,
    VER,
    Alcance,
    ErrorUsuarios,
    Usuarios,
)

log = logging.getLogger("mkbackup.web")


def _texto(valor) -> str:
    """Clave de orden para texto: sin tildes y sin distinguir mayusculas.

    Sin esto, "Ñandu" se iria detras de "Zeta" y "acme" detras de "Zeta"
    tambien, porque se compararia por el codigo del caracter. Quien ordena una
    lista espera el orden del diccionario, no el de la tabla de caracteres.
    """
    crudo = unicodedata.normalize("NFKD", str(valor or ""))
    return "".join(c for c in crudo if not unicodedata.combining(c)).casefold()


def _clave_ip(valor: str):
    """Ordena las IPs por su valor y no como texto.

    Como texto, 10.20.0.9 va DESPUES de 10.20.0.10, que es justo lo que nadie
    espera al ordenar una columna de direcciones. Los nombres DNS no son
    numeros, asi que van despues de todas las IPs y entre ellos por texto.
    """
    try:
        return (0, int(ipaddress.ip_address(valor.strip())), "")
    except ValueError:
        return (1, 0, _texto(valor))


# Como se ordena cada columna. El nombre va siempre de desempate.
CLAVES_ORDEN = {
    "nombre": lambda e: (_texto(e.nombre),),
    "empresa": lambda e: (_texto(e.empresa), _texto(e.nombre)),
    "ip": lambda e: (_clave_ip(e.ip), _texto(e.nombre)),
    "puerto": lambda e: (e.puerto, _texto(e.nombre)),
    "grupo": lambda e: (_texto(e.grupo), _texto(e.nombre)),
    # Un intervalo 0 significa "el general": se va al final, que es donde
    # menos molesta cuando lo que buscas son los que tienen uno propio.
    "cada": lambda e: (e.intervalo_minutos == 0, e.intervalo_minutos, _texto(e.nombre)),
}

# Un formulario de estos son cuatro campos: mas de esto es alguien probando.
MAXIMO_FORMULARIO = 8 * 1024
# Un inventario de 300 equipos en xlsx no llega a 100 KB. El margen es amplio,
# pero acotado: esto lo sube alguien por HTTP y se lee entero en memoria.
MAXIMO_SUBIDA = 4 * 1024 * 1024

# La imagen de fondo tiene su propio tope, mas alto: la idea es que se pueda
# subir la foto tal como sale del movil o de la camara y que el sistema la
# ajuste (ver imagen.py). Una foto de movil son 3-8 MB y una de reflex puede
# pasar de 20, asi que con 4 MB habia que editarla antes, que es justo lo que
# se queria evitar.
#
# Sigue habiendo tope, y alto no quiere decir libre: esto se lee ENTERO en
# memoria antes de tocarlo, y son varios hilos a la vez. 30 MB por subida es
# caro pero asumible; sin limite, tres peticiones simultaneas se llevan por
# delante un servidor de 2 GB.
#
# Ojo si hay un proxy delante: nginx corta en client_max_body_size (1 MB por
# defecto) y devolveria un 413 suyo antes de que esto llegue a verse.
MAXIMO_IMAGEN = 30 * 1024 * 1024

# Tope de la copia del servidor que se sube para restaurar. Es mucho mas alto
# que los demas y se lo puede permitir porque este NO se lee en memoria: se
# vuelca a disco segun llega (ver _recibir_paquete). Lo que lo acota de verdad
# es el disco, que se comprueba antes de aceptar nada.
#
# El tope existe igual, porque una subida sin limite es una forma comoda de
# llenarle el disco a un servidor. Por encima de esto se restaura desde el
# terminal, que no pasa por aqui.
#
# Ojo con el proxy: nginx corta en client_max_body_size (1 MB por defecto) y
# devolveria su propio 413 mucho antes que esto.
MAXIMO_MUDANZA = 1024 * 1024 * 1024

# Como se llaman los paquetes subidos que aun no se han confirmado. El prefijo
# empieza por punto para que no salga en un listado normal, y sirve para
# encontrarlos y borrarlos: son copias completas del sistema, con todas sus
# credenciales, esperando una confirmacion que puede no llegar nunca.
PREFIJO_SUBIDA = ".mudanza-subida-"

# Lo que hay que escribir para reemplazar el servidor entero. En mayusculas y
# sin acentos: hay que teclearlo a conciencia, que es justo el punto.
PALABRA_RESTAURAR = "RESTAURAR"

# Tipos de imagen que se aceptan como fondo del login. Lista blanca: el tipo
# que se declara sale de la EXTENSION, no del contenido, asi que se limita a
# formatos de imagen y nunca se sirve algo como text/html, que el navegador
# ejecutaria en el mismo origen que el panel.
# NADA de SVG, aunque sea una imagen: un SVG puede llevar <script> dentro y
# /fondo se sirve en el mismo origen que el panel, asi que abrir esa URL
# ejecutaria ese codigo con la sesion de quien la abra. Un formato de imagen
# que ademas es un documento ejecutable no cabe aqui.
TIPOS_IMAGEN = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".avif": "image/avif",
}

# Se comprueba el CONTENIDO y no solo la extension: renombrar un .html a .jpg
# es trivial, y el tipo que se declara al servirlo sale de la extension. Sin
# esto se podria servir un documento desde el origen del panel.
FIRMAS_IMAGEN = (
    # Escritas como numeros y no como escapes de texto: asi no hay forma de
    # que una edicion futura las estropee sin que se note.
    (bytes([0xFF, 0xD8, 0xFF]), "image/jpeg"),
    (bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]), "image/png"),
)


def _tipo_real(datos: bytes) -> str:
    """El tipo de imagen segun sus primeros bytes, o cadena vacia."""
    for firma, tipo in FIRMAS_IMAGEN:
        if datos.startswith(firma):
            return tipo
    # WebP y AVIF llevan el tipo dentro de un contenedor, no al principio.
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "image/webp"
    if datos[4:8] == b"ftyp" and b"avif" in datos[8:24]:
        return "image/avif"
    return ""


# --- De donde viene de verdad la peticion -----------------------------------
# El despliegue que este proyecto recomienda (panel en 127.0.0.1 y un proxy con
# TLS delante) hace que TODAS las peticiones lleguen desde la direccion del
# proxy. Con client_address a secas eso rompe dos cosas que si importan:
#
#   - el freno a la fuerza bruta pasa a ser un cubo unico para todo el mundo.
#     Cinco fallos de cualquiera dejan fuera a la empresa entera durante
#     bloqueo_segundos: de proteccion pasa a ser la forma mas comoda de tumbar
#     el panel.
#   - el registro de accesos, que se pidio justamente para saber quien intenta
#     entrar, escribe 127.0.0.1 en todas sus lineas.
#
# La regla es no creerse NUNCA una cabecera que escribe quien llama, salvo que
# venga por un camino declarado de antemano. Si la conexion no viene de un
# proxy de la lista, X-Forwarded-For se ignora entera. Si viene, se recorre de
# derecha a izquierda y se coge el primer salto que NO sea uno de los nuestros:
# los de mas a la derecha los escribio nuestra propia infraestructura, y todo
# lo que hay mas a la izquierda lo pudo inventar el cliente.


def _ip_de(client_address, cabeceras, de_confianza) -> str:
    """La direccion a la que se le apuntan los intentos y las lineas del log."""
    directa = client_address[0] if client_address else ""
    if not de_confianza or directa not in de_confianza:
        return directa

    reenviada = cabeceras.get("X-Forwarded-For", "")
    for salto in reversed([s.strip() for s in reenviada.split(",") if s.strip()]):
        if salto not in de_confianza:
            # Se recorta: esto viaja al registro de auditoria y al panel, y lo
            # escribe quien llama. Auditoria ya limpia los caracteres de
            # control; el largo se acota aqui para que una cabecera de 8 KB no
            # se convierta en 8 KB por linea del log.
            return salto[:64]
    return directa


class Contexto:
    """Lo que comparten todas las peticiones, en un solo sitio.

    El candado merece explicacion: dar de alta un equipo es leer el CSV,
    anadir y reescribirlo. `inventory.guardar` es atomico, pero dos altas
    simultaneas leerian la misma lista y la segunda se llevaria por delante a
    la primera. Con ThreadingHTTPServer eso no es hipotetico: cada pestana es
    un hilo.
    """

    def __init__(self, cfg: Config, sesiones: Sesiones):
        self.cfg = cfg
        self.sesiones = sesiones
        self.usuarios = Usuarios(cfg.almacen.usuarios)
        self.hechos = Hechos(cfg.almacen.hechos)
        self.auditoria = Auditoria(cfg.almacen.auditoria)
        self.candado = threading.Lock()
        # Se resuelve una vez al arrancar y no en cada peticion: cargar la
        # zona horaria lee la base de datos del sistema, y esto se usa en
        # cada fila de cada tabla.
        self.zona = zona_horaria(cfg.zona_horaria)


# --- Utilidades de peticion -------------------------------------------------


def _limpiar(cuerpo: bytes, frontera: bytes) -> list[tuple[str, bytes]]:
    """Trocea un multipart/form-data en pares (nombre_de_campo, contenido).

    Se hace a mano porque el modulo `cgi`, que era lo que servia para esto,
    esta retirado desde Python 3.13 y arrastrarlo ahora seria heredar una
    deuda. Solo hace falta lo justo: subir un archivo de inventario.
    """
    partes: list[tuple[str, bytes]] = []
    separador = b"--" + frontera
    for trozo in cuerpo.split(separador):
        trozo = trozo.strip(b"\r\n")
        if not trozo or trozo == b"--":
            continue
        cabeceras, _, datos = trozo.partition(b"\r\n\r\n")
        if not _:
            continue
        nombre = ""
        for linea in cabeceras.decode("utf-8", "replace").splitlines():
            if linea.lower().startswith("content-disposition:"):
                for pieza in linea.split(";"):
                    pieza = pieza.strip()
                    if pieza.startswith("name="):
                        nombre = pieza[5:].strip('"')
                    elif pieza.startswith("filename="):
                        # El nombre del archivo se pasa como un campo mas: el
                        # importador decide por la extension si es csv o xlsx.
                        partes.append(("__archivo__", pieza[9:].strip('"').encode()))
        if nombre:
            partes.append((nombre, datos))
    return partes


class Manejador(BaseHTTPRequestHandler):
    """Enrutado. Lo que se ve esta en paginas.py; lo que se decide, aqui."""

    protocol_version = "HTTP/1.1"
    server_version = "mkbackup"

    # Sin esto, una conexion que se abre y NO habla se queda con su hilo para
    # siempre. Con HTTP/1.1 y keep-alive no hace falta mala fe: basta un
    # navegador que deja pestanas abiertas o un monitor que no cierra. Y con
    # mala fe son diez lineas de script para dejar el panel sin hilos justo el
    # dia que hay que mirar por que no se respaldo la flota.
    #
    # 30s es de sobra para cualquier peticion real del panel (la mas lenta es
    # un diff de 2300 lineas, que se sirve en menos de uno) y corto para una
    # conexion que no dice nada.
    timeout = 30

    # La cuenta de la peticion en curso; la resuelve do_GET/do_POST.
    usuario = None
    # El cuerpo del POST con todos los valores por nombre; lo llena _campos.
    campos_lista: dict = {}
    sys_version = ""

    def __init__(self, *args, ctx: Contexto, **kwargs):
        self.ctx = ctx
        super().__init__(*args, **kwargs)

    @property
    def cfg(self) -> Config:
        return self.ctx.cfg

    # --- Respuestas ---------------------------------------------------------

    def _responder(
        self,
        cuerpo: bytes,
        tipo: str,
        codigo: int = 200,
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        self._cabeceras(codigo, tipo, len(cuerpo), extra)
        self.wfile.write(cuerpo)

    def _cabeceras(
        self,
        codigo: int,
        tipo: str,
        largo: int,
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        """Las cabeceras de CUALQUIER respuesta. Separadas del cuerpo a proposito.

        Existe aparte porque hay una respuesta que no puede pasar por
        _responder: la copia del servidor entero (ver _mudanza), que se manda
        leyendola del disco por trozos. Con el historico de una flota grande son
        cientos de megas, y tenerlos que juntar en memoria para poder pasarlos
        como bytes tumbaria el panel en una maquina pequena.

        Lo que NO se hace es que esa respuesta se escriba sus propias cabeceras:
        se le olvidaria una, y las que hay aqui son las que impiden que el panel
        cargue nada externo, que lo empotren en un iframe o que una copia con las
        credenciales de la flota se quede en la cache del disco.
        """
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(largo))
        # El estado cambia cada pocos segundos y las paginas van tras login:
        # nada de esto debe quedarse en una cache de disco.
        if not any(c.lower() == "cache-control" for c, _ in extra or []):
            self.send_header("Cache-Control", "no-store")
        # El panel no carga nada externo; esto lo deja por escrito.
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Que nadie lo empotre en un iframe para robar clics sobre la sesion.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        # Si algo decidio antes que esta conexion no sigue (ver _error), hay que
        # DECIRLO. Poner solo el atributo cierra el socket pero sin avisar, y el
        # navegador se entera intentando reutilizar una conexion que ya no esta:
        # una peticion perdida y un reintento, en vez de abrir otra y ya. La
        # cabecera es lo unico que convierte "se corto" en "se cerro".
        if self.close_connection:
            self.send_header("Connection", "close")
        for clave, valor in extra or []:
            self.send_header(clave, valor)
        self.end_headers()

    def _html(self, pagina: str, codigo: int = 200) -> None:
        self._responder(pagina.encode("utf-8"), "text/html; charset=utf-8", codigo)

    def _json(self, datos, codigo: int = 200) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self._responder(cuerpo, "application/json; charset=utf-8", codigo)

    def _redirigir(self, destino: str, extra=None) -> None:
        # 303 y no 302: tras un POST, el navegador debe hacer GET al destino.
        # Asi recargar la pagina no repite el alta ni la baja.
        self._responder(
            b"", "text/plain; charset=utf-8", 303,
            [("Location", destino), *(extra or [])],
        )

    def _error(self, codigo: int, texto: str) -> None:
        # Se cierra la conexion al contestar un error, y no es por cortesia.
        # Casi todos los caminos que llegan aqui cortan ANTES de leer el cuerpo
        # del POST ("no tienes permiso", "el panel es de solo lectura", "esa
        # pagina no existe"). Con HTTP/1.1 la conexion se queda abierta, y lo
        # que quede sin leer en el socket lo interpreta la peticion SIGUIENTE
        # como su primera linea: el navegador ve la conexion caerse a la mitad
        # mientras el registro del servidor dice que se contesto bien.
        #
        # Ya paso una vez por un formulario que no leia su cuerpo. La respuesta
        # entonces fue arreglar ese formulario; esto lo cierra para todos los
        # errores de golpe, que es donde el descuido es mas probable porque el
        # camino de error es justo el que nadie prueba a mano.
        self.close_connection = True
        self._html(paginas.error(codigo, texto), codigo)

    # --- Sesion -------------------------------------------------------------

    def _token(self) -> str | None:
        cabecera = self.headers.get("Cookie", "")
        if not cabecera:
            return None
        try:
            galletas = SimpleCookie(cabecera)
        except Exception:  # noqa: BLE001  (SimpleCookie lanza CookieError y mas)
            return None
        galleta = galletas.get(COOKIE)
        return galleta.value if galleta else None

    def _usuario(self):
        """La cuenta de esta peticion, o None si no hay sesion valida.

        Se resuelve del archivo en CADA peticion y no se guarda en el token:
        asi bajarle el rol a alguien surte efecto de inmediato, y una cuenta
        borrada deja de valer aunque su token siguiera vivo.
        """
        # El refresco automatico NO cuenta como actividad. La pantalla de
        # Estado pregunta a /api/estado cada pocos segundos ella sola: si eso
        # reiniciara el reloj de inactividad, bastaria con dejar una pestana
        # abierta para que la sesion no caducara jamas, y el tope de media hora
        # no protegeria de nada. Se comprueba el permiso igual; lo unico que no
        # se hace es dar por vivo a quien no esta.
        ruta = urlparse(self.path).path
        nombre = self.ctx.sesiones.valida(
            self._token(), refrescar=not ruta.startswith("/api/")
        )
        if not nombre:
            return None
        return self.ctx.usuarios.obtener(nombre)

    def _para_pintar(self, usuario) -> dict:
        """Lo que paginas.py necesita saber de la sesion, sin acoplarse."""
        if usuario is None:
            return {}
        return {
            "nombre": usuario.nombre,
            "rol": usuario.rol,
            "etiqueta": usuario.rol,
            "puede": self.ctx.usuarios.permisos(usuario),
            "todo": usuario.alcance.todo,
        }

    @property
    def ip(self) -> str:
        """De donde viene la peticion, mirando el proxy si hay uno declarado.

        UN SOLO sitio donde se decide, igual que las credenciales en device.py:
        el freno a la fuerza bruta y el registro de accesos tienen que estar de
        acuerdo, porque si no se bloquea una direccion y se apunta otra.
        """
        return _ip_de(
            self.client_address, self.headers, self.cfg.web.proxies_de_confianza
        )

    def _anotar(self, evento: str, detalle: str = "", usuario: str = "") -> None:
        """Deja constancia del evento con quien lo hizo y desde donde."""
        self.ctx.auditoria.anotar(
            evento,
            usuario=usuario or getattr(self.usuario, "nombre", ""),
            ip=self.ip,
            detalle=detalle,
        )

    def _a_donde_puede(self, sesion: dict) -> None:
        """Manda a la primera pantalla que esta cuenta si puede abrir.

        El orden es el de la navegacion (paginas.SECCIONES), asi que a donde se
        llega es la primera pestana que se ve arriba: lo mismo que habria
        pulsado la persona. Y sale de la MISMA tabla que pinta el menu, para
        que no puedan decir cosas distintas.
        """
        permisos = sesion.get("puede", set())
        for destino, _clave, _texto, permiso in paginas.SECCIONES:
            if destino != "/" and permiso in permisos:
                self._redirigir(destino)
                return

        # Ninguna. Es una cuenta a la que no se le dio ni un permiso de ver, y
        # merece que se le diga eso y no un "no tienes permiso para esto" sobre
        # una pantalla concreta, que manda a pedir el permiso equivocado.
        log.warning("%s entro y no puede ver ninguna pantalla",
                    getattr(self.usuario, "nombre", "?"))
        self._anotar("sin_pantallas")
        self._error(
            403,
            "Tu cuenta ha entrado bien, pero no tiene permiso para ver ninguna "
            "pantalla. Pidele a un administrador que te de al menos uno.",
        )

    def _permite(self, accion: str) -> bool:
        """Corta la peticion con un 403 si a la cuenta le falta ese permiso.

        Se comprueba aqui, en el servidor, y no solo escondiendo botones: la
        navegacion oculta lo que no se puede usar, pero cualquiera puede
        escribir la URL a mano.

        A una ruta de /api/ se le contesta JSON. Parece cosmetico y no lo es:
        el JS que consume esas rutas pregunta cada pocos segundos, y una pagina
        de error en HTML no le dice nada que sepa interpretar, asi que sigue
        preguntando. Con una pestana abierta a la que le quitan el permiso en
        caliente, eso son miles de peticiones al dia, y CADA UNA escribe una
        linea de auditoria: el registro rota y se lleva por delante los eventos
        de verdad. Un fallo de permisos que acaba borrando las pruebas.
        """
        if self.usuario is not None and self.ctx.usuarios.puede(self.usuario, accion):
            return True
        log.warning(
            "%s intento '%s' sin permiso",
            getattr(self.usuario, "nombre", "?"), accion,
        )
        self._anotar("sin_permiso", f"{accion} en {self.path.split('?')[0]}")
        if urlparse(self.path).path.startswith("/api/"):
            self.close_connection = True
            self._json({"error": "sin permiso"}, 403)
        else:
            self._error(403, "Tu cuenta no tiene permiso para esto.")
        return False

    # --- Alcance: sobre QUE equipos vale lo que puede hacer -----------------
    #
    # Todo lo que devuelve datos de equipos pasa por aqui. Es a proposito que
    # sean tres lineas y esten juntas: en un panel multitenant, el dia que un
    # camino nuevo se salte este filtro, un cliente ve la red de otro. Si
    # anades una ruta que lea equipos, tiene que llamar a _visibles o a _ve.

    def _ve(self, equipo) -> bool:
        return self.usuario.alcance.puede_ver(equipo.empresa, equipo.nombre)

    def _edita(self, equipo) -> bool:
        return self.usuario.alcance.puede_editar(equipo.empresa, equipo.nombre)

    def _visibles(self, equipos):
        return [e for e in equipos if self._ve(e)]

    def _alcanzable(self, equipo, escribir: bool = False) -> bool:
        """Comprueba el alcance sobre UN equipo y responde si no llega.

        Cuando no se puede ver, la respuesta es 404 y no 403: decir "no tienes
        permiso" sobre un nombre concreto ya confirma que ese equipo existe, y
        en multitenant eso es informacion del cliente de al lado.
        """
        if not self._ve(equipo):
            log.warning(
                "%s intento alcanzar '%s' (%s), fuera de su alcance",
                self.usuario.nombre, equipo.nombre, equipo.empresa,
            )
            self._anotar("fuera_de_alcance", f"{equipo.nombre} ({equipo.empresa})")
            self._error(404, "No hay ningun equipo con ese nombre.")
            return False
        if escribir and not self._edita(equipo):
            self._error(403, f"Solo puedes consultar los equipos de {equipo.empresa}.")
            return False
        return True

    @staticmethod
    def _cookie(token: str, borrar: bool = False) -> tuple[str, str]:
        # HttpOnly: si algun dia se cuela un XSS, al menos no se lleva la sesion.
        # SameSite=Strict: nadie puede hacer que tu navegador use tu sesion desde
        # otra pagina. Es tambien lo que protege las altas y bajas de un CSRF:
        # un POST desde otro sitio llega sin cookie y muere en el login.
        # Sin Secure a proposito: el panel habla HTTP y el navegador descartaria
        # la cookie. Si lo pones tras un proxy TLS, ese es el sitio de anadirlo.
        #
        # Y SIN Max-Age al entrar: eso la convierte en una cookie de sesion del
        # navegador, que se borra al cerrarlo. Con Max-Age, la cookie sobrevivia
        # a cerrar el navegador y volver a abrirlo, asi que en un ordenador
        # compartido el siguiente que abriera el panel entraba como el anterior.
        # El tope de verdad no es este de todas formas: el token vive en el
        # servidor y caduca alli (ver Sesiones). Esto solo hace que el navegador
        # no se lo guarde mas de la cuenta.
        #
        # Nota honesta: los navegadores con "restaurar pestanas al arrancar"
        # pueden conservar las cookies de sesion entre arranques. Por eso el
        # tope por inactividad del servidor es el que manda.
        caduca = "; Max-Age=0" if borrar else ""
        return (
            "Set-Cookie",
            f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict{caduca}",
        )

    # --- Entrada de datos ---------------------------------------------------

    def _campos(self) -> dict[str, str] | None:
        """Cuerpo de un POST normal. None si ya se respondio un error.

        El cuerpo hay que leerlo entero aunque no sirva: con keep-alive, lo
        que quede sin leer lo interpreta la siguiente peticion como su
        cabecera.
        """
        try:
            largo = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            largo = -1
        if largo < 0 or largo > MAXIMO_FORMULARIO:
            self.close_connection = True
            self._error(400, "Peticion invalida o demasiado grande.")
            return None
        crudo = self.rfile.read(largo).decode("utf-8", errors="replace")
        # Se guarda la version con TODOS los valores porque un grupo de
        # casillas manda el mismo nombre repetido, y quedarse con el primero
        # (que es lo que hace el dict de abajo) perderia el resto.
        self.campos_lista = parse_qs(crudo, keep_blank_values=True)
        return {k: v[0] for k, v in self.campos_lista.items()}

    def _tragar_cuerpo(self) -> bool:
        """Lee y descarta el cuerpo del POST. False si ya se contesto con error.

        Un formulario SIN campos manda Content-Length: 0 y aqui no hay nada que
        leer, asi que da la sensacion de que sobra. No sobra: con HTTP/1.1 la
        conexion se queda abierta, y lo que quede sin leer en el socket lo
        interpreta la peticion SIGUIENTE como su primera linea. El sintoma no
        es un error legible, es la conexion cayendose a la mitad.

        Se llama tambien desde los formularios vacios de hoy porque el dia que
        alguien les anada una casilla, esa casilla ya viaja: acordarse entonces
        no es un plan.
        """
        return self._campos() is not None

    def _consulta_lista(self, clave: str) -> list[str]:
        """Todos los valores de un parametro repetido en la URL.

        `_consulta` se queda con el primero, que es lo correcto para un filtro;
        pero las columnas llegan como `col=a&col=b&col=c` y ahi hacen falta
        todas.
        """
        return parse_qs(urlparse(self.path).query, keep_blank_values=True).get(clave, [])

    def _consulta(self) -> dict[str, str]:
        partido = urlparse(self.path)
        return {
            k: v[0] for k, v in parse_qs(partido.query, keep_blank_values=True).items()
        }

    # --- Inventario ---------------------------------------------------------

    def _inventario(self):
        return cargar(self.cfg.inventario, self.cfg.ssh.puerto_defecto)

    def _guardar_inventario(self, equipos) -> None:
        guardar(self.cfg.inventario, equipos)

    # --- GET ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802  (lo impone http.server)
        ruta = urlparse(self.path).path.rstrip("/") or "/"

        # Abierta sin sesion, y hace falta que lo este: es la hoja de estilos de
        # la propia pantalla de entrada, donde todavia no hay sesion que valga.
        # No dice nada de la flota, es el mismo CSS para todo el mundo.
        #
        # Se cachea un ano. Eso solo se puede hacer porque la direccion lleva la
        # huella del contenido (ver paginas.MARCA_ESTILO): si el CSS cambia,
        # cambia la direccion, y la version vieja deja de pedirse. Sin esa
        # huella, cachear seria condenar a quien ya entro a no ver nunca un
        # arreglo de estilos.
        if ruta == "/estilo.css":
            self._responder(
                paginas.ESTILO.encode("utf-8"), "text/css; charset=utf-8", 200,
                [("Cache-Control", "public, max-age=31536000, immutable")],
            )
            return

        # Abierta sin sesion y a proposito: es la ruta que mira un monitor
        # externo para saber si el proceso vive. No dice nada de la flota, solo
        # que el panel responde; pedirle credenciales obligaria a repartirlas
        # por cada sistema de monitoreo.
        if ruta == "/salud":
            self._responder(b"ok\n", "text/plain; charset=utf-8")
            return

        # Abierta sin sesion por necesidad: es el fondo de la pantalla de
        # entrada, y ahi todavia no hay sesion que valga. Solo se sirve el
        # archivo que diga la configuracion, que es de root; no hay forma de
        # pedir otro distinto desde la URL.
        if ruta == "/fondo":
            self._fondo()
            return

        # Una sola vez por peticion: lee el archivo de usuarios y se reutiliza
        # en el enrutado, en los permisos y al pintar.
        self.usuario = self._usuario()
        cfg_fondo = self.cfg.web.fondo_login

        if ruta == "/entrar":
            if self.usuario is not None:
                self._redirigir("/")
            else:
                self._html(paginas.login(fondo=self._marca_fondo()))
            return

        if self.usuario is None:
            if ruta.startswith("/api/"):
                # Al JS se le responde 401 y el se va al login; un 303 aqui le
                # daria el HTML del formulario como si fuera el estado.
                self._json({"error": "sin sesion"}, 401)
            else:
                self._redirigir("/entrar")
            return

        try:
            self._rutas_get(ruta)
        except ErrorInventario as exc:
            self._error(500, f"No se pudo leer el inventario: {exc}")
        except hist.ErrorHistorial as exc:
            self._error(400, str(exc))
        except ErrorUsuarios as exc:
            self._error(500, f"No se pudo leer el archivo de usuarios: {exc}")

    def _rutas_get(self, ruta: str) -> None:
        cfg = self.cfg
        sesion = self._para_pintar(self.usuario)

        if ruta == "/":
            # Quien no puede ver Estado NO se encuentra un 403 nada mas entrar:
            # se le lleva a la primera pantalla que si puede usar. Con permisos
            # finos esto deja de ser un caso raro y pasa a ser lo normal -"que
            # solo vea la lista de equipos" es justo para lo que se partieron
            # los permisos-, y aterrizar en una pagina de error al escribir bien
            # la clave se lee como que el panel no funciona, no como que a esa
            # cuenta le falta un permiso.
            if "estado.ver" not in sesion.get("puede", set()):
                self._a_donde_puede(sesion)
                return
            self._html(
                paginas.panel(sesion, cfg.web.refresco, cfg.zona_horaria)
            )

        elif ruta == "/api/estado":
            # El mismo permiso que la pantalla que lo pinta: esta ruta ES el
            # contenido de Estado. Sin comprobarlo, quien no puede abrir la
            # pantalla se lleva igual las cifras pidiendo el JSON a mano.
            if not self._permite("estado.ver"):
                return
            datos = self._estado_visible()
            # El proximo ciclo lo publica el programador en su propio archivo:
            # se pega aqui para que el panel haga una sola peticion, recortado
            # al alcance igual que el resto.
            datos["programador"] = self._programador_visible()
            self._json(datos)

        elif ruta == "/api/identidades":
            # Mismo permiso que el boton que lo lanza: lo que devuelve son los
            # nombres que van diciendo los routers, o sea el resultado del
            # sondeo. Quien no puede pedirlo tampoco tiene por que leerlo.
            if not self._permite("equipos.identidades"):
                return
            self._api_identidades()

        elif ruta == "/equipos":
            if not self._permite("equipos.ver"):
                return
            equipos, avisos = self._inventario_visible()
            filtro = self._filtro()
            consulta = self._consulta()
            columna = consulta.get("orden", "")
            descendente = consulta.get("dir") == "desc"
            visibles = self._ordenar(
                self._filtrar(equipos, filtro), columna, descendente
            )
            columnas = paginas.columnas_validas(
                self._consulta_lista("col"),
                paginas.COLUMNAS_EQUIPOS, paginas.COLUMNAS_EQUIPOS_DEFECTO,
            )
            self._html(
                paginas.equipos(
                    visibles, avisos,
                    # El interruptor CRUDO del modo solo lectura, sin mezclarlo
                    # con ningun permiso: la pagina ya decide boton a boton con
                    # los permisos de la sesion. Mezclados aqui, una cuenta que
                    # puede ANADIR pero no editar se quedaba sin el boton de
                    # anadir, porque el permiso de editar apagaba los dos.
                    cfg.web.editar_inventario,
                    self._consulta().get("ok", ""),
                    empresas=self._valores(equipos, "empresa"),
                    grupos=self._valores(equipos, "grupo"),
                    filtro=filtro, total_sin_filtrar=len(equipos), sesion=sesion,
                    orden=columna, descendente=descendente,
                    columnas=columnas, hechos=self.ctx.hechos.leer(),
                    zona=self.ctx.zona,
                )
            )

        elif ruta == "/equipos/nuevo":
            if not self._editable("equipos.crear"):
                return
            self._html(paginas.formulario_equipo({}, [], True, sesion=sesion))

        elif ruta == "/equipos/editar":
            if not self._editable("equipos.editar"):
                return
            nombre = self._consulta().get("nombre", "")
            equipos, _ = self._inventario_tolerante()
            actual = next((e for e in equipos if e.nombre == nombre), None)
            if actual is None:
                self._error(404, f"No hay ningun equipo llamado '{nombre}'.")
                return
            if not self._alcanzable(actual, escribir=True):
                return
            datos = {
                "nombre": actual.nombre, "empresa": actual.empresa,
                "ip": actual.ip, "puerto": actual.puerto, "grupo": actual.grupo,
                "intervalo": actual.intervalo_minutos or "",
                "usuario": actual.usuario,
                # La clave a proposito NO va: ver la pista del formulario.
            }
            self._html(
                paginas.formulario_equipo(datos, [], False, actual.nombre, sesion)
            )

        elif ruta == "/importar":
            if not self._editable("equipos.importar"):
                return
            self._html(paginas.importar(imp.hay_soporte_xlsx(), sesion=sesion))

        elif ruta == "/importar/plantilla":
            # Solo 'equipos.ver' y NO 'equipos.importar': lo que se descarga es
            # una hoja vacia con las columnas y las instrucciones, sin un solo
            # dato de la flota. No hace falta poder importar para mirar que
            # formato pide el sistema (por ejemplo, para prepararle el archivo
            # a quien si puede subirlo).
            if not self._permite("equipos.ver"):
                return
            self._plantilla()

        elif ruta == "/cambios":
            if not self._permite("cambios.ver"):
                return
            self._cambios()

        elif ruta == "/historial":
            # Es la ficha de UN equipo, asi que pide ver equipos y no ver
            # cambios: se llega desde la lista de la flota, y lo que ensena son
            # las versiones de ese equipo concreto.
            if not self._permite("equipos.ver"):
                return
            self._historial()

        elif ruta == "/diferencia":
            self._diferencia()

        elif ruta == "/ajustes":
            if not self._permite("ajustes.ver"):
                return
            self._pintar_ajustes()

        elif ruta == "/usuarios":
            if not self._permite("usuarios.ver"):
                return
            cuentas = self.ctx.usuarios.listar()
            self._html(paginas.usuarios(
                cuentas, self.ctx.usuarios.roles(),
                {u.nombre: self.ctx.usuarios.permisos(u) for u in cuentas},
                self.usuario.nombre, sesion, self._consulta().get("ok", ""),
                zona=self.ctx.zona,
            ))

        elif ruta == "/usuarios/nuevo":
            # El formulario pide ya el permiso de CREAR, igual que en equipos:
            # rellenar un alta que despues se va a rechazar no es informacion,
            # es hacerle perder el trabajo a quien la escribio.
            if not self._permite("usuarios.crear"):
                return
            self._html(self._pintar_formulario_usuario(
                {"rol": "lector", "permisos": self.ctx.usuarios.roles().get("lector", []),
                 "alcance": {}}, [], True,
            ))

        elif ruta == "/usuarios/rol":
            # Mismo permiso que el POST que lo guarda: esta pantalla es el
            # editor de un rol, no una ficha de consulta.
            if not self._permite("roles.editar"):
                return
            nombre = self._consulta().get("rol", "")
            roles = self.ctx.usuarios.roles()
            self._html(paginas.formulario_rol(
                nombre, roles.get(nombre, []), PERMISOS, ETIQUETAS_PERMISO, [],
                nombre not in roles, self._cuantos_con_rol(nombre), sesion,
            ))

        elif ruta == "/usuarios/editar":
            # El formulario solo pide VER: quien no pueda guardar se topara con
            # el 403 en el POST, pero la ficha de una cuenta (su rol, sus
            # permisos y su alcance) es justo lo que hay que poder consultar
            # para saber a quien pedirle un cambio.
            if not self._permite("usuarios.ver"):
                return
            self._editar_usuario()

        elif ruta == "/auditoria":
            if not self._permite("auditoria.ver"):
                return
            consulta = self._consulta()
            try:
                desde = max(0, int(consulta.get("desde", "0")))
            except ValueError:
                # La pagina viene de la URL y puede traer cualquier cosa; una
                # letra ahi no puede ser un error, se empieza por el principio.
                desde = 0
            eventos, total, desde = self.ctx.auditoria.buscar(
                desde=desde,
                cuantos=cfg.web.eventos_por_pagina,
                evento=consulta.get("evento", ""),
                usuario=consulta.get("usuario", ""),
                solo_sospechosos=consulta.get("sospechosos") == "1",
            )
            self._html(paginas.auditoria(
                eventos, self.ctx.auditoria.usuarios_vistos(),
                consulta, sesion, zona=self.ctx.zona,
                desde=desde, total=total,
                por_pagina=cfg.web.eventos_por_pagina,
                vigiladas=self.ctx.sesiones.vigiladas(),
                bloqueo_minutos=max(1, cfg.web.bloqueo_segundos // 60),
                mensaje=consulta.get("ok", ""),
            ))

        elif ruta == "/cuenta":
            # Sin comprobacion de rol: cambiar la clave PROPIA lo puede hacer
            # cualquiera, incluido el de solo lectura. Es su cuenta.
            self._html(paginas.cuenta(
                self.usuario.nombre, self.usuario.rol, [], sesion
            ))

        else:
            self._error(404, "Esa pagina no existe.")

    def _editar_usuario(self) -> None:
        nombre = self._consulta().get("nombre", "")
        actual = self.ctx.usuarios.obtener(nombre)
        if actual is None:
            self._error(404, f"No hay ninguna cuenta llamada '{nombre}'.")
            return
        aviso = ""
        if actual.nombre == self.usuario.nombre:
            # Quitarse permisos a uno mismo es la forma mas facil de perder el
            # acceso a esta pantalla sin darse cuenta.
            aviso = ("Es tu propia cuenta: si te quitas permisos o te recortas "
                     "el alcance, perderas ese acceso tu tambien.")
        self._html(self._pintar_formulario_usuario(
            {
                "nombre": actual.nombre,
                "rol": actual.rol,
                "permisos": sorted(self.ctx.usuarios.permisos(actual)),
                "alcance": actual.alcance.como_dict(),
            },
            [], False, aviso,
        ))

    def _marca_fondo(self) -> str:
        """Marca que cambia cuando cambia el archivo de fondo.

        Se compone del tamano y de la fecha de modificacion: es barato (una
        llamada al sistema) y basta para lo que hace falta, que es que la URL
        cambie cuando cambie la imagen. Vacia si no hay fondo configurado o no
        se puede leer, y entonces la pantalla de entrada sale sin imagen.
        """
        if not self.cfg.web.fondo_login:
            return ""
        try:
            info = Path(self.cfg.web.fondo_login).stat()
        except OSError:
            return ""
        return f"{int(info.st_mtime)}-{info.st_size}"

    def _fondo(self) -> None:
        """Sirve la imagen de fondo del login, si hay una configurada."""
        configurado = self.cfg.web.fondo_login
        if not configurado:
            self._responder(b"", "text/plain; charset=utf-8", 404)
            return

        archivo = Path(configurado)
        tipo = TIPOS_IMAGEN.get(archivo.suffix.lower())
        if tipo is None:
            log.warning(
                "web.fondo_login apunta a %s, que no es una imagen de las que "
                "se sirven (%s)", archivo, ", ".join(sorted(TIPOS_IMAGEN))
            )
            self._responder(b"", "text/plain; charset=utf-8", 404)
            return

        try:
            datos = archivo.read_bytes()
        except OSError as exc:
            # Que falte el fondo no puede impedir entrar al panel: se registra
            # y la pantalla de entrada sale sin imagen.
            log.warning("No se pudo leer el fondo del login %s: %s", archivo, exc)
            self._responder(b"", "text/plain; charset=utf-8", 404)
            return

        # Aqui si interesa que el navegador la cachee: es la misma imagen en
        # cada visita y pesa mucho mas que el resto de la pagina junta. El
        # resto del panel sigue con no-store, que es lo correcto para datos.
        self._responder(
            datos, tipo, 200,
            [("Cache-Control", "public, max-age=86400")],
        )

    # --- Piezas de GET ------------------------------------------------------

    def _inventario_tolerante(self):
        """El inventario completo, o vacio si todavia no hay ninguno.

        Un inventario inexistente NO es un error en el panel: es justo el
        estado de quien acaba de instalar esto y viene a dar de alta su primer
        equipo. Reventar con un 500 ahi seria dejarle sin la unica pantalla
        que le sirve.
        """
        try:
            return self._inventario()
        except ErrorInventario as exc:
            return [], [str(exc)]

    def _equipos_del_estado(self, datos: dict) -> list[dict]:
        """La flota de AHORA con el resultado que tenga cada uno del ultimo ciclo.

        La lista manda el INVENTARIO, no el archivo de estado, y esto es lo que
        hay que entender: el archivo de estado es la foto del ultimo ciclo, y
        entre ese ciclo y ahora la flota puede haber cambiado.

        Pasaban las dos cosas, y las dos se veian como una cifra que no cuadra
        con la realidad:

          - se da de baja un equipo y Estado seguia contandolo durante horas,
            hasta el siguiente ciclo. Con un solo equipo en la flota, el panel
            decia "1 equipo, 1 respaldado" sobre un router que ya no existe.
          - se da de alta uno y NO aparecia hasta que le tocara turno, asi que
            el panel decia "3 equipos" cuando en Equipos habia 4.

        Ahora se parte de lo que hay y se le pega encima lo que se sepa: un
        equipo recien dado de alta sale como PENDIENTE, que es exactamente lo
        que es, y uno dado de baja desaparece en el acto.
        """
        try:
            equipos, _ = self._inventario_visible()
        except ErrorInventario:
            # Sin inventario legible no se puede decir cual es la flota. Se cae
            # a lo que diga el estado: es dato viejo, pero es mejor que una
            # pantalla en blanco cuando lo que falla es el CSV.
            return [
                e for e in datos.get("equipos", [])
                if self.usuario.alcance.puede_ver(
                    e.get("empresa", ""), e.get("nombre", ""))
            ]

        del_ciclo = {e.get("nombre", ""): e for e in datos.get("equipos", [])}
        salida = []
        for equipo in equipos:
            visto = del_ciclo.get(equipo.nombre)
            if visto is not None:
                # La empresa y el grupo se toman del inventario y no de la
                # foto: si a un equipo le cambiaron de cliente esta manana, la
                # tarta tiene que pintarlo donde esta ahora.
                salida.append({
                    **visto,
                    "empresa": equipo.empresa,
                    "grupo": equipo.grupo,
                    "ip": equipo.ip,
                })
            else:
                salida.append({
                    "nombre": equipo.nombre, "ip": equipo.ip,
                    "grupo": equipo.grupo, "empresa": equipo.empresa,
                    "estado": "pendiente", "detalle": "", "intento": 0, "fin": "",
                })
        return salida

    def _estado_visible(self) -> dict:
        """El estado de la ejecucion, recortado al alcance de esta cuenta.

        No basta con filtrar la lista de equipos: los contadores que trae el
        archivo (total, ok, cambios, fallidos) son de la flota entera, y las
        cifras y las graficas del panel se pintan con ellos. Un cliente que ve
        12 equipos pero un total de 300 ya sabe el tamano de su proveedor.
        Por eso se recalculan sobre lo que si le corresponde.
        """
        datos = resumen(self.cfg.almacen.estado)
        suyos = self._equipos_del_estado(datos)

        def cuantos(estado):
            return sum(1 for e in suyos if e.get("estado") == estado)

        ok = cuantos("sin_cambios") + cuantos("cambio")
        fallidos = cuantos("fallo")
        recalculado = {
            "equipos": suyos,
            "total": len(suyos),
            "ok": ok,
            "cambios": cuantos("cambio"),
            "fallidos": fallidos,
            "hechos": ok + fallidos,
        }

        if self.usuario.alcance.todo:
            return {**datos, **recalculado}

        # Lista BLANCA, no negra. Antes se devolvia el archivo entero con unos
        # cuantos campos recalculados encima, y por ahi se colaban la duracion
        # del ciclo de toda la flota, la concurrencia configurada y el pid.
        # Con una lista negra, cada campo nuevo que se anada al estado sale
        # publicado por descuido; con una blanca, hay que anadirlo aqui a mano.
        return {
            "situacion": datos.get("situacion"),
            **recalculado,
            # Sin historial de ejecuciones (son totales de la flota entera) ni
            # avisos del inventario (nombran equipos de otras empresas).
            "historial": [],
            "avisos_inventario": [],
        }

    def _programador_visible(self) -> dict:
        """Lo que esta cuenta puede saber del programador.

        `estado_programador` devuelve, entre otras cosas, la ultima consulta de
        CADA equipo de la flota y el tamano del ultimo lote. Pegado tal cual,
        un cliente veia los nombres de los equipos de los demas y cuantos hay
        en total. Aqui se recorta a lo suyo.
        """
        prog = estado_programador(self.cfg)
        if self.usuario.alcance.todo:
            return prog

        mios = {e.nombre for e in self._inventario_visible()[0]}
        return {
            # Cuando toca el proximo ciclo si es util saberlo: es lo que dice
            # si sus respaldos siguen vivos.
            "proxima": prog.get("proxima"),
            "corriendo": prog.get("corriendo"),
            "ultimas": {
                nombre: cuando
                for nombre, cuando in (prog.get("ultimas") or {}).items()
                if nombre in mios
            },
        }

    def _inventario_visible(self):
        """El inventario recortado a lo que esta cuenta puede ver."""
        equipos, avisos = self._inventario_tolerante()
        if self.usuario.alcance.todo:
            return equipos, avisos
        # Los avisos del inventario nombran equipos de toda la flota (lineas
        # descartadas, nombres duplicados, choques de empresa): a quien solo ve
        # una empresa no le corresponden, y ademas delatan que hay mas.
        return self._visibles(equipos), []

    def _editable(self, accion: str = "equipos.editar") -> bool:
        """Si esta peticion puede tocar el inventario.

        Dos condiciones distintas: que el panel lo permita (configuracion) y
        que la cuenta tenga ese permiso. Se separan porque los mensajes tienen
        que decir cual de las dos falla; "no tienes permiso" cuando en realidad
        esta apagado para todo el mundo manda a buscar el problema al sitio
        equivocado.
        """
        if not self.cfg.web.editar_inventario:
            self._error(403, "El panel esta configurado en modo solo lectura.")
            return False
        return self._permite(accion)

    def _plantilla(self) -> None:
        formato = self._consulta().get("formato", "csv")
        try:
            if formato == "xlsx":
                datos = imp.plantilla_xlsx()
                tipo = ("application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")
                nombre = "inventario-mkbackup.xlsx"
            else:
                datos = imp.plantilla_csv()
                tipo = "text/csv; charset=utf-8"
                nombre = "inventario-mkbackup.csv"
        except imp.ErrorImportacion as exc:
            self._error(503, str(exc))
            return
        self._responder(
            datos, tipo, 200,
            [("Content-Disposition", 'attachment; filename="' + nombre + '"')],
        )

    def _historial(self) -> None:
        nombre = self._consulta().get("equipo", "")
        equipos, _ = self._inventario_tolerante()
        actual = next((e for e in equipos if e.nombre == nombre), None)
        if actual is None:
            # Dado de baja. NO es un error, y ensenar un 404 aqui es enganoso:
            # sus respaldos siguen en el repositorio a proposito ("dar de baja
            # es dejar de consultarlo, no perder lo que se sabia de el"), y de
            # hecho se acaba de llegar desde Cambios, donde salen listados. Lo
            # unico que ya no existe es la ficha del inventario, que es de
            # donde sale la ruta de sus archivos.
            #
            # Se vuelve a Cambios diciendolo, que es de donde se venia y donde
            # sus versiones se siguen viendo una a una.
            log.info("Historial de '%s': ya no esta en el inventario", nombre)
            self._redirigir("/cambios?ok=" + quote(
                f"'{nombre}' ya no esta en el inventario, asi que no tiene ficha "
                "con su historial. Sus respaldos siguen aqui: cada version es "
                "una fila de esta tabla."
            ))
            return
        if not self._alcanzable(actual):
            return
        versiones = hist.versiones(
            self.cfg.almacen.git, actual.ruta_relativa, self.cfg.web.historial_maximo
        )
        self._html(
            paginas.historial(
                actual.nombre, actual.ruta_relativa, versiones,
                self.cfg.web.ver_diferencias, zona=self.ctx.zona,
                sesion=self._para_pintar(self.usuario),
            )
        )

    def _diferencia(self) -> None:
        if not self.cfg.web.ver_diferencias:
            self._error(403, "Ver diferencias esta desactivado (web.ver_diferencias).")
            return
        if not self._permite("diferencias"):
            return
        consulta = self._consulta()
        ruta = consulta.get("ruta", "")
        commit = consulta.get("commit", "")

        # La ruta viene del navegador y apunta a un archivo del repositorio, no
        # a un equipo. Hay que traducirla al equipo del inventario para poder
        # decidir el alcance: sin esto, escribir a mano la ruta de otra empresa
        # ensenaria su configuracion entera.
        equipos, _ = self._inventario_tolerante()
        duenyo = next((e for e in equipos if e.ruta_relativa == ruta), None)

        if duenyo is None:
            # La ruta no esta en el inventario. Puede ser de un equipo
            # RENOMBRADO: sus versiones anteriores viven bajo el nombre viejo,
            # y sin esto quien no lo ve todo se encontraba un 404 en el
            # historial de su propio equipo, que es exactamente el "se pierde
            # todo" que se venia a arreglar.
            #
            # El historial manda ademas de que equipo es. Con eso se comprueba
            # el alcance sobre el equipo de VERDAD y despues que la ruta pedida
            # sea una de las suyas segun git. Lo segundo es lo que impide que
            # esto se convierta en un agujero: no basta con nombrar un equipo
            # propio para leer un archivo cualquiera del repositorio.
            actual = next(
                (e for e in equipos if e.nombre == consulta.get("equipo", "")),
                None,
            )
            if actual is not None and self._alcanzable(actual):
                historicas = {
                    v.ruta for v in hist.versiones(
                        self.cfg.almacen.git, actual.ruta_relativa,
                        self.cfg.web.historial_maximo,
                    ) if v.ruta
                }
                if ruta in historicas:
                    duenyo = actual

            if duenyo is None and not self.usuario.alcance.todo:
                self._error(404, "No hay ninguna version con esa ruta.")
                return
        elif not self._alcanzable(duenyo):
            return

        # historial.diferencia valida ruta y commit antes de pasarlos a git;
        # aqui no se sanean a medias para no acabar con dos criterios distintos.
        lineas = hist.diferencia(
            self.cfg.almacen.git, ruta, commit, self.cfg.web.ocultar_secretos
        )
        equipo = ruta.split("/")[-1].removesuffix(".rsc")
        oculto = any(getattr(l, "oculta", False) for l in lineas)
        self._html(paginas.diferencia(equipo, ruta, commit, lineas, oculto,
                                      self._para_pintar(self.usuario)))

    # --- Filtros ------------------------------------------------------------

    def _filtro(self) -> dict:
        c = self._consulta()
        return {
            "empresa": c.get("empresa", "").strip(),
            "grupo": c.get("grupo", "").strip(),
            "q": c.get("q", "").strip(),
        }

    @staticmethod
    def _valores(equipos, campo: str):
        """Los valores distintos de un campo, para llenar un desplegable."""
        return sorted({getattr(e, campo) for e in equipos if getattr(e, campo)})

    @staticmethod
    def _coincide(equipo, filtro: dict) -> bool:
        if filtro["empresa"] and equipo.empresa != filtro["empresa"]:
            return False
        if filtro["grupo"] and equipo.grupo != filtro["grupo"]:
            return False
        texto = filtro["q"].lower()
        # El buscador libre mira nombre e IP a la vez: quien escribe "10.20."
        # busca una subred y quien escribe "BTS" busca por nombre, y obligarle
        # a elegir en que campo busca solo estorba.
        if texto and texto not in equipo.nombre.lower() and texto not in equipo.ip.lower():
            return False
        return True

    def _filtrar(self, equipos, filtro: dict):
        return [e for e in equipos if self._coincide(e, filtro)]

    @staticmethod
    def _ordenar(equipos, columna: str, descendente: bool):
        """Ordena la tabla de equipos por la columna pedida.

        Cada clave termina en el nombre del equipo como desempate. Sin eso,
        al ordenar por empresa los equipos de una misma empresa saldrian en el
        orden en que esten en el archivo, que es arbitrario: justo lo que se
        quiere evitar al pulsar "ordenar".
        """
        if columna not in CLAVES_ORDEN:
            return equipos
        return sorted(equipos, key=CLAVES_ORDEN[columna], reverse=descendente)

    # Cuantos commits se leen del repositorio para armar la lista de Cambios.
    # No es lo mismo que cuantos se ENSENAN (eso es cambios_por_pagina): de este
    # monton se descuenta lo que no se puede ver y lo que no pasa el filtro, y
    # lo que queda se pagina. Alto para que haya varias paginas que recorrer,
    # acotado porque cada commit es trabajo de git y esto se pide en cada carga.
    CAMBIOS_LEIDOS = 500

    def _cambios(self) -> None:
        equipos, _ = self._inventario_visible()
        filtro = self._filtro()
        por_ruta = {e.ruta_relativa: e for e in equipos}
        lista = hist.cambios(self.cfg.almacen.git, self.CAMBIOS_LEIDOS)

        # Un equipo dado de baja ya no esta en el inventario, asi que no se le
        # puede calcular el alcance. Solo lo ve quien lo ve todo; para los
        # demas es como si no existiera, que es lo seguro.
        if not self.usuario.alcance.todo:
            lista = [(r, v) for r, v in lista if r in por_ruta]

        if any(filtro.values()):
            lista = [
                (ruta, v) for ruta, v in lista
                if ruta in por_ruta and self._coincide(por_ruta[ruta], filtro)
            ]

        # La pagina que se pide. Viene de la URL, asi que puede traer cualquier
        # cosa: una letra ahi no puede ser un error, se empieza por el principio.
        try:
            desde = max(0, int(self._consulta().get("desde", "0")))
        except ValueError:
            desde = 0
        total = len(lista)
        por_pagina = self.cfg.web.cambios_por_pagina
        # Si se pide una pagina que ya no existe (se filtro despues de avanzar),
        # se vuelve a la ultima con contenido en vez de ensenar una tabla vacia.
        if desde >= total:
            desde = max(0, (max(0, total - 1) // por_pagina) * por_pagina)
        pagina = lista[desde:desde + por_pagina]

        self._html(
            paginas.cambios(
                pagina, self.cfg.web.ver_diferencias, zona=self.ctx.zona,
                desde=desde, total=total, por_pagina=por_pagina,
                por_ruta=por_ruta, empresas=self._valores(equipos, "empresa"),
                grupos=self._valores(equipos, "grupo"), filtro=filtro,
                sesion=self._para_pintar(self.usuario),
                columnas=paginas.columnas_validas(
                    self._consulta_lista("col"),
                    paginas.COLUMNAS_CAMBIOS, paginas.COLUMNAS_CAMBIOS_DEFECTO,
                ),
                aviso=self._consulta().get("ok", ""),
            )
        )

    # --- POST ---------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        ruta = urlparse(self.path).path.rstrip("/") or "/"

        self.usuario = self._usuario()

        if ruta == "/entrar":
            self._entrar()
            return

        if self.usuario is None:
            self._redirigir("/entrar")
            return

        if ruta == "/salir":
            self._anotar("salir")
            self.ctx.sesiones.cerrar(self._token())
            # Max-Age=0 borra la cookie: sin esto queda un token muerto dando
            # vueltas en el navegador.
            self._redirigir("/entrar", [self._cookie("", borrar=True)])
            return

        try:
            self._rutas_post(ruta)
        except ErrorInventario as exc:
            self._error(500, f"No se pudo guardar el inventario: {exc}")
        except ErrorUsuarios as exc:
            # El archivo de cuentas esta corrupto o no se puede escribir. Es
            # grave y hay que decirlo entero: si se tragara el error, quien
            # acaba de crear una cuenta creeria que existe.
            log.error("Archivo de usuarios: %s", exc)
            self._error(500, f"No se pudo guardar el archivo de usuarios: {exc}")

    def _rutas_post(self, ruta: str) -> None:
        if ruta == "/equipos/nuevo":
            self._alta()
        elif ruta == "/equipos/editar":
            self._edicion()
        elif ruta == "/equipos/baja":
            self._baja()
        elif ruta == "/importar":
            self._importacion()
        elif ruta == "/ajustes":
            if self._permite("ajustes.programador"):
                self._ajustes()
        elif ruta == "/ajustes/respaldar":
            if self._permite("ajustes.respaldar"):
                self._respaldar_ahora()
        elif ruta == "/ajustes/identidades":
            # Sin comprobar aqui: el permiso lo mira el propio manejador, que
            # ademas necesita _editable (renombra equipos del inventario).
            self._identidades_masivo()
        elif ruta == "/ajustes/remoto":
            if self._permite("ajustes.remoto"):
                self._ajustes_remoto()
        elif ruta == "/ajustes/remoto/probar":
            # Probar es parte de configurar a donde se sube: quien no puede
            # cambiar el remoto tampoco tiene por que hacerle hablar al panel
            # con el servidor de fuera.
            if self._permite("ajustes.remoto"):
                self._probar_remoto()
        elif ruta == "/ajustes/ssh":
            if self._permite("ajustes.ssh"):
                self._ajustes_ssh()
        elif ruta == "/ajustes/fondo":
            if self._permite("ajustes.fondo"):
                self._ajustes_fondo()
        elif ruta == "/ajustes/fondo/quitar":
            if self._permite("ajustes.fondo"):
                self._ajustes_fondo_quitar()
        elif ruta == "/ajustes/mudanza":
            self._mudanza()
        elif ruta == "/ajustes/mudanza/subir":
            self._subir_mudanza()
        elif ruta == "/ajustes/mudanza/restaurar":
            self._restaurar_mudanza()
        elif ruta == "/ajustes/datos/borrar":
            # Igual que identidades: lo comprueba el manejador, y con _editable
            # porque tira datos de la flota.
            self._borrar_datos()
        elif ruta == "/usuarios/nuevo":
            if self._permite("usuarios.crear"):
                self._alta_usuario()
        elif ruta == "/usuarios/editar":
            if self._permite("usuarios.editar"):
                self._editar_usuario_post()
        elif ruta == "/usuarios/baja":
            if self._permite("usuarios.baja"):
                self._baja_usuario()
        elif ruta == "/usuarios/rol":
            if self._permite("roles.editar"):
                self._rol_post()
        elif ruta == "/usuarios/rol/baja":
            if self._permite("roles.baja"):
                self._rol_baja()
        elif ruta == "/auditoria/desbloquear":
            if self._permite("auditoria.desbloquear"):
                self._desbloquear()
        elif ruta == "/cuenta":
            self._cambiar_mi_clave()
        else:
            self._error(404, "Esa pagina no existe.")

    # --- Usuarios -----------------------------------------------------------

    def _alcance_del_formulario(self) -> Alcance:
        """Reconstruye el alcance a partir de los campos del formulario.

        Los niveles llegan como `empresa:<nombre>` y `equipo:<nombre>` para no
        tener que mandar listas paralelas que se puedan descuadrar entre si.
        Solo se guardan los que tienen nivel: un desplegable en "sin acceso" no
        deja rastro, y asi el archivo no crece con una linea por cada empresa
        que alguien NO puede ver.
        """
        empresas, equipos = {}, {}
        for clave, valores in self.campos_lista.items():
            nivel = (valores[0] or "").strip()
            if nivel not in (VER, EDITAR):
                continue
            if clave.startswith("empresa:"):
                empresas[clave[len("empresa:"):]] = nivel
            elif clave.startswith("equipo:"):
                equipos[clave[len("equipo:"):]] = nivel
        return Alcance(
            todo=self.campos_lista.get("todo", [""])[0] == "1",
            empresas=empresas, equipos=equipos,
        )

    def _datos_formulario_usuario(self, campos: dict) -> dict:
        """Lo tecleado, para poder repintar el formulario si algo falla."""
        return {
            "nombre": campos.get("nombre", "").strip(),
            "rol": campos.get("rol", ""),
            "permisos": self.campos_lista.get("permisos", []),
            "alcance": self._alcance_del_formulario().como_dict(),
        }

    def _pintar_formulario_usuario(self, datos, errores, alta, aviso=""):
        equipos, _ = self._inventario_tolerante()
        return paginas.formulario_permisos(
            datos, self.ctx.usuarios.roles(), PERMISOS, ETIQUETAS_PERMISO,
            self._valores(equipos, "empresa"), equipos, errores, alta,
            self._para_pintar(self.usuario), aviso,
        )

    def _alta_usuario(self) -> None:
        campos = self._campos()
        if campos is None:
            return
        datos = self._datos_formulario_usuario(campos)
        rol = datos["rol"]
        # Los permisos se guardan como diferencia contra el rol, no como lista
        # suelta: asi, si manana se le anade un permiso al rol, lo hereda quien
        # no lo tenga tocado a mano. Lo marcado que el rol no da entra como
        # "mas"; lo que el rol da y no esta marcado, como "menos".
        del_rol = set(self.ctx.usuarios.roles().get(rol, ()))
        marcados = set(datos["permisos"])
        errores = self.ctx.usuarios.crear(
            datos["nombre"], campos.get("clave", ""), rol,
            permisos_mas=sorted(marcados - del_rol),
            permisos_menos=sorted(del_rol - marcados),
            alcance=self._alcance_del_formulario(),
        )
        if errores:
            self._html(self._pintar_formulario_usuario(datos, errores, True))
            return
        log.info(
            "%s creo la cuenta '%s' (rol %s, alcance %s)",
            self.usuario.nombre, datos["nombre"], rol,
            "toda la flota" if datos["alcance"].get("todo") else "acotado",
        )
        self._anotar("usuario_alta", f"{datos['nombre']} con rol {rol}")
        aviso = f"Cuenta {datos['nombre']} creada."
        self._redirigir(f"/usuarios?ok={quote(aviso)}")

    def _editar_usuario_post(self) -> None:
        campos = self._campos()
        if campos is None:
            return
        datos = self._datos_formulario_usuario(campos)
        nombre = datos["nombre"]
        actual = self.ctx.usuarios.obtener(nombre)
        if actual is None:
            self._error(404, f"No hay ninguna cuenta llamada '{nombre}'.")
            return

        rol = datos["rol"]
        del_rol = set(self.ctx.usuarios.roles().get(rol, ()))
        marcados = set(datos["permisos"])
        errores = self.ctx.usuarios.actualizar(
            nombre, rol=rol,
            permisos_mas=sorted(marcados - del_rol),
            permisos_menos=sorted(del_rol - marcados),
            alcance=self._alcance_del_formulario(),
        )
        clave = campos.get("clave", "")
        # Vacia significa "no la toques", igual que en el formulario de
        # equipos: si no, abrir la pantalla y guardar le borraria la clave.
        if not errores and clave:
            errores = self.ctx.usuarios.cambiar_clave(nombre, clave)

        if errores:
            self._html(self._pintar_formulario_usuario(datos, errores, False))
            return

        # Cambiarle permisos, rol o alcance a alguien tiene que echarlo de sus
        # sesiones: si no, sigue dentro con lo de antes hasta que le caduque.
        cerradas = self.ctx.sesiones.cerrar_usuario(nombre)
        log.info(
            "%s actualizo la cuenta '%s' (%d sesiones cerradas)",
            self.usuario.nombre, nombre, cerradas,
        )
        self._anotar(
            "usuario_cambio",
            f"{nombre}: rol {rol}" + (", clave nueva" if clave else ""),
        )
        if nombre == self.usuario.nombre:
            # Se acaba de cerrar su propia sesion.
            self._redirigir("/entrar", [self._cookie("", borrar=True)])
            return
        aviso = f"Cuenta {nombre} actualizada."
        if cerradas:
            aviso += f" Se cerraron {cerradas} sesiones suyas."
        self._redirigir(f"/usuarios?ok={quote(aviso)}")

    def _rol_post(self) -> None:
        """Crea o actualiza un rol con las casillas marcadas."""
        campos = self._campos()
        if campos is None:
            return
        nombre = campos.get("rol", "").strip()
        marcados = self.campos_lista.get("permisos", [])
        errores = self.ctx.usuarios.guardar_rol(nombre, marcados)
        if errores:
            self._html(paginas.formulario_rol(
                nombre, marcados, PERMISOS, ETIQUETAS_PERMISO, errores, False,
                self._cuantos_con_rol(nombre), self._para_pintar(self.usuario),
            ))
            return
        # Un rol cambiado cambia lo que pueden todos los que lo tienen: hay que
        # echarlos para que la proxima pagina que pidan use lo nuevo.
        cerradas = sum(
            self.ctx.sesiones.cerrar_usuario(u.nombre)
            for u in self.ctx.usuarios.listar() if u.rol == nombre
        )
        log.info(
            "%s guardo el rol '%s' con %d permisos (%d sesiones cerradas)",
            self.usuario.nombre, nombre, len(marcados), cerradas,
        )
        self._anotar("rol_cambio", f"{nombre}: {len(marcados)} permisos")
        self._redirigir(f"/usuarios?ok={quote(f'Rol {nombre} guardado.')}")

    def _rol_baja(self) -> None:
        campos = self._campos()
        if campos is None:
            return
        nombre = campos.get("rol", "")
        errores = self.ctx.usuarios.borrar_rol(nombre)
        if errores:
            self._error(400, " ".join(errores))
            return
        log.info("%s borro el rol '%s'", self.usuario.nombre, nombre)
        self._anotar("rol_baja", nombre)
        self._redirigir(f"/usuarios?ok={quote(f'Rol {nombre} borrado.')}")

    def _desbloquear(self) -> None:
        """Levanta el bloqueo de una IP que se paso de intentos."""
        campos = self._campos()
        if campos is None:
            return
        ip = campos.get("ip", "").strip()
        habia = self.ctx.sesiones.desbloquear(ip)
        if habia:
            log.info("%s desbloqueo la direccion %s", self.usuario.nombre, ip)
            self._anotar("desbloqueo", ip)
            aviso = f"Desbloqueada {ip}."
        else:
            # Puede haber caducado sola entre que se pinto la tabla y se pulso
            # el boton: no es un error, pero conviene decirlo para que quien lo
            # pulso no se quede pensando si hizo algo.
            aviso = f"{ip} ya no estaba bloqueada."
        self._redirigir(f"/auditoria?ok={quote(aviso)}")

    def _cuantos_con_rol(self, rol: str) -> int:
        return sum(1 for u in self.ctx.usuarios.listar() if u.rol == rol)

    def _baja_usuario(self) -> None:
        campos = self._campos()
        if campos is None:
            return
        nombre = campos.get("nombre", "")
        if nombre == self.usuario.nombre:
            self._error(400, "No puedes borrar tu propia cuenta.")
            return
        errores = self.ctx.usuarios.borrar(nombre)
        if errores:
            self._error(400, " ".join(errores))
            return
        cerradas = self.ctx.sesiones.cerrar_usuario(nombre)
        # El ultimo sondeo de nombres se guarda a nombre de quien lo lanzo, y
        # se le ensena por ese nombre. Si la cuenta se borra y manana se crea
        # otra igual para OTRO cliente, esa cuenta nueva heredaria el detalle
        # del sondeo viejo: los nombres de los equipos de quien estaba antes.
        # Un nombre de usuario no es una identidad estable; borrarlo tiene que
        # llevarse lo que colgaba de el.
        ultimo = identidades.ultimo()
        if ultimo is not None and ultimo.quien == nombre and not ultimo.corriendo:
            identidades.olvidar()
        log.info(
            "%s borro la cuenta '%s' (%d sesiones cerradas)",
            self.usuario.nombre, nombre, cerradas,
        )
        self._anotar("usuario_baja", nombre)
        self._redirigir(f"/usuarios?ok={quote(f'Cuenta {nombre} borrada.')}")

    def _cambiar_mi_clave(self) -> None:
        campos = self._campos()
        if campos is None:
            return
        sesion = self._para_pintar(self.usuario)
        etiqueta = self.usuario.rol
        nueva = campos.get("nueva", "")

        errores = []
        # Se pide la clave actual aunque ya haya sesion: si alguien se deja el
        # panel abierto, que el de al lado no pueda quedarse con la cuenta.
        if self.ctx.usuarios.autenticar(
            self.usuario.nombre, campos.get("actual", "")
        ) is None:
            errores.append("la clave actual no es correcta")
        if nueva != campos.get("repetida", ""):
            errores.append("las dos claves nuevas no coinciden")

        if not errores:
            errores = self.ctx.usuarios.cambiar_clave(self.usuario.nombre, nueva)

        if errores:
            self._html(paginas.cuenta(
                self.usuario.nombre, etiqueta, errores, sesion
            ))
            return

        cerradas = self.ctx.sesiones.cerrar_usuario(self.usuario.nombre)
        log.info(
            "%s cambio su propia clave (%d sesiones cerradas)",
            self.usuario.nombre, cerradas,
        )
        self._anotar("clave_propia")
        # Incluida la suya: se vuelve a entrar con la clave nueva, que ademas
        # confirma que se escribio la que se creia.
        self._redirigir("/entrar", [self._cookie("", borrar=True)])

    def _entrar(self) -> None:
        campos = self._campos()
        if campos is None:
            return

        ip = self.ip
        espera = self.ctx.sesiones.bloqueado(ip)
        if espera:
            log.warning("Login bloqueado para %s (%ds restantes)", ip, espera)
            self._anotar(
                "login_bloqueado",
                f"quedan {espera}s de bloqueo",
                usuario=campos.get("usuario", "")[:32],
            )
            self._html(
                paginas.login(f"Demasiados intentos. Prueba en {espera} segundos."),
                429,
            )
            return

        nombre = campos.get("usuario", "")
        # `autenticar` gasta el mismo tiempo exista la cuenta o no: si con un
        # usuario inexistente respondiera sin llegar a pbkdf2, la respuesta
        # volveria mucho mas rapido y eso delataria que cuentas existen.
        cuenta = self.ctx.usuarios.autenticar(nombre, campos.get("clave", ""))

        if cuenta is None:
            self.ctx.sesiones.fallo(ip)
            log.warning("Login fallido desde %s (usuario %r)", ip, nombre[:32])
            # Se anota el nombre TECLEADO aunque no exista: media auditoria de
            # un ataque es saber que cuentas estuvieron probando.
            fallos_previos = self.ctx.sesiones.intentos(ip)
            self._anotar(
                "login_fallido",
                f"intento {fallos_previos} de {self.ctx.sesiones.intentos_max}",
                usuario=nombre[:32],
            )
            self._html(paginas.login("Usuario o contrasena incorrectos."), 401)
            return

        self.ctx.sesiones.acierto(ip)
        token = self.ctx.sesiones.abrir(cuenta.nombre)
        log.info("Sesion abierta para %s (%s) desde %s", cuenta.nombre, cuenta.rol, ip)
        self._anotar("login_ok", f"rol {cuenta.rol}", usuario=cuenta.nombre)
        self._redirigir("/", [self._cookie(token)])

    # --- Alta, edicion y baja ----------------------------------------------

    def _preguntar_identidad(self, campos: dict, alta: bool) -> bool:
        """Le pregunta el nombre al router y repinta el formulario. Sin guardar.

        Devuelve True si atendio la peticion (y ya escribio la respuesta).

        Existe porque preguntar la identidad solo pasaba con el nombre VACIO, y
        en la pantalla de editar el campo viene relleno: un equipo al que le
        cambiaron el nombre en el router se quedaba para siempre con el que se
        tecleo el primer dia. Que sea un boton y no algo automatico al guardar
        es deliberado: cambiar el nombre mueve el archivo dentro del repo, y eso
        no puede pasar de sorpresa por abrir el formulario y darle a guardar.

        No valida ni guarda nada: solo rellena el campo y devuelve el formulario
        tal como estaba, para que quien lo mira vea lo que va a guardar ANTES de
        guardarlo.
        """
        if campos.get("accion") != "identidad":
            return False

        # Se construye un Equipo solo para poder preguntar: lleva la IP, el
        # puerto y las credenciales que hay AHORA MISMO en el formulario, que es
        # lo que hace util al boton (se pueden corregir y volver a probar sin
        # guardar nada). El nombre da igual, no viaja al equipo.
        clave = campos.get("clave", "")
        if not clave and campos.get("original"):
            equipos, _ = self._inventario_tolerante()
            anterior = next(
                (e for e in equipos if e.nombre == campos["original"]), None
            )
            if anterior is not None:
                clave = getattr(anterior, "clave", "")

        sonda, errores = validar_equipo(
            nombre=self._nombre_provisional(campos.get("ip", "").strip()),
            empresa=campos.get("empresa", ""),
            ip=campos.get("ip", ""),
            puerto=campos.get("puerto", ""),
            grupo=campos.get("grupo", ""),
            # Sin nombres ocupados: esta sonda NO se guarda, solo se usa para
            # abrir la sesion. Pasarle el inventario haria que el boton fallara
            # con "ese nombre ya existe" justo cuando se esta editando un
            # equipo que, por definicion, ya esta en el inventario.
            existentes=[],
            usuario=campos.get("usuario", ""),
            clave=clave,
        )
        if errores:
            # Sin una IP y un puerto validos no hay a quien preguntar. Se dice
            # eso y no "no respondio", que mandaria a revisar el router.
            self._html(paginas.formulario_equipo(
                campos, errores, alta, original=campos.get("original", ""),
                sesion=self._para_pintar(self.usuario)))
            return True

        real = self._identidad(sonda)
        if real:
            campos = dict(campos, nombre=real)
            aviso = []
            log.info("Identidad de %s: '%s'", sonda.ip, real)
        else:
            aviso = [
                f"El equipo de {sonda.ip}:{sonda.puerto} no dijo como se llama. "
                "Puede estar apagado, con otras credenciales, o con un usuario "
                "sin permiso para leer /system identity. El nombre se deja como "
                "estaba."
            ]

        self._html(paginas.formulario_equipo(
            campos, aviso, alta, original=campos.get("original", ""),
            sesion=self._para_pintar(self.usuario)))
        return True

    def _alta(self) -> None:
        if not self._editable("equipos.crear"):
            return
        campos = self._campos()
        if campos is None:
            return
        if self._preguntar_identidad(campos, alta=True):
            return

        with self.ctx.candado:
            equipos, _ = self._inventario_tolerante()
            equipo, errores, provisional = self._resolver(campos, equipos)
            # El alcance se mira sobre el equipo YA validado, no sobre lo que
            # llego en el formulario: asi no hay forma de colar una empresa
            # por un camino distinto al que se comprueba.
            if not errores and not self._edita(equipo):
                # Se audita: alguien tanteando los limites del vecino no puede
                # ser invisible. El intento por NOMBRE ya se registraba; este,
                # por empresa, no dejaba ni una linea.
                self._anotar(
                    "fuera_de_alcance",
                    f"intento crear {equipo.nombre} en '{equipo.empresa}'",
                )
                errores = [f"No puedes crear equipos en '{equipo.empresa}'."]
            if errores:
                self._html(paginas.formulario_equipo(
                    campos, errores, True, sesion=self._para_pintar(self.usuario)))
                return

            equipos.append(equipo)
            self._guardar_inventario(equipos)

        aviso = f"{equipo.nombre} anadido."
        if provisional:
            aviso += (" El equipo no respondio, asi que se guardo con su IP;"
                      " se renombrara solo en el primer respaldo que salga bien.")
        log.info("Alta desde el panel: %s (%s)", equipo.nombre, equipo.ip)
        self._anotar("equipo_alta", f"{equipo.nombre} ({equipo.empresa})")
        self._redirigir(f"/equipos?ok={quote(aviso)}")

    def _edicion(self) -> None:
        if not self._editable("equipos.editar"):
            return
        campos = self._campos()
        if campos is None:
            return

        if self._preguntar_identidad(campos, alta=False):
            return

        original = campos.get("original", "")
        with self.ctx.candado:
            equipos, _ = self._inventario_tolerante()
            indice = next(
                (i for i, e in enumerate(equipos) if e.nombre == original), None
            )
            if indice is None:
                self._error(404, f"No hay ningun equipo llamado '{original}'.")
                return

            antiguo = equipos[indice]
            if not self._alcanzable(antiguo, escribir=True):
                return
            # Mismo camino que el alta: si al editar se borra el nombre, se le
            # vuelve a preguntar al equipo. Es la forma de arreglar un router
            # que se dio de alta estando apagado.
            equipo, errores, _prov = self._resolver(campos, equipos, original)
            # Cambiarle la empresa a un equipo es sacarlo de un alcance y
            # meterlo en otro: hay que poder escribir en los dos lados, o
            # seria una forma de regalarle un equipo a alguien (o de robarselo).
            if not errores and not self._edita(equipo):
                self._anotar(
                    "fuera_de_alcance",
                    f"intento mover {antiguo.nombre} a '{equipo.empresa}'",
                )
                errores = [f"No puedes mover este equipo a '{equipo.empresa}'."]
            if errores:
                self._html(paginas.formulario_equipo(
                    campos, errores, False, original,
                    self._para_pintar(self.usuario)))
                return

            equipos[indice] = equipo
            self._guardar_inventario(equipos)

        aviso = f"{equipo.nombre} actualizado."
        # Si cambio el nombre, la empresa o el grupo, cambia la ruta dentro del
        # repo. Hay que mover el archivo con git: si no, el proximo respaldo
        # crearia uno nuevo y el historico del equipo quedaria partido en dos.
        if antiguo.nombre != equipo.nombre:
            # Lo observado (modelo, version, ultimo respaldo) esta indexado por
            # el nombre: sin esto el equipo aparece como recien dado de alta y
            # no se arregla hasta el siguiente ciclo bueno, que pueden ser
            # horas, y ademas sin la marca de "fallando" que hiciera sospechar.
            self.ctx.hechos.renombrar(antiguo.nombre, equipo.nombre)

        if antiguo.ruta_relativa != equipo.ruta_relativa:
            if Almacen(self.cfg).renombrar(
                antiguo.ruta_relativa, equipo.ruta_relativa
            ):
                aviso += " Su historico se movio con el."
            else:
                aviso += (" Ojo: no se pudo mover su historico en el repositorio;"
                          " revisa el log.")
        log.info("Edicion desde el panel: %s -> %s", original, equipo.nombre)
        self._anotar("equipo_cambio", f"{original} -> {equipo.nombre}")
        self._redirigir(f"/equipos?ok={quote(aviso)}")

    def _baja(self) -> None:
        if not self._editable("equipos.baja"):
            return
        campos = self._campos()
        if campos is None:
            return

        nombre = campos.get("nombre", "")
        with self.ctx.candado:
            equipos, _ = self._inventario_tolerante()
            objetivo = next((e for e in equipos if e.nombre == nombre), None)
            if objetivo is None:
                self._error(404, f"No hay ningun equipo llamado '{nombre}'.")
                return
            if not self._alcanzable(objetivo, escribir=True):
                return
            self._guardar_inventario([e for e in equipos if e.nombre != nombre])

        # Los respaldos NO se borran, y es a proposito: dar de baja un equipo
        # es dejar de consultarlo, no perder lo que se sabia de el. El repo es
        # justo lo que hace falta el dia que alguien pregunta como estaba
        # configurado ese router antes de retirarlo.
        log.info("Baja desde el panel: %s", nombre)
        self._anotar("equipo_baja", nombre)
        self._redirigir(
            f"/equipos?ok={quote(f'{nombre} dado de baja. Sus respaldos siguen en el repositorio.')}"
        )

    def _resolver(self, campos: dict, equipos, original: str = ""):
        """Convierte lo que llego del formulario en un Equipo.

        Devuelve (equipo, errores, provisional). `provisional` significa que
        hubo que quedarse con la IP como nombre porque el equipo no supo (o no
        pudo) decir el suyo.

        El orden importa: primero se valida y solo despues se pregunta la
        identidad. Al reves, un formulario con la IP mal escrita costaria una
        conexion SSH con su timeout antes de poder decir lo obvio, y quien
        esta dando de alta 20 equipos lo nota.
        """
        nombre = campos.get("nombre", "").strip()
        preguntar = not nombre
        if preguntar:
            nombre = self._nombre_provisional(campos.get("ip", "").strip())

        # validar_equipo espera los NOMBRES ya ocupados, no los equipos.
        ocupados = [e.nombre for e in equipos]

        # La clave no se manda al navegador al editar, asi que vacia significa
        # "deja la que ya tenia". Si no, abrir el formulario y guardar sin
        # tocar nada le borraria la contrasena al equipo.
        clave = campos.get("clave", "")
        if not clave and original:
            anterior = next((e for e in equipos if e.nombre == original), None)
            if anterior is not None:
                clave = getattr(anterior, "clave", "")

        comunes = {
            "empresa": campos.get("empresa", ""),
            "ip": campos.get("ip", ""),
            "puerto": campos.get("puerto", ""),
            "grupo": campos.get("grupo", ""),
            "existentes": ocupados,
            "original": original,
            # Dos nombres para lo mismo: el formulario manda "intervalo" y el
            # importador de Excel manda "intervalo_minutos", que es como se
            # llama la columna. Se aceptan los dos porque este metodo sirve a
            # los dos caminos.
            #
            # Esto estuvo roto y no dio la cara: al no encontrar la clave se
            # leia cadena vacia, que es un valor VALIDO ("usa el general"), asi
            # que cada equipo importado perdia su intervalo en silencio y el
            # panel mostraba "general" como si asi se hubiera pedido. Un fallo
            # que devuelve un valor por defecto plausible no se nota nunca.
            "intervalo": campos.get("intervalo") or campos.get("intervalo_minutos", ""),
            "usuario": campos.get("usuario", ""),
            "clave": clave,
        }

        equipo, errores = validar_equipo(nombre=nombre, **comunes)
        if errores or not preguntar:
            return equipo, errores, False

        # `equipo` ya lleva la IP y el PUERTO validados: preguntar por el
        # puerto por defecto cuando el equipo escucha en otro seria no
        # preguntar en absoluto.
        real = self._identidad(equipo)
        if not real or real == equipo.nombre:
            return equipo, [], True

        candidato, choque = validar_equipo(nombre=real, **comunes)
        if choque:
            # El equipo dijo llamarse como otro que ya esta en el inventario.
            # Se queda con la IP: dos equipos con el mismo nombre compartirian
            # archivo en el repo y se pisarian los respaldos.
            log.warning(
                "%s dice llamarse '%s', pero ese nombre no se puede usar: %s",
                equipo.ip, real, "; ".join(choque),
            )
            return equipo, [], True
        return candidato, [], False

    def _identidad(self, equipo) -> str:
        """Le pregunta el nombre al router. Cadena vacia si no se pudo."""
        # Import perezoso: device arrastra paramiko, y las pantallas que solo
        # leen el inventario no tienen por que pagar esa carga.
        from . import device

        try:
            return device.identidad(equipo, self.cfg)
        except Exception:  # noqa: BLE001
            # Un alta no se puede caer con un traceback porque el router este
            # apagado: se guarda con la IP y ya se renombrara.
            log.warning(
                "No se pudo consultar la identidad de %s", equipo.ip, exc_info=True
            )
            return ""

    @staticmethod
    def _nombre_provisional(ip: str) -> str:
        """Nombre de archivo valido a partir de la IP.

        Para IPv4 sale la IP tal cual, que es la convencion que usa el resto
        del proyecto para reconocer un equipo sin identificar. Una IPv6 lleva
        dos puntos, que no valen en un nombre de archivo: se sustituyen, y ese
        equipo habra que renombrarlo a mano.
        """
        return ip.replace(":", "-")

    # --- Importacion --------------------------------------------------------

    def _importacion(self) -> None:
        if not self._editable("equipos.importar"):
            return

        tipo = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in tipo or "boundary=" not in tipo:
            self._error(400, "Se esperaba un archivo.")
            return
        try:
            largo = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            largo = -1
        if largo < 0 or largo > MAXIMO_SUBIDA:
            self.close_connection = True
            self._error(413, "El archivo es demasiado grande.")
            return

        frontera = tipo.split("boundary=", 1)[1].strip().strip('"').encode()
        partes = dict(_limpiar(self.rfile.read(largo), frontera))
        contenido = partes.get("archivo", b"")
        nombre_archivo = partes.get("__archivo__", b"").decode("utf-8", "replace")

        if not contenido:
            self._html(paginas.importar(
                imp.hay_soporte_xlsx(), None, "El archivo llego vacio.",
                self._para_pintar(self.usuario)))
            return

        try:
            filas, avisos = imp.leer(contenido, nombre_archivo)
        except imp.ErrorImportacion as exc:
            self._html(paginas.importar(
                imp.hay_soporte_xlsx(), None, str(exc),
                self._para_pintar(self.usuario)))
            return

        resultado = self._dar_de_alta(filas, avisos)
        self._html(paginas.importar(
            imp.hay_soporte_xlsx(), resultado, sesion=self._para_pintar(self.usuario)))

    def _dar_de_alta(self, filas, avisos) -> dict:
        """Valida fila a fila y guarda una sola vez al final.

        Se guarda al final y no por equipo porque cada `guardar` reescribe el
        CSV entero: con 300 filas serian 300 reescrituras, y el proceso de
        respaldo podria leer el inventario a mitad de la importacion y ver una
        flota incompleta.
        """
        rechazos = []
        sin_identificar: list[str] = []
        altas = 0

        with self.ctx.candado:
            equipos, _ = self._inventario_tolerante()
            for fila in filas:
                equipo, errores, provisional = self._resolver(fila.datos, equipos)
                if not errores and not self._edita(equipo):
                    errores = [f"no puedes dar de alta en '{equipo.empresa}'"]
                if errores:
                    rechazos.append({
                        "fila": fila.numero,
                        "equipo": (fila.datos.get("nombre")
                                   or fila.datos.get("ip") or "(vacia)"),
                        "motivo": "; ".join(errores),
                    })
                    continue

                equipos.append(equipo)
                altas += 1
                if provisional:
                    sin_identificar.append(equipo.nombre)

            if altas:
                self._guardar_inventario(equipos)

        log.info(
            "Importacion desde el panel: %d altas, %d rechazos", altas, len(rechazos)
        )
        self._anotar("importacion", f"{altas} altas, {len(rechazos)} rechazos")
        if sin_identificar:
            avisos = list(avisos) + [
                f"{len(sin_identificar)} equipos no respondieron y se guardaron "
                "con su IP como nombre: se renombraran solos en el primer "
                f"respaldo que salga bien ({', '.join(sin_identificar[:5])}"
                f"{'...' if len(sin_identificar) > 5 else ''})."
            ]
        return {
            "leidas": len(filas),
            "altas": altas,
            "rechazos": rechazos,
            "avisos": avisos,
        }

    # --- Ajustes ------------------------------------------------------------

    def _ajustes_fondo(self) -> None:
        """Guarda la imagen de fondo que se sube desde Ajustes."""
        tipo = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in tipo or "boundary=" not in tipo:
            self._error(400, "Se esperaba un archivo.")
            return
        try:
            largo = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            largo = -1
        if largo < 0 or largo > MAXIMO_IMAGEN:
            self.close_connection = True
            self._error(
                413,
                f"La imagen pasa de {MAXIMO_IMAGEN // (1024 * 1024)} MB. "
                "Hasta ese tamano se ajusta sola; por encima hay que reducirla "
                "antes.",
            )
            return

        frontera = tipo.split("boundary=", 1)[1].strip().strip(chr(34)).encode()
        partes = dict(_limpiar(self.rfile.read(largo), frontera))
        datos = partes.get("archivo", b"")
        if not datos:
            self._fondo_error("El archivo llego vacio.")
            return

        # El tipo se decide por el CONTENIDO. La extension de lo que suba
        # alguien no es una fuente de verdad: renombrar es gratis.
        #
        # Se comprueba ANTES de pasarsela a Pillow, y no despues ni en su lugar:
        # decodificar es meter bytes de un desconocido en codigo C, y no hay por
        # que hacerlo con un archivo que ya se sabe que no es una imagen.
        real = _tipo_real(datos)
        if not real:
            self._fondo_error(
                "Eso no es una imagen de las que se pueden usar. "
                "Formatos: JPG, PNG, WebP o AVIF."
            )
            return

        extension = {v: k for k, v in TIPOS_IMAGEN.items()}[real]

        # Se ajusta al tamano que hace falta: 1920 px de ancho y unos cientos de
        # KB. La imagen se descarga entera antes de que aparezca el formulario
        # de entrada, asi que lo que pese se paga en cada login.
        nota = ""
        try:
            datos, ext_nueva, nota = imagen.ajustar(datos)
            if ext_nueva:
                extension = ext_nueva
        except imagen.ErrorImagen as exc:
            self._fondo_error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            # Que falle el ajuste no puede impedir poner un fondo: se guarda lo
            # que se subio, que es lo que pasaba antes de que esto existiera.
            log.warning("No se pudo ajustar la imagen de fondo: %s", exc,
                        exc_info=True)
            nota = ("No se pudo ajustar el tamano, asi que se guardo tal cual. "
                    "Si tarda en aparecer el formulario, subela mas pequena.")
        # El nombre del archivo lo decide el panel, no quien sube: asi no hay
        # forma de escribir fuera de la carpeta de datos ni de pisar otra cosa.
        destino = Path(self.cfg.almacen.ajustes).parent / f"fondo-login{extension}"
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(datos)
        except OSError as exc:
            log.error("No se pudo guardar el fondo en %s: %s", destino, exc)
            self._fondo_error(f"No se pudo guardar la imagen: {exc}")
            return

        # Se limpian los fondos con otra extension: si no, al cambiar de PNG a
        # JPG quedaria el viejo ocupando sitio para siempre.
        for otra in TIPOS_IMAGEN:
            sobra = destino.with_suffix(otra)
            if sobra != destino:
                try:
                    sobra.unlink()
                except OSError:
                    pass

        self.cfg.guardar_ajustes({"fondo_login": str(destino)})
        log.info(
            "%s cambio el fondo del login (%s, %d KB)",
            self.usuario.nombre, real, len(datos) // 1024,
        )
        self._anotar("ajustes", f"fondo del login ({real}, {len(datos) // 1024} KB)")
        self._ajustes_ok(
            f"Fondo guardado ({len(datos) // 1024} KB)."
            + (f" {nota}" if nota else "")
            + " Recarga la pantalla de entrada para verlo."
        )

    def _ajustes_fondo_quitar(self) -> None:
        """Deja la pantalla de entrada sin imagen."""
        if not self._tragar_cuerpo():
            return

        actual = self.cfg.web.fondo_login
        self.cfg.guardar_ajustes({"fondo_login": ""})
        if actual:
            try:
                Path(actual).unlink()
            except OSError:
                # Que no se pueda borrar el archivo no impide quitarlo del
                # panel: lo que manda es el ajuste.
                pass
        log.info("%s quito el fondo del login", self.usuario.nombre)
        self._anotar("ajustes", "quito el fondo del login")
        self._ajustes_ok("Fondo quitado.")

    def _respaldar_ahora(self) -> None:
        """Pide al programador que arranque un ciclo ya.

        El panel NO lanza el respaldo: deja la peticion y el programador la
        recoge en su siguiente vistazo (ver planificador.pedir_ciclo). Hacerlo
        aqui significaria dos procesos escribiendo en el mismo repositorio de
        git, que es exactamente lo que este proyecto evita.
        """
        if not self._tragar_cuerpo():
            return

        prog = estado_programador(self.cfg)

        # Si ya hay uno corriendo no se pide otro: el programador es de un solo
        # hilo y lo encolaria detras, asi que quien le da al boton esperaria sin
        # entender por que no pasa nada. Mejor decirlo.
        if prog.get("corriendo"):
            self._ajustes_ok(
                "Ya hay un respaldo en marcha ahora mismo. Mira como va en Estado."
            )
            return

        # El guardian tiene que ir en los DOS sentidos. El sondeo de nombres se
        # niega a arrancar con un ciclo en marcha; si aqui no se mirara lo
        # contrario, bastaria lanzar el sondeo y pulsar este boton para provocar
        # a mano justo lo que aquel evita: dos procesos reescribiendo el mismo
        # inventario y el mismo repositorio.
        if identidades.en_curso() is not None:
            self._pintar_ajustes(
                error="Se les esta preguntando el nombre a los routers, y ese "
                      "trabajo tambien toca el inventario. Espera a que termine.",
                codigo=409,
            )
            return

        if not pedir_ciclo(self.cfg, self.usuario.nombre):
            self._fondo_error(
                "No se pudo dejar la peticion. Revisa que el servicio pueda "
                "escribir en la carpeta de datos."
            )
            return

        log.info("%s pidio un respaldo inmediato desde el panel",
                 self.usuario.nombre)
        self._anotar("respaldo_manual", "pidio un respaldo inmediato")
        self._ajustes_ok(
            "Respaldo pedido: el programador lo arranca en unos segundos. "
            "Se sigue en Estado."
        )

    def _pintar_ajustes(self, mensaje: str = "", error: str = "",
                        codigo: int = 200) -> None:
        """La pantalla de ajustes, entera. TODOS los caminos pasan por aqui.

        Antes cada camino llamaba a paginas.ajustes con su propia lista de
        argumentos -siete sitios repitiendo los mismos seis-, y eso significa
        que cualquier dato nuevo que necesite la pantalla hay que acordarse de
        anadirlo en los siete. El que se olvide no da error: pinta la pagina
        con el valor por defecto, o sea con el contador a cero o el bloque
        vacio, que parece un estado y es un descuido.
        """
        self._html(paginas.ajustes(
            self.cfg,
            # El recortado y no el crudo. Hoy la plantilla solo lee 'proxima' y
            # 'ultima', asi que daria igual; pero ese diccionario lleva dentro
            # 'ultimas', con el nombre de CADA equipo de la flota y el tamano
            # del ultimo lote. El dia que alguien pinte una fila mas aqui, una
            # cuenta que solo ve a un cliente se llevaria la lista entera.
            self._programador_visible(),
            zona=self.ctx.zona,
            sesion=self._para_pintar(self.usuario),
            mensaje=mensaje,
            error=error,
            fondo=self._marca_fondo(),
            replica=leer_replica(self.cfg),
            huerfanos=self._huerfanos(),
            mudanza=self._resumen_mudanza(),
            **self._contexto_identidades(),
        ), codigo)

    def _huerfanos(self) -> list[dict]:
        """Lo que queda en el repositorio de equipos que ya no estan de alta.

        Un equipo dado de baja no esta en el inventario, asi que no tiene
        empresa contra la que calcular el alcance. Es la MISMA regla que en
        _cambios: quien no lo ve todo no ve ninguno. Aqui pesa mas todavia,
        porque no es mirar sino borrar: adivinar la empresa por el nombre de su
        carpeta seria dejar que quien acierte el slug se lleve por delante los
        respaldos del cliente de al lado.
        """
        if not self.usuario.alcance.todo:
            return []

        # A proposito el crudo y no _inventario_tolerante: aquel devuelve una
        # lista VACIA cuando el CSV no se puede leer, y con el inventario vacio
        # todos los archivos del repositorio pareceria que son de equipos dados
        # de baja. Ofrecer borrar la flota entera porque el inventario no se
        # pudo leer es el fallo que aqui no se puede permitir: mejor una lista
        # vacia y el resto de Ajustes funcionando.
        try:
            equipos, _ = self._inventario()
        except ErrorInventario as exc:
            log.warning("No se puede listar lo dado de baja: %s", exc)
            return []

        try:
            return Almacen(self.cfg).huerfanos(equipos)
        except (ErrorAlmacen, OSError) as exc:
            log.warning("No se pudo leer el repositorio: %s", exc)
            return []

    def _borrar_datos(self) -> None:
        """Borra del repositorio los respaldos de un equipo dado de baja.

        Dos formas, y elige quien borra: 'retirar' lo quita de la version de
        hoy y deja su historia dentro de git (se puede recuperar), y 'purgar'
        reescribe el repositorio para que no quede en ninguna version (no se
        puede). La segunda existe porque un cliente que se va puede pedir que
        no quede nada suyo, y "esta borrado pero sigue en el historial" no es
        eso.
        """
        # Con _editable y no con _permite aunque el permiso sea de Ajustes: el
        # panel en modo solo lectura no puede borrar respaldos, igual que no
        # puede dar de baja un equipo. 'datos.borrar' es su propio permiso y no
        # 'equipos.baja' porque son cosas distintas: dar de baja deja los
        # respaldos donde estan, y esto es justo lo contrario.
        if not self._editable("datos.borrar"):
            return
        campos = self._campos()
        if campos is None:
            return

        ruta = campos.get("ruta", "").strip()
        # Lista blanca con caida al lado prudente: un modo inventado desde
        # fuera no puede convertirse en el irreversible.
        modo = campos.get("modo", "retirar")
        if modo not in ("retirar", "purgar"):
            modo = "retirar"
        confirmacion = campos.get("confirmacion", "").strip()

        # Que el formulario mande una ruta no significa nada: un formulario se
        # edita. La ruta tiene que estar de verdad en la lista de dados de
        # baja QUE ESTA CUENTA VE, porque si no, escribirla a mano seria borrar
        # el respaldo de un equipo que sigue de alta o de otro cliente.
        lista = self._huerfanos()
        objetivo = next((h for h in lista if h["ruta"] == ruta), None)
        if objetivo is None:
            self._pintar_ajustes(
                error="Ese archivo no esta en la lista de equipos dados de baja. "
                      "Aqui solo se puede borrar lo de los equipos que ya no "
                      "estan en el inventario.",
                codigo=404,
            )
            return

        # Escribir el nombre es lo unico que separa este boton de un clic por
        # error en la fila de al lado. Se compara tal cual, sin recortes ni
        # mayusculas: la gracia es que haya que leer cual se esta borrando.
        if confirmacion != objetivo["nombre"]:
            self._pintar_ajustes(
                error=f"Para borrar los datos de '{objetivo['nombre']}' hay que "
                      "escribir su nombre exacto en el campo de confirmacion. "
                      "No se ha borrado nada.",
                codigo=400,
            )
            return

        # Mientras el programador corre un ciclo, NO. El es otro proceso y esta
        # commiteando en este mismo repositorio; purgar ademas lo reescribe
        # entero. Dos escritores sobre el mismo indice de git es justo lo que
        # este proyecto evita en todas partes, y el candado del panel no cruza
        # el limite del proceso.
        if estado_programador(self.cfg).get("corriendo"):
            self._pintar_ajustes(
                error="Ahora mismo hay un respaldo en marcha y esta escribiendo "
                      "en el mismo repositorio. Espera a que termine.",
                codigo=409,
            )
            return

        with self.ctx.candado:
            almacen = Almacen(self.cfg)
            if modo == "purgar":
                hecho = almacen.purgar(ruta)
            else:
                hecho = almacen.retirar(ruta)

        if not hecho:
            self._pintar_ajustes(
                error=f"No se pudo borrar '{objetivo['nombre']}'. El motivo esta "
                      "en el registro del servicio.",
                codigo=500,
            )
            return

        # Borrar datos tiene que dejar rastro de quien y de que: es lo primero
        # que se pregunta cuando alguien echa de menos un respaldo.
        log.info("%s borro los datos de %s (%s)", self.usuario.nombre, ruta, modo)
        self._anotar("datos_borrados", f"{ruta} ({modo})")

        if modo == "purgar":
            aviso = (f"Borrado '{objetivo['nombre']}' de todo el repositorio, "
                     "incluida su historia. No se puede deshacer.")
        else:
            aviso = (f"Quitado '{objetivo['nombre']}' del repositorio. Sus "
                     "versiones anteriores siguen en git y se pueden recuperar.")
        self._pintar_ajustes(mensaje=aviso)

    def _resumen_mudanza(self) -> dict | None:
        """Que se llevaria la copia del servidor. None si esta cuenta no la ve.

        Solo se calcula para quien tiene el permiso, y no por discrecion: mide
        el repositorio recorriendolo entero, y a esta pantalla entra tambien
        quien solo viene a cambiar el intervalo de respaldo. Con una flota
        grande son miles de archivos que contar para pintar un cuadro que esa
        cuenta ni siquiera va a ver.
        """
        if self.usuario is None:
            return None
        if not self.ctx.usuarios.puede(self.usuario, "datos.mudanza"):
            return None
        try:
            return mudanza.resumen(self.cfg)
        except OSError as exc:
            log.warning("No se pudo medir la copia del servidor: %s", exc)
            return None

    def _mudanza(self) -> None:
        """Empaqueta este servidor entero y lo manda como descarga.

        Es la respuesta mas peligrosa que da el panel: dentro van las claves
        SSH de la flota en claro, los hashes del panel y, segun la
        configuracion, las passwords de los clientes. De ahi que tenga permiso
        propio, que se anote SIEMPRE en la auditoria -antes de mandar nada, no
        despues- y que el archivo temporal nazca ya privado.
        """
        if not self._permite("datos.mudanza"):
            return
        # El formulario no lleva campos, pero SI lleva cuerpo. Sin leerlo, lo
        # que quede en el socket lo lee la peticion siguiente como su primera
        # linea (ver el comentario de _error).
        if self._campos() is None:
            return

        # El programador es otro proceso y esta commiteando en el mismo
        # repositorio. Empaquetarlo a la vez daria una copia con el arbol de
        # git a medias: se leeria como un paquete correcto y al restaurarlo
        # aparecerian los objetos sin la referencia que los nombra.
        if estado_programador(self.cfg).get("corriendo"):
            self._pintar_ajustes(
                error="Ahora mismo hay un respaldo en marcha y esta escribiendo "
                      "en el repositorio. Espera a que termine y vuelve a "
                      "pedir la copia.",
                codigo=409,
            )
            return

        nombre = mudanza.nombre_sugerido(self.cfg)
        lado = Path(self.cfg.almacen.estado).parent
        temporal = lado / f".{nombre}.parcial"
        try:
            # Con el candado del panel: si alguien esta purgando un equipo
            # desde otra pestana, no se empaqueta el repositorio a mitad de la
            # reescritura.
            with self.ctx.candado:
                mudanza.empaquetar(self.cfg, temporal)
            tamano = temporal.stat().st_size
        except (mudanza.ErrorMudanza, OSError) as exc:
            log.error("No se pudo empaquetar el servidor: %s", exc)
            try:
                temporal.unlink()
            except OSError:
                pass
            self._pintar_ajustes(
                error=f"No se pudo preparar la copia: {exc}", codigo=500,
            )
            return

        # Se anota ANTES de mandarla. Si se anotara despues, una descarga que se
        # corta a la mitad no dejaria rastro, y una copia parcial de esto sigue
        # llevando dentro el inventario entero.
        log.info("%s descargo la copia del servidor (%d bytes)",
                 self.usuario.nombre, tamano)
        self._anotar("mudanza", f"{nombre} ({tamano} bytes)")

        try:
            self._cabeceras(
                200, "application/gzip", tamano,
                [("Content-Disposition", f'attachment; filename="{nombre}"')],
            )
            with temporal.open("rb") as fh:
                for trozo in iter(lambda: fh.read(mudanza.TROZO), b""):
                    self.wfile.write(trozo)
        finally:
            # Pase lo que pase, incluida una descarga que el navegador corta a
            # la mitad: este archivo no puede quedarse en el disco. Es una copia
            # de las credenciales de la flota entera esperando a que alguien la
            # encuentre.
            try:
                temporal.unlink()
            except OSError as exc:
                log.error("Quedo una copia sin borrar en %s: %s", temporal, exc)

    # --- Restaurar una copia, en dos pasos ----------------------------------
    #
    # Primero se sube el archivo y se LEE su manifiesto; despues, en otra
    # peticion, se confirma. Son dos y no uno por dos razones distintas:
    #
    #   - Se ve de que servidor viene el paquete y de cuando es ANTES de que
    #     reemplace nada. Equivocarse de archivo aqui es sustituir la flota
    #     entera por la de otro momento, y eso tiene que poder pararse leyendo
    #     una linea, no descubrirse despues mirando el inventario.
    #   - El formulario de subida lleva un unico campo, asi que el cuerpo se
    #     puede volcar a disco segun llega, sin juntarlo en memoria. El paquete
    #     de una flota grande son cientos de megas y este servidor no tiene ni
    #     swap: leerlo entero a memoria seria tumbar el panel justo cuando se
    #     esta intentando recuperarlo.

    def _carpeta_datos(self) -> Path:
        return Path(self.cfg.almacen.estado).parent

    def _recibir_paquete(self, destino: Path) -> str:
        """Vuelca a `destino` el archivo del formulario. '' si fue bien.

        Parsea el multipart a mano, y puede permitirselo porque el formulario
        tiene UN solo campo: lo que hay antes del contenido es una cabecera que
        termina en la primera linea en blanco, y lo que hay despues es la
        frontera de cierre. Todo lo de en medio es el archivo, y se copia por
        trozos sin mirarlo.
        """
        tipo = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in tipo or "boundary=" not in tipo:
            return "Se esperaba un archivo."
        try:
            largo = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            largo = -1
        if largo <= 0:
            return "El archivo llego vacio."
        if largo > MAXIMO_MUDANZA:
            return (f"El paquete pasa de {MAXIMO_MUDANZA // (1024 * 1024)} MB. "
                    "Un archivo asi de grande se restaura desde el terminal con "
                    "mkbackup --restaurar-datos, que no tiene este limite.")

        # Hace falta sitio para el archivo, para desempaquetarlo y para apartar
        # lo que ya hay. Sin esta comprobacion, restaurar en un servidor justo
        # de disco lo llena a la mitad y deja el sistema sin lo viejo y sin lo
        # nuevo, que es el peor final posible.
        try:
            libre = shutil.disk_usage(self._carpeta_datos()).free
        except OSError:
            libre = None
        if libre is not None and libre < largo * 3:
            return (f"No hay disco suficiente: el paquete ocupa "
                    f"{largo // (1024 * 1024)} MB y hacen falta unas tres veces "
                    f"eso para desempaquetarlo y apartar lo de ahora. Libres: "
                    f"{libre // (1024 * 1024)} MB.")

        frontera = tipo.split("boundary=", 1)[1].strip().strip('"').encode()
        cierre = b"\r\n--" + frontera
        quedan = largo

        # La cabecera de la parte: como mucho unos cientos de bytes.
        cabecera = b""
        while b"\r\n\r\n" not in cabecera:
            if quedan <= 0 or len(cabecera) > 8192:
                return "El archivo llego mal formado."
            trozo = self.rfile.read(min(4096, quedan))
            if not trozo:
                return "La subida se corto antes de tiempo."
            quedan -= len(trozo)
            cabecera += trozo
        _, resto = cabecera.split(b"\r\n\r\n", 1)

        escritos = 0
        descriptor = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as fh:
            fh.write(resto)
            escritos += len(resto)
            while quedan > 0:
                trozo = self.rfile.read(min(mudanza.TROZO, quedan))
                if not trozo:
                    return "La subida se corto antes de tiempo."
                quedan -= len(trozo)
                fh.write(trozo)
                escritos += len(trozo)

        # Y se le quita la frontera de cierre, que viaja pegada al final. Se
        # busca desde atras en una ventana pequena en vez de dar por hecho su
        # longitud exacta: hay clientes que anaden el salto de linea final y
        # otros que no, y sobrarle o faltarle dos bytes al .tar.gz lo rompe.
        ventana = min(escritos, len(cierre) + 16)
        with open(destino, "r+b") as fh:
            fh.seek(escritos - ventana)
            cola = fh.read(ventana)
            corte = cola.rfind(cierre)
            if corte < 0:
                return "El archivo llego mal formado."
            fh.truncate(escritos - ventana + corte)
        return ""

    def _limpiar_subidas(self) -> None:
        """Tira los paquetes subidos que nadie llego a confirmar.

        Se quedan ahi si alguien sube un archivo, ve de que servidor es y
        cierra la pestana. Son copias completas del sistema con todas sus
        credenciales: no pueden acumularse en el disco esperando a nadie.
        """
        limite = time.time() - 3600
        for viejo in self._carpeta_datos().glob(f"{PREFIJO_SUBIDA}*"):
            try:
                if viejo.stat().st_mtime < limite:
                    viejo.unlink()
            except OSError:
                continue

    def _subir_mudanza(self) -> None:
        """Recibe un paquete y ensena lo que trae, sin restaurar nada todavia."""
        if not self._editable("datos.mudanza"):
            return

        self._limpiar_subidas()
        vale = secrets.token_hex(16)
        destino = self._carpeta_datos() / f"{PREFIJO_SUBIDA}{vale}.tar.gz"
        fallo = self._recibir_paquete(destino)
        if fallo:
            try:
                destino.unlink()
            except OSError:
                pass
            self._pintar_ajustes(error=fallo, codigo=400)
            return

        # Se lee el manifiesto YA. Un archivo que no es un paquete de mkbackup,
        # o que llego a medias, se caza aqui y no despues de haber apartado los
        # datos buenos.
        try:
            manifiesto = mudanza.leer_manifiesto(destino)
        except mudanza.ErrorMudanza as exc:
            try:
                destino.unlink()
            except OSError:
                pass
            self._pintar_ajustes(error=str(exc), codigo=400)
            return

        log.info("%s subio un paquete de '%s' (%s)", self.usuario.nombre,
                 manifiesto.get("servidor"), manifiesto.get("cuando"))
        self._html(paginas.confirmar_mudanza(
            vale, manifiesto, destino.stat().st_size,
            sesion=self._para_pintar(self.usuario), zona=self.ctx.zona,
        ))

    def _restaurar_mudanza(self) -> None:
        """Vuelca el paquete que se subio antes. Reemplaza el sistema entero."""
        if not self._editable("datos.mudanza"):
            return
        campos = self._campos()
        if campos is None:
            return

        # El vale es un nombre de archivo compuesto por el propio panel. Se
        # comprueba que solo tenga hexadecimal antes de pegarlo a una ruta: si
        # no, escribirle puntos y barras seria elegir cualquier archivo del
        # disco como paquete a restaurar.
        vale = campos.get("vale", "")
        if not vale or len(vale) != 32 or any(c not in "0123456789abcdef" for c in vale):
            self._pintar_ajustes(error="La subida ya no vale. Subela otra vez.",
                                 codigo=400)
            return
        paquete = self._carpeta_datos() / f"{PREFIJO_SUBIDA}{vale}.tar.gz"
        if not paquete.is_file():
            self._pintar_ajustes(
                error="Esa subida ya no esta: se limpian solas al cabo de una "
                      "hora. Vuelve a subir el archivo.",
                codigo=404,
            )
            return

        if campos.get("confirmacion", "").strip() != PALABRA_RESTAURAR:
            self._pintar_ajustes(
                error=f"Para reemplazar todo hay que escribir "
                      f"'{PALABRA_RESTAURAR}' en el campo de confirmacion. No "
                      "se ha tocado nada.",
                codigo=400,
            )
            return

        if estado_programador(self.cfg).get("corriendo"):
            self._pintar_ajustes(
                error="Hay un respaldo en marcha escribiendo en el repositorio "
                      "que esto va a reemplazar. Espera a que termine.",
                codigo=409,
            )
            return

        # Se anota ANTES: si la restauracion sale a medias, el registro tiene
        # que decir quien la lanzo. Ademas el archivo de auditoria es una de las
        # piezas que se reemplazan, o sea que esta linea se pierde y queda la
        # del servidor de origen; por eso va tambien al log del servicio, que
        # no lo toca nadie.
        log.warning("%s esta restaurando una copia completa del servidor",
                    self.usuario.nombre)
        self._anotar("mudanza_restaurada", paquete.name)

        try:
            with self.ctx.candado:
                hecho = mudanza.restaurar(self.cfg, paquete, sobrescribir=True)
        except (mudanza.ErrorMudanza, OSError) as exc:
            log.error("No se pudo restaurar: %s", exc)
            self._pintar_ajustes(error=f"No se restauro nada: {exc}", codigo=500)
            return
        finally:
            try:
                paquete.unlink()
            except OSError:
                pass

        log.warning("Restauradas %d piezas; lo anterior quedo en %s",
                    len(hecho["puestas"]), ", ".join(hecho["apartadas"]) or "-")
        for aviso in hecho["avisos"]:
            log.warning("%s", aviso)

        # Se echa a todo el mundo, incluido quien acaba de hacerlo. El archivo
        # de cuentas es otro: los tokens que hay en memoria pertenecen a
        # usuarios que quiza ya no existen o que ahora tienen otro rol, y
        # dejarlos dentro seria conservar unos permisos que ya no dice nadie.
        self.ctx.sesiones.cerrar_todas()
        self._html(paginas.mudanza_hecha(hecho), 200)

    def _contexto_identidades(self) -> dict:
        """Lo que sabe la pantalla sobre los nombres de los routers.

        Las cuentas se sacan del inventario que ESTA CUENTA ve: a quien solo
        alcanza a un cliente, el boton tiene que decirle a cuantos equipos
        suyos va a preguntar, no a cuantos de la flota entera.
        """
        try:
            equipos, _ = self._inventario_visible()
        except ErrorInventario:
            # Sin inventario legible no hay nada que sondear, pero tampoco es
            # motivo para no poder abrir Ajustes: el resto de la pantalla
            # (intervalo, acceso SSH, fondo) no depende de el.
            equipos = []
        mios = [e for e in equipos if self._edita(e)]
        return {
            "sondeo": self._sondeo_visible(),
            "equipos_totales": len(mios),
            "equipos_sin_nombre": sum(1 for e in mios if identidades.es_provisional(e)),
        }

    def _sondeo_visible(self) -> dict | None:
        """El sondeo, pero solo si esta cuenta tiene algo que ver con el.

        El sondeo es uno para todo el panel, y lo que lleva dentro son nombres
        de equipos y cuentas de la flota. En un panel donde cada cuenta ve solo
        a su cliente, ensenarselo a cualquiera con permiso de ajustes seria
        contarle cuantos equipos tiene el proveedor y como se llaman los del
        cliente de al lado. Lo ve quien lo lanzo, y quien alcanza a la flota
        entera; para el resto es como si no existiera.
        """
        sondeo = identidades.ultimo()
        if sondeo is None:
            return None
        if self.usuario.alcance.todo or sondeo.quien == self.usuario.nombre:
            return sondeo.instantanea()
        return None

    def _identidades_masivo(self) -> None:
        """Le pregunta el nombre a varios routers a la vez.

        Es el boton de la ficha de un equipo, pero para la flota. Y por eso no
        se puede hacer igual: preguntar a uno son dos segundos, preguntar a
        trescientos -con los apagados agotando el timeout- son minutos, y de
        eso no se entera nadie porque el navegador ya se ha rendido. Asi que
        aqui solo se arranca; el trabajo lo hace identidades.py en segundo
        plano y la pantalla lo va mirando.
        """
        # Su propio permiso, y con _editable: esto reescribe el nombre de los
        # equipos en el inventario, asi que en modo solo lectura no va. Se
        # separo de 'equipos.editar' porque son riesgos distintos: uno cambia
        # una ficha a mano, este sale a hablar con toda la flota de golpe.
        if not self._editable("equipos.identidades"):
            return
        campos = self._campos()
        if campos is None:
            return

        alcance = campos.get("alcance", identidades.SOLO_SIN_NOMBRE)
        if alcance not in identidades.ALCANCES:
            alcance = identidades.SOLO_SIN_NOMBRE

        # Mientras corre un ciclo NO. El programador es otro proceso y renombra
        # los equipos sin nombre el mismo; los dos a la vez son dos escritores
        # sobre el mismo inventario y el mismo repositorio de git, que es justo
        # lo que este proyecto evita en todas partes. El cerrojo del panel no
        # sirve aqui: no cruza el limite del proceso.
        if estado_programador(self.cfg).get("corriendo"):
            self._pintar_ajustes(
                error="Ahora mismo hay un respaldo en marcha, y ese ciclo tambien "
                      "renombra equipos. Espera a que termine y vuelve a darle.",
                codigo=409,
            )
            return

        if identidades.en_curso() is not None:
            self._pintar_ajustes(
                error="Ya se les esta preguntando. Abajo se ve como va."
            )
            return

        equipos, _ = self._inventario_visible()
        # Solo los que esta cuenta puede EDITAR: ver un equipo no da derecho a
        # cambiarle el nombre, y el nombre es la ruta de su respaldo.
        mios = [e for e in equipos if self._edita(e)]
        objetivo = identidades.a_quien_preguntar(mios, alcance)
        if not objetivo:
            self._pintar_ajustes(
                mensaje="No hay ningun equipo al que preguntarle: todos los que "
                        "puedes editar ya tienen nombre propio."
            )
            return

        sondeo = identidades.lanzar(
            self.cfg, self.ctx.candado, self.ctx.hechos,
            objetivo, self.usuario.nombre, alcance,
            # El motivo entero de un rechazo puede nombrar equipos de otros
            # clientes (un choque de nombres). Solo se cuenta entero a quien
            # alcanza a la flota completa.
            detallado=self.usuario.alcance.todo,
        )
        # La comprobacion de arriba mira y esta la cierra: entre las dos hay
        # sitio para que otro gane la carrera. Dar por bueno el arranque seria
        # decirle a esta persona "preguntando a 120 equipos" cuando no se
        # arranco nada con su alcance, y dejarla mirando un bloque vacio.
        if sondeo is None:
            self._pintar_ajustes(
                error="Otra persona acaba de lanzarlo. Espera a que termine."
            )
            return

        log.info(
            "%s pidio preguntar el nombre a %d equipo(s) (%s)",
            self.usuario.nombre, len(objetivo), alcance,
        )
        self._anotar("identidades", f"{len(objetivo)} equipos ({alcance})")
        self._pintar_ajustes(
            mensaje=f"Preguntando a {len(objetivo)} equipo(s). "
                    "Puedes irte de esta pantalla: sigue por su cuenta."
        )

    def _api_identidades(self) -> None:
        """Como va el sondeo, para el JS de la pantalla de ajustes."""
        self._json(self._sondeo_visible() or {"vacio": True})

    def _ajustes_ok(self, mensaje: str) -> None:
        self._pintar_ajustes(mensaje=mensaje)

    def _fondo_error(self, mensaje: str) -> None:
        self._pintar_ajustes(error=mensaje, codigo=400)

    def _ajustes_remoto(self) -> None:
        """A donde se suben los respaldos, y con que credencial."""
        campos = self._campos()
        if campos is None:
            return

        url = campos.get("remoto", "").strip()
        # Solo git: nada de rutas locales ni de esquemas raros. La direccion la
        # escribe un administrador, pero es lo que decide a QUE maquina de
        # internet salen las configuraciones de los clientes, con sus claves
        # dentro. Una lista de esquemas conocidos es barata y cierra la puerta a
        # que un descuido (o una cuenta comprometida) lo mande a otro sitio por
        # un protocolo que nadie esperaba.
        if url and not url.startswith(("https://", "ssh://", "git@")):
            self._pintar_ajustes(
                error="La direccion tiene que empezar por https:// o por ssh:// "
                      "(o ser del tipo git@servidor:empresa/repo.git). No se "
                      "admite http:// sin cifrar: por ahi viajarian las "
                      "configuraciones y el token.",
                codigo=400,
            )
            return

        # Y NO se admite el token metido dentro de la direccion, que es la
        # costumbre ("https://oauth2:TOKEN@gitlab.com/..."). Pegado ahi, ese
        # token deja de ser un secreto: se escribe en .git/config, viaja en cada
        # copia del repositorio, sale en el texto de "replicado a ..." que se
        # guarda y se pinta en esta misma pantalla, y aparece en el mensaje de
        # cualquier error de red. El campo de abajo existe justo para que no
        # haga falta; si se admitiera esto, todo el cuidado del otro camino no
        # serviria de nada.
        if url.startswith("https://") and "@" in url.split("/", 3)[2]:
            self._pintar_ajustes(
                error="No pongas el usuario ni el token dentro de la direccion. "
                      "Quedaria escrito en el disco y saldria en los mensajes de "
                      "error. Deja la direccion limpia y ponlos en los campos de "
                      "abajo.",
                codigo=400,
            )
            return

        try:
            cada = int(campos.get("remoto_cada", "1") or 0)
        except ValueError:
            cada = -1
        if cada < 0:
            self._pintar_ajustes(
                error="'Cada cuantos ciclos' tiene que ser 0 o mas.", codigo=400,
            )
            return

        cambios = {
            "remoto": url,
            "remoto_rama": campos.get("remoto_rama", "").strip(),
            "remoto_usuario": campos.get("remoto_usuario", "").strip(),
            "remoto_cada": cada,
        }
        # Vacio significa "no lo toques", igual que la clave de los routers. Si
        # se guardara la cadena vacia, abrir esta pantalla y pulsar Guardar
        # dejaria la subida sin credencial y fallando en silencio hasta que
        # alguien mirase el log.
        token = campos.get("remoto_token", "")
        if token:
            cambios["remoto_token"] = token
        # Quitar la direccion es apagar la subida: la credencial que quedara
        # ahi no serviria para nada y seria un secreto guardado sin motivo.
        if not url:
            cambios["remoto_token"] = ""

        self.cfg.guardar_ajustes(cambios)
        log.info("%s cambio el repositorio remoto (%s%s)", self.usuario.nombre,
                 url or "ninguno", ", token nuevo" if token else "")
        # La direccion si, el token NUNCA.
        self._anotar("remoto", f"{url or 'ninguno'}"
                               + (", token nuevo" if token else ""))
        self._pintar_ajustes(
            mensaje="Guardado. Pruebala antes de fiarte: el proximo ciclo con "
                    "cambios la usara."
            if url else "Guardado. Los respaldos se quedan solo en este servidor."
        )

    def _probar_remoto(self) -> None:
        """Pregunta al remoto que ramas tiene. No escribe nada."""
        if not self._tragar_cuerpo():
            return
        try:
            detalle = Almacen(self.cfg).probar_remoto()
        except ErrorAlmacen as exc:
            # El mensaje de git ya viene con la credencial tapada (ver
            # store._git): aqui llega listo para ensenarlo.
            self._anotar("remoto_prueba", "fallo")
            self._pintar_ajustes(error=f"No se pudo: {exc}", codigo=400)
            return
        except Exception:  # noqa: BLE001
            log.exception("Fallo inesperado probando el remoto")
            self._pintar_ajustes(
                error="No se pudo probar la conexion. Mira el registro del servicio.",
                codigo=500,
            )
            return
        self._anotar("remoto_prueba", "ok")
        self._pintar_ajustes(mensaje=detalle)

    def _ajustes_ssh(self) -> None:
        """Cambia el usuario y la clave con los que se entra a los routers."""
        campos = self._campos()
        if campos is None:
            return

        usuario = campos.get("ssh_usuario", "").strip()
        if not usuario:
            self._pintar_ajustes(
                error="El usuario SSH no puede quedar vacio: sin el no se entra "
                      "a ningun equipo.",
                codigo=400,
            )
            return

        cambios = {"ssh_usuario": usuario}
        clave = campos.get("ssh_password", "")
        # Vacia significa "no la toques". Si se guardara la cadena vacia, abrir
        # la pantalla y pulsar Guardar dejaria la flota entera sin credencial.
        if clave:
            cambios["ssh_password"] = clave

        self.cfg.guardar_ajustes(cambios)
        # A proposito NO se registra la clave, solo que se cambio y quien.
        log.info(
            "%s cambio el acceso general a los routers (usuario '%s'%s)",
            self.usuario.nombre, usuario, ", clave nueva" if clave else "",
        )
        # El usuario si, la clave NUNCA: solo que se cambio.
        self._anotar(
            "acceso_ssh",
            f"usuario '{usuario}'" + (", clave nueva" if clave else ""),
        )
        self._pintar_ajustes(
            mensaje="Acceso guardado. El programador lo usa en el siguiente ciclo."
        )

    def _ajustes(self) -> None:
        campos = self._campos()
        if campos is None:
            return

        try:
            intervalo = int(campos.get("intervalo_minutos", ""))
        except ValueError:
            intervalo = 0
        if intervalo < 1:
            self._pintar_ajustes(
                error="El intervalo tiene que ser un numero de minutos mayor que cero.",
                codigo=400,
            )
            return

        # guardar_ajustes filtra por lista blanca: aunque alguien inyecte otro
        # campo en el formulario, de aqui no sale nada que no sea el intervalo.
        self.cfg.guardar_ajustes({
            "intervalo_minutos": intervalo,
            "al_arrancar": campos.get("al_arrancar") == "1",
        })
        log.info("Ajustes cambiados desde el panel: intervalo %d min", intervalo)
        self._anotar("ajustes", f"intervalo {intervalo} min")
        self._pintar_ajustes(
            mensaje="Guardado. El programador lo recoge en el siguiente ciclo."
        )

    def log_message(self, formato: str, *args) -> None:
        # Por defecto http.server escribe a stderr y ensuciaria el journal con
        # una linea por refresco (uno cada pocos segundos, por pestana abierta).
        log.debug("%s %s", self.address_string(), formato % args)


# Cuantas conexiones se atienden a la vez. Con ThreadingHTTPServer a secas es
# un hilo por conexion y SIN TOPE: mil conexiones son mil hilos, y ahi el
# servidor no se defiende, se cae.
#
# El numero es alto a proposito, y esto es lo que hay que entender antes de
# bajarlo: NO es "cuantas personas caben". Con HTTP/1.1 la conexion se queda
# abierta despues de responder, y un navegador abre hasta SEIS por pestana. El
# panel ademas se refresca solo cada pocos segundos, o sea que esas conexiones
# no se quedan quietas. Cuatro personas con tres pestanas cada una pasan
# holgadamente de 32, y el tope no se nota como lentitud: se nota como una
# pagina que no carga, que es lo que estaba a punto de introducirse aqui con un
# tope "razonable" de 32.
#
# 128 deja sitio de sobra para la oficina entera y sigue estando muy lejos de
# lo que hace falta para tumbar la maquina. Lo que quede fuera espera en la
# cola de accept del sistema, que es lo correcto con una avalancha.
#
# El tope y el timeout del manejador van juntos y se sostienen el uno al otro:
# sin timeout, un tope solo cambia "quedarse sin memoria" por "quedarse
# atascado para siempre", que no es mejor. Es el timeout el que devuelve las
# plazas de las conexiones que ya no van a decir nada mas.
CONEXIONES_MAXIMAS = 128


class _Servidor(ThreadingHTTPServer):
    """ThreadingHTTPServer con tope de hilos y sin heredar el socket a git.

    daemon_threads: al parar el servicio no se espera a las conexiones con
    keep-alive abiertas, que con un panel que se refresca solo son todas.
    """

    daemon_threads = True
    # Una avalancha encuentra la cola llena y se le dice que no ahora, en vez
    # de aceptarla para morir un poco mas adelante.
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self._plazas = threading.BoundedSemaphore(CONEXIONES_MAXIMAS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        # Si no hay plaza se cierra ya, sin abrir hilo. Preferimos una conexion
        # rechazada a un servidor que no puede atender ninguna.
        if not self._plazas.acquire(blocking=False):
            log.warning(
                "Panel al limite (%d conexiones): se rechaza la de %s",
                CONEXIONES_MAXIMAS, client_address[0] if client_address else "?",
            )
            # close_request y NO shutdown_request: shutdown_request es quien
            # suelta la plaza, y aqui no se llego a coger ninguna. Soltarla
            # igualmente le regalaria un hueco al contador en CADA rechazo, o
            # sea que el tope subiria solo justo cuando esta de mas.
            self.close_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # process_request es quien crea el hilo. Si revienta ANTES de
            # crearlo (sin memoria para un hilo mas, tipicamente) nadie va a
            # llamar a shutdown_request, y la plaza se quedaria pillada para
            # siempre: el panel iria quedandose sin sitio poco a poco y sin que
            # nada lo explicara.
            self._plazas.release()
            raise

    def shutdown_request(self, request):
        # Lo llama process_request_thread al terminar, UNA vez por conexion
        # aceptada, le fuera bien o mal. Es el unico sitio por el que pasan
        # todas, asi que es donde se devuelve la plaza.
        try:
            super().shutdown_request(request)
        finally:
            self._plazas.release()


def servir(cfg: Config) -> int:
    # Se comprueba aqui y no en Config.validar(): quien solo respalda no tiene
    # por que configurar el login para que le corra el programador. Pero el
    # panel no arranca sin el, en vez de quedarse abierto de par en par.
    if not cfg.web.clave_hash:
        log.error(
            "El panel necesita login: falta web.clave_hash en la configuracion. "
            "Generalo con `mkbackup --hash-clave` y pegalo en el YAML."
        )
        return 2

    sesiones = Sesiones(
        duracion_horas=cfg.web.sesion_horas,
        inactividad_minutos=cfg.web.sesion_minutos,
        intentos_max=cfg.web.intentos_max,
        bloqueo_segundos=cfg.web.bloqueo_segundos,
    )
    ctx = Contexto(cfg, sesiones)

    # Migracion: quien ya tenia el panel con un unico usuario en config.yaml
    # se encuentra su misma cuenta y su misma clave, ya como administrador.
    # Sin esto, actualizar dejaria a la gente fuera de su propio panel.
    try:
        if ctx.usuarios.sembrar(cfg.web.usuario, cfg.web.clave_hash):
            log.info(
                "Creado %s con la cuenta '%s' de la configuracion, como "
                "administrador. A partir de ahora las cuentas y las claves se "
                "gestionan desde el panel.",
                cfg.almacen.usuarios, cfg.web.usuario,
            )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "No se pudo preparar el archivo de usuarios %s: %s",
            cfg.almacen.usuarios, exc,
        )
        return 2

    if not ctx.usuarios.listar():
        log.error(
            "No hay ninguna cuenta en %s y no se pudo sembrar ninguna. Crea "
            "una con `mkbackup --crear-usuario NOMBRE`.", cfg.almacen.usuarios
        )
        return 2

    manejador = partial(Manejador, ctx=ctx)

    try:
        servidor = _Servidor((cfg.web.direccion, cfg.web.puerto), manejador)
    except OSError as exc:
        log.error("No se pudo abrir %s:%s - %s", cfg.web.direccion, cfg.web.puerto, exc)
        return 3

    log.info(
        "Panel en http://%s:%d/ (usuario: %s, estado: %s)",
        cfg.web.direccion, cfg.web.puerto, cfg.web.usuario, cfg.almacen.estado,
    )
    if cfg.web.direccion not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "Escuchando en %s por HTTP: el login viaja en claro y el panel "
            "lista nombres, IPs y empresas de la flota. Dejalo en la LAN de "
            "gestion o detras de un proxy con TLS.",
            cfg.web.direccion,
        )
    if cfg.web.editar_inventario and not Path(cfg.inventario).parent.exists():
        log.warning(
            "La carpeta del inventario (%s) no existe: el panel no podra dar "
            "de alta equipos hasta que se pueda escribir ahi.", cfg.inventario
        )
    if not cfg.web.ocultar_secretos:
        log.warning(
            "web.ocultar_secretos esta en false: el panel mostrara passwords, "
            "PSK y secrets de las configuraciones a cualquiera que entre."
        )

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log.info("Parando el panel.")
    finally:
        servidor.server_close()
    return 0
