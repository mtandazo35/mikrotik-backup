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
import threading
import unicodedata
from functools import partial
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import historial as hist
from . import importar as imp
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
from .store import Almacen
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
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
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
        for clave, valor in extra or []:
            self.send_header(clave, valor)
        self.end_headers()
        self.wfile.write(cuerpo)

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
        nombre = self.ctx.sesiones.valida(self._token())
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

    def _permite(self, accion: str) -> bool:
        """Corta la peticion con un 403 si a la cuenta le falta ese permiso.

        Se comprueba aqui, en el servidor, y no solo escondiendo botones: la
        navegacion oculta lo que no se puede usar, pero cualquiera puede
        escribir la URL a mano.
        """
        if self.usuario is not None and self.ctx.usuarios.puede(self.usuario, accion):
            return True
        log.warning(
            "%s intento '%s' sin permiso",
            getattr(self.usuario, "nombre", "?"), accion,
        )
        self._anotar("sin_permiso", f"{accion} en {self.path.split('?')[0]}")
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
    def _cookie(token: str, segundos: int) -> tuple[str, str]:
        # HttpOnly: si algun dia se cuela un XSS, al menos no se lleva la sesion.
        # SameSite=Strict: nadie puede hacer que tu navegador use tu sesion desde
        # otra pagina. Es tambien lo que protege las altas y bajas de un CSRF:
        # un POST desde otro sitio llega sin cookie y muere en el login.
        # Sin Secure a proposito: el panel habla HTTP y el navegador descartaria
        # la cookie. Si lo pones tras un proxy TLS, ese es el sitio de anadirlo.
        return (
            "Set-Cookie",
            f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={segundos}",
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
            if ruta == "/api/estado":
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
            self._html(
                paginas.panel(sesion, cfg.web.refresco, cfg.zona_horaria)
            )

        elif ruta == "/api/estado":
            datos = self._estado_visible()
            # El proximo ciclo lo publica el programador en su propio archivo:
            # se pega aqui para que el panel haga una sola peticion, recortado
            # al alcance igual que el resto.
            datos["programador"] = self._programador_visible()
            self._json(datos)

        elif ruta == "/equipos":
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
                    cfg.web.editar_inventario
                    and self.ctx.usuarios.puede(self.usuario, "equipos.editar"),
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
            if not self._editable("equipos.importar"):
                return
            self._plantilla()

        elif ruta == "/cambios":
            self._cambios()

        elif ruta == "/historial":
            self._historial()

        elif ruta == "/diferencia":
            self._diferencia()

        elif ruta == "/ajustes":
            if not self._permite("ajustes"):
                return
            self._html(
                paginas.ajustes(
                    cfg, estado_programador(cfg), zona=self.ctx.zona, sesion=sesion,
                    fondo=self._marca_fondo(),
                )
            )

        elif ruta == "/usuarios":
            if not self._permite("usuarios"):
                return
            cuentas = self.ctx.usuarios.listar()
            self._html(paginas.usuarios(
                cuentas, self.ctx.usuarios.roles(),
                {u.nombre: self.ctx.usuarios.permisos(u) for u in cuentas},
                self.usuario.nombre, sesion, self._consulta().get("ok", ""),
                zona=self.ctx.zona,
            ))

        elif ruta == "/usuarios/nuevo":
            if not self._permite("usuarios"):
                return
            self._html(self._pintar_formulario_usuario(
                {"rol": "lector", "permisos": self.ctx.usuarios.roles().get("lector", []),
                 "alcance": {}}, [], True,
            ))

        elif ruta == "/usuarios/rol":
            if not self._permite("usuarios"):
                return
            nombre = self._consulta().get("rol", "")
            roles = self.ctx.usuarios.roles()
            self._html(paginas.formulario_rol(
                nombre, roles.get(nombre, []), PERMISOS, ETIQUETAS_PERMISO, [],
                nombre not in roles, self._cuantos_con_rol(nombre), sesion,
            ))

        elif ruta == "/usuarios/editar":
            if not self._permite("usuarios"):
                return
            self._editar_usuario()

        elif ruta == "/auditoria":
            # Con el mismo permiso que gestionar cuentas: quien puede crear
            # usuarios es quien tiene que poder ver que se ha hecho con ellos.
            if not self._permite("usuarios"):
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

    def _estado_visible(self) -> dict:
        """El estado de la ejecucion, recortado al alcance de esta cuenta.

        No basta con filtrar la lista de equipos: los contadores que trae el
        archivo (total, ok, cambios, fallidos) son de la flota entera, y las
        cifras y las graficas del panel se pintan con ellos. Un cliente que ve
        12 equipos pero un total de 300 ya sabe el tamano de su proveedor.
        Por eso se recalculan sobre lo que si le corresponde.
        """
        datos = resumen(self.cfg.almacen.estado)
        if self.usuario.alcance.todo:
            return datos

        # El estado guarda nombre y empresa por equipo: se filtra con eso, sin
        # tener que cruzarlo con el inventario.
        suyos = [
            e for e in datos.get("equipos", [])
            if self.usuario.alcance.puede_ver(e.get("empresa", ""), e.get("nombre", ""))
        ]

        def cuantos(estado):
            return sum(1 for e in suyos if e.get("estado") == estado)

        ok = cuantos("sin_cambios") + cuantos("cambio")
        fallidos = cuantos("fallo")

        # Lista BLANCA, no negra. Antes se devolvia el archivo entero con unos
        # cuantos campos recalculados encima, y por ahi se colaban la duracion
        # del ciclo de toda la flota, la concurrencia configurada y el pid.
        # Con una lista negra, cada campo nuevo que se anada al estado sale
        # publicado por descuido; con una blanca, hay que anadirlo aqui a mano.
        return {
            "situacion": datos.get("situacion"),
            "equipos": suyos,
            "total": len(suyos),
            "ok": ok,
            "cambios": cuantos("cambio"),
            "fallidos": fallidos,
            "hechos": ok + fallidos,
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
            self._error(404, f"No hay ningun equipo llamado '{nombre}'.")
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
            self._redirigir("/entrar", [self._cookie("", 0)])
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
            if self._permite("ajustes"):
                self._ajustes()
        elif ruta == "/ajustes/respaldar":
            if self._permite("ajustes"):
                self._respaldar_ahora()
        elif ruta == "/ajustes/ssh":
            if self._permite("ajustes"):
                self._ajustes_ssh()
        elif ruta == "/ajustes/fondo":
            if self._permite("ajustes"):
                self._ajustes_fondo()
        elif ruta == "/ajustes/fondo/quitar":
            if self._permite("ajustes"):
                self._ajustes_fondo_quitar()
        elif ruta == "/usuarios/nuevo":
            if self._permite("usuarios"):
                self._alta_usuario()
        elif ruta == "/usuarios/editar":
            if self._permite("usuarios"):
                self._editar_usuario_post()
        elif ruta == "/usuarios/baja":
            if self._permite("usuarios"):
                self._baja_usuario()
        elif ruta == "/usuarios/rol":
            if self._permite("usuarios"):
                self._rol_post()
        elif ruta == "/usuarios/rol/baja":
            if self._permite("usuarios"):
                self._rol_baja()
        elif ruta == "/auditoria/desbloquear":
            if self._permite("usuarios"):
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
            self._redirigir("/entrar", [self._cookie("", 0)])
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
        self._redirigir("/entrar", [self._cookie("", 0)])

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
        self._redirigir("/", [self._cookie(token, self.ctx.sesiones.duracion)])

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

    def _ajustes_ok(self, mensaje: str) -> None:
        self._html(paginas.ajustes(
            self.cfg, estado_programador(self.cfg), zona=self.ctx.zona,
            sesion=self._para_pintar(self.usuario), mensaje=mensaje,
            fondo=self._marca_fondo(),
        ))

    def _fondo_error(self, mensaje: str) -> None:
        self._html(paginas.ajustes(
            self.cfg, estado_programador(self.cfg), zona=self.ctx.zona,
            sesion=self._para_pintar(self.usuario), error=mensaje,
            fondo=self._marca_fondo(),
        ), 400)

    def _ajustes_ssh(self) -> None:
        """Cambia el usuario y la clave con los que se entra a los routers."""
        campos = self._campos()
        if campos is None:
            return

        usuario = campos.get("ssh_usuario", "").strip()
        if not usuario:
            self._html(paginas.ajustes(
                self.cfg, estado_programador(self.cfg), zona=self.ctx.zona,
                sesion=self._para_pintar(self.usuario),
                error="El usuario SSH no puede quedar vacio: sin el no se entra a ningun equipo.",
                fondo=self._marca_fondo(),
            ), 400)
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
        self._html(paginas.ajustes(
            self.cfg, estado_programador(self.cfg), zona=self.ctx.zona,
            sesion=self._para_pintar(self.usuario),
            mensaje="Acceso guardado. El programador lo usa en el siguiente ciclo.",
            fondo=self._marca_fondo(),
        ))

    def _ajustes(self) -> None:
        campos = self._campos()
        if campos is None:
            return

        try:
            intervalo = int(campos.get("intervalo_minutos", ""))
        except ValueError:
            intervalo = 0
        if intervalo < 1:
            self._html(
                paginas.ajustes(
                    self.cfg, estado_programador(self.cfg),
                    error="El intervalo tiene que ser un numero de minutos mayor que cero.",
                    zona=self.ctx.zona, sesion=self._para_pintar(self.usuario),
                    fondo=self._marca_fondo(),
                ),
                400,
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
        self._html(
            paginas.ajustes(
                self.cfg, estado_programador(self.cfg),
                mensaje="Guardado. El programador lo recoge en el siguiente ciclo.",
                zona=self.ctx.zona, sesion=self._para_pintar(self.usuario),
                fondo=self._marca_fondo(),
            )
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
