"""Cuentas del panel: quien entra, que puede hacer y sobre que equipos.

Hasta ahora el panel tenia UN usuario, escrito en config.yaml
(`web.usuario` / `web.clave_hash`). Eso funciona mientras solo lo mire quien
administra el servidor, pero el panel gestiona routers de VARIAS empresas
cliente de un ISP: el tecnico de guardia, el jefe de operaciones y el cliente
de Acme no pueden ver lo mismo, y sobre todo el cliente de Acme no puede ver
que existe Bravo.

De ahi que este modulo conteste a dos preguntas distintas, y a proposito
separadas:

  1. QUE PUEDE HACER una cuenta (crear equipos, importar, tocar ajustes...).
     Eso son los PERMISOS.
  2. SOBRE QUE EQUIPOS puede hacerlo. Eso es el ALCANCE.

Un operador con todos los permisos de equipos pero con alcance solo a Acme es
un tecnico de Acme; el mismo rol con alcance a todo es el tecnico del ISP. El
rol no decide de quien es el router, y el alcance no decide si se puede editar
o solo mirar. Las dos preguntas hay que responderlas siempre.

PERMISOS EFECTIVOS
------------------
Los roles ya NO son tres constantes en el codigo: son datos, viven en el mismo
archivo y se pueden crear, editar y borrar desde el panel (`roles`,
`guardar_rol`, `borrar_rol`). Un ISP no tiene los mismos tres perfiles que
otro, y pedir un despliegue nuevo para anadir "facturacion" no tiene sentido.

Encima del rol hay excepciones POR USUARIO, porque siempre aparece el caso de
"este de aqui, y solo este, tambien importa el Excel":

    efectivos = (permisos del rol | permisos_mas) - permisos_menos

`permisos_menos` gana SIEMPRE ante un conflicto: si un permiso esta a la vez en
`mas` y en `menos`, se queda fuera. Quitar es la operacion segura, y ante una
duda (o una interfaz que mande las dos listas mal) el error tiene que caer del
lado restrictivo, nunca del que concede de mas.

Un permiso que no este en PERMISOS se IGNORA en vez de reventar: el dia que se
retire un permiso, los usuarios que lo tuvieran guardado tienen que seguir
cargando y entrando. Perder un permiso retirado es lo esperado; dejar fuera a
la persona porque su archivo menciona algo que ya no existe, no.

ALCANCE
-------
`Alcance` dice sobre que equipos actua una cuenta y con que profundidad (VER o
EDITAR). Se puede dar por empresa, por equipo suelto, o `todo=True` para el
personal del ISP. Un permiso suelto sobre un equipo SUMA al de su empresa y
nunca resta: sirve para ensenar un router de una empresa que por lo demas no se
ve, o para poder editar uno concreto de una empresa que solo se mira.

LA INVARIANTE: EL ULTIMO ADMINISTRADOR TOTAL
--------------------------------------------
La regla que no se puede saltar por comodidad ya no es "que quede un admin"
(el rol admin es editable, asi que el nombre del rol no garantiza nada), sino
esta:

    SIEMPRE tiene que quedar al menos un usuario que tenga a la vez el permiso
    'usuarios' Y `alcance.todo`.

Ese es el unico que puede volver a repartirlo todo: crear cuentas, arreglar
roles y dar acceso a cualquier empresa. Sin ninguno, el panel queda sin llaves
y la unica salida es editar este JSON a mano por SSH. Se protege en TODAS las
operaciones que pueden romperla, incluidas las indirectas: borrar a esa cuenta,
cambiarle el rol, quitarle el permiso con `permisos_menos`, recortarle el
alcance, quitarle el permiso 'usuarios' AL ROL que se lo daba, o borrar ese rol.

POR QUE UN ARCHIVO APARTE Y NO config.yaml
------------------------------------------
config.yaml lo edita una PERSONA con root: lleva comentarios, decisiones y
secretos de conexion, y el servicio web no puede ni debe reescribirlo (un
volcado automatico se comeria los comentarios, y darle permiso de escritura al
proceso que atiende peticiones sobre el archivo que dice a que equipos
conectarse y con que credenciales es exactamente lo que no se quiere).

Las cuentas, en cambio, se crean y se cambian DESDE LA WEB. Eso necesita un
archivo que el servicio pueda escribir, con un formato que no le importe a
nadie leer a mano. De ahi este JSON aparte, escrito de forma atomica y en 0600.

El archivo guarda HASHES pbkdf2 (los mismos de `sesion.hashear`), NUNCA
contrasenas: quien se lleve una copia del archivo (un backup, un scp) no se
lleva las claves de nadie.

Solo libreria estandar, como el resto del proyecto.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .inventory import sanear_empresa
from .sesion import ITERACIONES, formato_valido, hashear, verificar

log = logging.getLogger("mkbackup.usuarios")

# Version del formato del archivo.
#   1 -> tres roles fijos en el codigo, sin alcance ni excepciones.
#   2 -> roles como datos, permisos_mas/permisos_menos y alcance por usuario.
#   3 -> permisos finos: 'ver', 'ajustes' y 'usuarios' se parten en uno por
#        pantalla y por accion (ver PERMISOS y HERENCIA).
# Las migraciones son automaticas al leer (ver _leer y _migrar).
FORMATO = 3

# Todo lo que se puede permitir o negar, por AREAS. Es una lista CERRADA a
# proposito: el panel comprueba permisos por nombre, y un permiso que solo
# exista en el archivo de un cliente no lo comprobaria nadie.
#
# Estan agrupados porque asi se pintan en la pantalla de roles, y porque una
# lista plana de treinta casillas no se entiende. El orden manda tambien en la
# interfaz: primero mirar, luego tocar la flota, luego la configuracion y al
# final las cuentas, que es de menos a mas peligroso.
AREAS = (
    ("Consultar", (
        ("estado.ver", "Ver la pantalla de Estado"),
        ("equipos.ver", "Ver la lista de equipos"),
        ("cambios.ver", "Ver la lista de cambios"),
        ("diferencias", "Ver el contenido de los cambios (diffs)"),
    )),
    ("La flota", (
        ("equipos.crear", "Anadir equipos"),
        ("equipos.editar", "Editar equipos"),
        ("equipos.baja", "Dar de baja equipos"),
        ("equipos.importar", "Importar desde Excel"),
        ("equipos.identidades", "Preguntarle el nombre a los routers"),
    )),
    ("Ajustes", (
        ("ajustes.ver", "Abrir la pantalla de Ajustes"),
        ("ajustes.programador", "Cambiar cada cuanto se respalda"),
        ("ajustes.respaldar", "Lanzar un respaldo ahora"),
        ("ajustes.ssh", "Cambiar el acceso SSH a los routers"),
        ("ajustes.remoto", "Cambiar a donde se suben los respaldos"),
        ("ajustes.fondo", "Cambiar la imagen de la pantalla de entrada"),
        ("datos.borrar", "BORRAR los respaldos de un equipo"),
    )),
    ("Cuentas y registro", (
        ("usuarios.ver", "Ver las cuentas y los roles"),
        ("usuarios.crear", "Anadir cuentas"),
        ("usuarios.editar", "Editar cuentas (rol, permisos y alcance)"),
        ("usuarios.baja", "Borrar cuentas"),
        ("roles.editar", "Crear y editar roles"),
        ("roles.baja", "Borrar roles"),
        ("auditoria.ver", "Ver el registro de auditoria"),
        ("auditoria.desbloquear", "Desbloquear direcciones IP"),
    )),
)

PERMISOS = tuple(clave for _, grupo in AREAS for clave, _ in grupo)

# Como se explica cada permiso en la web. Vive aqui y no en las plantillas para
# que el texto visible y el valor guardado no se separen nunca.
ETIQUETAS_PERMISO = {
    clave: texto for _, grupo in AREAS for clave, texto in grupo
}

# Permisos que ARRASTRAN otro: quien puede hacer algo tiene que poder ver la
# pantalla donde se hace. Se aplica al calcular los efectivos, no al guardar,
# para que la casilla que marco una persona siga siendo la que ve marcada.
#
# Existe porque la alternativa es un rol que "puede editar equipos" y no ve la
# lista de equipos: la persona marca lo que quiere permitir, guarda, y el panel
# le contesta 403 en una pantalla que ella misma acaba de habilitar. Eso no se
# lee como una configuracion estricta, se lee como que el panel esta roto.
HERENCIA = {
    "equipos.crear": ("equipos.ver",),
    "equipos.editar": ("equipos.ver",),
    "equipos.baja": ("equipos.ver",),
    "equipos.importar": ("equipos.ver",),
    "equipos.identidades": ("equipos.ver", "ajustes.ver"),
    "diferencias": ("cambios.ver",),
    "ajustes.programador": ("ajustes.ver",),
    "ajustes.respaldar": ("ajustes.ver",),
    "ajustes.ssh": ("ajustes.ver",),
    "ajustes.remoto": ("ajustes.ver",),
    "ajustes.fondo": ("ajustes.ver",),
    "datos.borrar": ("ajustes.ver",),
    "usuarios.crear": ("usuarios.ver",),
    "usuarios.editar": ("usuarios.ver",),
    "usuarios.baja": ("usuarios.ver",),
    "roles.editar": ("usuarios.ver",),
    "roles.baja": ("usuarios.ver",),
    "auditoria.desbloquear": ("auditoria.ver",),
}

# Como se traduce cada permiso viejo al conjunto nuevo. Esto NO es cosmetico:
# sin ello, al actualizar, los permisos guardados dejarian de existir, se
# ignorarian (que es lo que se hace con un permiso desconocido) y todo el mundo
# se quedaria sin acceso -incluido el ultimo administrador, o sea que el panel
# se cerraria solo y solo se abriria editando el JSON por SSH-.
#
# La regla es no cambiarle los permisos a nadie: quien podia hacer algo antes
# tiene que poder seguir haciendolo despues, ni mas ni menos.
EQUIVALENCIAS = {
    "ver": ("estado.ver", "equipos.ver", "cambios.ver"),
    "ajustes": ("ajustes.ver", "ajustes.programador", "ajustes.respaldar",
                "ajustes.ssh", "ajustes.remoto", "ajustes.fondo",
                "equipos.identidades", "datos.borrar"),
    "usuarios": ("usuarios.ver", "usuarios.crear", "usuarios.editar",
                 "usuarios.baja", "roles.editar", "roles.baja",
                 "auditoria.ver", "auditoria.desbloquear"),
}

# Los roles con los que arranca una instalacion nueva (y a los que se convierte
# un archivo de formato 1). A partir de ahi son datos: se editan desde el panel
# y esta constante solo vuelve a mirarse si el archivo no trae ninguno.
ROLES_INICIALES = {
    "admin": list(PERMISOS),
    "operador": ["estado.ver", "equipos.ver", "cambios.ver", "diferencias",
                 "equipos.crear", "equipos.editar", "equipos.baja",
                 "equipos.importar", "equipos.identidades", "ajustes.ver",
                 "ajustes.respaldar"],
    "lector": ["estado.ver", "equipos.ver", "cambios.ver"],
}

# Nombres de los roles iniciales. Se mantiene por compatibilidad con cli.py,
# que valida el --rol de --crear-usuario contra esta tupla. OJO: NO refleja los
# roles que se hayan creado despues desde el panel; para eso esta Usuarios.roles().
ROLES = tuple(ROLES_INICIALES)

# El rol que nunca se puede borrar: es el punto de partida conocido para volver
# a montar los permisos si alguien se lia editando roles.
ROL_ADMIN = "admin"

# Los permisos que abren la gestion. Junto con alcance.todo forman la invariante
# del "ultimo administrador total" (ver el docstring del modulo).
#
# Son DOS y hacen falta los dos: 'usuarios.editar' es lo que permite devolverle
# el rol o el permiso a alguien, y 'usuarios.ver' es lo que permite llegar a esa
# pantalla. Con uno solo se puede quedar una cuenta que teoricamente manda pero
# no encuentra la puerta, que a efectos practicos es un panel cerrado.
LLAVES = ("usuarios.ver", "usuarios.editar")

# Se conserva el nombre viejo porque hay codigo y mensajes que lo usan; apunta
# al permiso que de verdad reparte, que es el de editar.
LLAVE = "usuarios.editar"

# Profundidad del alcance. EDITAR incluye VER: quien puede cambiar un equipo
# evidentemente puede mirarlo.
VER = "ver"
EDITAR = "editar"
_ORDEN_NIVEL = {VER: 1, EDITAR: 2}

MINIMO_CLAVE = 8

# Nombre de usuario. Mismo juego de caracteres que inventory.NOMBRE_VALIDO,
# pero se define aqui a proposito y no se importa de alli: el de inventory
# existe porque el nombre del equipo acaba siendo un nombre de ARCHIVO dentro
# del repo git, y si un dia esa restriccion cambia (o se relaja) no tiene por
# que arrastrar a los nombres de las cuentas. Ademas aqui hay un limite de
# longitud que alli no pinta nada.
NOMBRE_VALIDO = re.compile(r"^[A-Za-z0-9._-]+$")
NOMBRE_MINIMO = 3
NOMBRE_MAXIMO = 32

# Nombre de rol. Se guarda SIEMPRE en minusculas (ver _clave_rol): el rol es un
# identificador que viaja en el campo 'rol' de cada usuario, no una etiqueta
# para leer, y tener 'Operador' y 'operador' a la vez seria una trampa igual
# que tenerla con los nombres de cuenta. La interfaz lo presenta como quiera.
ROL_VALIDO = re.compile(r"^[a-z0-9._-]+$")
ROL_MINIMO = 2
ROL_MAXIMO = 32


class ErrorUsuarios(Exception):
    """El archivo de usuarios no se pudo leer o no se pudo guardar.

    Es distinto de las listas de errores que devuelven crear/borrar/etc.: esas
    son fallos de la persona que llena el formulario y se le muestran para que
    los corrija. Esto es que el almacen esta roto y no hay nada que corregir
    desde la web.
    """


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clave_rol(nombre: str) -> str:
    return (nombre or "").strip().lower()


def _clave_empresa(nombre: str) -> str:
    """Como se comparan dos nombres de empresa dentro de un alcance.

    Se compara por el SLUG de inventory.sanear_empresa, no por el texto tal
    cual. Es la misma funcion que decide en que carpeta del repo acaban los
    respaldos de esa empresa, y por eso es la unica comparacion coherente:

      - El nombre legible lo escribe una persona en el Excel del inventario, y
        aparece como 'Acme S.A.', 'ACME S.A.' o '  Acme  S.A. ' segun el dia.
        Un alcance que no viera esas tres como la misma empresa dejaria fuera a
        un cliente por un espacio de mas, y eso en un panel multitenant se
        traduce en "no veo mis routers" sin ninguna pista de por que.
      - El slug ademas normaliza tildes (NFKD), que es justo donde el texto
        crudo falla peor: 'Telefonica' escrito con tilde y sin ella son dos
        cadenas distintas pero un solo cliente, y en macOS la misma tilde se
        codifica distinto que en Linux.
      - Y sobre todo: el slug es la granularidad a la que los datos estan
        REALMENTE separados en disco. Si dos nombres legibles distintos caen en
        el mismo slug, sus respaldos ya comparten carpeta en el repo (y
        inventory.cargar avisa de ello). Conceder acceso a esa carpeta entera
        es lo que de verdad ocurre; comparar por texto crudo daria la ilusion
        de una separacion mas fina de la que existe.

    Como sanear_empresa nunca devuelve vacio (cae en EMPRESA_DEFECTO), una
    empresa vacia se compara contra el mismo bucket 'sin-empresa' en el que el
    inventario mete las filas sin empresa. Tambien ahi son coherentes.
    """
    return sanear_empresa(nombre)


def _clave_equipo(nombre: str) -> str:
    """Como se comparan dos nombres de equipo dentro de un alcance.

    Sin distinguir mayusculas, por el mismo motivo que el slug de empresa: el
    nombre del equipo es el nombre de un ARCHIVO dentro del repo, y en Windows
    y macOS 'Core-01.rsc' y 'core-01.rsc' son el mismo archivo. Que el alcance
    los viera como dos equipos distintos daria acceso a uno y no al otro
    segun donde este clonado el repo.
    """
    return (nombre or "").strip().lower()


def _nivel_valido(valor) -> str | None:
    """VER, EDITAR o None si no se reconoce.

    Lo que no se entiende NO da acceso: el alcance sale de un JSON que se puede
    editar a mano, y ante un 'lectura' o un 'rw' mal escrito la respuesta segura
    es no conceder nada.
    """
    texto = str(valor or "").strip().lower()
    return texto if texto in _ORDEN_NIVEL else None


def _mayor(a: str | None, b: str | None) -> str | None:
    """El mas alto de dos niveles (EDITAR > VER > nada)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _ORDEN_NIVEL[a] >= _ORDEN_NIVEL[b] else b


@dataclass
class Alcance:
    """Sobre que equipos actua una cuenta, y con que profundidad.

    - `todo=True`: el personal del ISP. Manda sobre cualquier otra cosa.
    - `empresas`: nombre legible de la empresa -> VER o EDITAR.
    - `equipos`: nombre del equipo -> VER o EDITAR.

    Un equipo suelto SUMA a lo que da su empresa y nunca resta (ver `nivel`).
    Las claves se guardan tal como se escribieron y se comparan normalizadas
    (ver _clave_empresa / _clave_equipo).

    Un alcance vacio no ve NADA: una cuenta nueva no debe ver los routers de
    todos los clientes del ISP hasta que alguien decida a cuales tiene que ver.
    """

    todo: bool = False
    empresas: dict[str, str] = field(default_factory=dict)
    equipos: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _mejor(tabla: dict[str, str], clave: str, normalizar) -> str | None:
        """El nivel mas alto de las entradas que casan con esa clave.

        Se recorre la tabla en vez de indexar porque las claves se guardan tal
        como las escribio una persona y dos escrituras distintas ('Acme S.A.' y
        'acme sa') pueden normalizar a la misma empresa. Si eso pasa, mandan
        las dos y gana la mas alta; ignorar una seria conceder menos de lo que
        alguien dejo escrito. Son tablas de unas pocas decenas de entradas.
        """
        objetivo = normalizar(clave)
        if not objetivo:
            return None
        mejor: str | None = None
        for nombre, valor in (tabla or {}).items():
            if normalizar(nombre) != objetivo:
                continue
            mejor = _mayor(mejor, _nivel_valido(valor))
        return mejor

    def nivel(self, empresa: str, equipo: str) -> str | None:
        """EDITAR, VER o None para ese equipo de esa empresa.

        `todo` gana siempre. Si no, se toma el MAYOR entre lo que diga la
        empresa y lo que diga el equipo suelto: el permiso por equipo suma y
        nunca resta, asi que sirve tanto para ensenar un router de una empresa
        que por lo demas no se ve, como para poder editar uno concreto de una
        empresa que solo se mira. Un equipo listado con VER dentro de una
        empresa con EDITAR NO baja a ese equipo a solo lectura: para eso hay
        que quitarle EDITAR a la empresa y listarlos uno a uno, que es lo
        explicito.
        """
        if self.todo:
            return EDITAR
        return _mayor(
            self._mejor(self.empresas, empresa, _clave_empresa),
            self._mejor(self.equipos, equipo, _clave_equipo),
        )

    def puede_ver(self, empresa: str, equipo: str) -> bool:
        return self.nivel(empresa, equipo) is not None

    def puede_editar(self, empresa: str, equipo: str) -> bool:
        return self.nivel(empresa, equipo) == EDITAR

    def como_dict(self) -> dict:
        """Version JSON. Las claves salen tal como entraron."""
        return {
            "todo": bool(self.todo),
            "empresas": dict(self.empresas or {}),
            "equipos": dict(self.equipos or {}),
        }

    @classmethod
    def desde_dict(cls, datos) -> "Alcance":
        """Reconstruye un alcance desde JSON (o copia otro Alcance).

        Tolerante con la basura y siempre del lado que no concede: lo que no se
        entiende se descarta, no se convierte en acceso.
        """
        if isinstance(datos, cls):
            datos = datos.como_dict()
        if not isinstance(datos, dict):
            return cls()

        def tabla(origen) -> dict[str, str]:
            limpia: dict[str, str] = {}
            if not isinstance(origen, dict):
                return limpia
            for nombre, valor in origen.items():
                clave = str(nombre or "").strip()
                nivel = _nivel_valido(valor)
                if clave and nivel:
                    limpia[clave] = nivel
            return limpia

        return cls(
            todo=bool(datos.get("todo", False)),
            empresas=tabla(datos.get("empresas")),
            equipos=tabla(datos.get("equipos")),
        )


@dataclass
class Usuario:
    nombre: str
    rol: str
    clave_hash: str
    creado: str = ""          # ISO 8601 UTC
    ultimo_acceso: str = ""   # ISO 8601 UTC
    # Excepciones sobre lo que da el rol. Ver "PERMISOS EFECTIVOS" arriba.
    permisos_mas: list[str] = field(default_factory=list)
    permisos_menos: list[str] = field(default_factory=list)
    alcance: Alcance = field(default_factory=Alcance)


class Usuarios:
    """Las cuentas y los roles del panel, respaldados por un JSON.

    Carga perezosa con RELECTURA: el archivo se lee la primera vez que hace
    falta y se vuelve a leer en cuanto cambia en disco (ver `_asegurar`).
    Escritura atomica y en 0600 (ver `_escribir`).

    Seguro para usar desde varios hilos: el panel corre sobre
    ThreadingHTTPServer y cada peticion llega por el suyo.
    """

    def __init__(self, ruta: str | Path, iteraciones: int = ITERACIONES):
        self.ruta = Path(ruta)
        # Coste de pbkdf2 al crear cuentas y al cambiar claves. Solo se baja en
        # las pruebas: con el valor de produccion, una prueba que cree veinte
        # cuentas tardaria mas que todo el resto del proyecto junto.
        self._iteraciones = iteraciones

        self._lock = threading.RLock()
        self._cache: list[Usuario] = []
        self._roles: dict[str, list[str]] = _roles_iniciales()
        # Marca del archivo tal como estaba cuando se leyo (ver _marca_actual).
        self._marca: tuple[int, int] | None = None
        self._cargado = False
        # El archivo existe pero no se pudo leer o no se entiende. No es lo
        # mismo que "no hay archivo": ahi simplemente no hay cuentas todavia.
        self._corrupto = False
        self._descarte = ""

    # --- Lectura ------------------------------------------------------------

    def _marca_actual(self) -> tuple[int, int] | None:
        """(mtime_ns, tamano) del archivo, o None si no existe.

        Se miran las dos cosas y no solo la fecha: la resolucion del reloj del
        sistema de archivos no es infinita (en NTFS son decenas de ns, en
        algunos FS es peor), y dos escrituras muy seguidas podrian compartir
        marca de tiempo. Que ademas cambie el tamano cierra casi todo ese hueco
        sin tener que leer el archivo entero para comparar.
        """
        try:
            info = self.ruta.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _asegurar(self) -> None:
        """Carga el archivo si hace falta. Debe llamarse con el lock tomado.

        Se relee cuando la marca del archivo no coincide con la de la ultima
        lectura. Hace falta de verdad: el programador y el panel son procesos
        distintos, el panel puede tener varios hilos vivos durante horas, y un
        admin puede editar el archivo a mano por SSH. Cachear sin comprobar
        significaria que un usuario recien creado no existe para el proceso de
        al lado hasta que alguien lo reinicie.
        """
        marca = self._marca_actual()
        if self._cargado and marca == self._marca:
            return
        self._leer(marca)

    def _leer(self, marca: tuple[int, int] | None) -> None:
        """Relee el archivo entero. Debe llamarse con el lock tomado.

        Aqui es donde ocurre la migracion de formato 1 a 2: al leer, sin perder
        nada y de forma idempotente (ver _migrar).
        """
        self._cache = []
        self._roles = _roles_iniciales()
        self._corrupto = False
        self._marca = marca
        self._cargado = True

        texto = self._texto()
        if texto is None:
            return  # no existe todavia: no hay cuentas, y no es ningun error
        if texto is False:
            self._corrupto = True
            return

        try:
            datos = json.loads(texto)
        except ValueError as exc:
            log.error("El archivo de usuarios %s no es un JSON valido: %s",
                      self.ruta, exc)
            self._corrupto = True
            return

        if not isinstance(datos, dict) or not isinstance(datos.get("usuarios"), list):
            log.error("El archivo de usuarios %s no tiene la estructura esperada",
                      self.ruta)
            self._corrupto = True
            return

        formato = datos.get("formato")
        formato = formato if isinstance(formato, int) else 0

        self._roles = self._roles_desde(datos.get("roles"))

        cuentas: list[Usuario] = []
        for fila in datos["usuarios"]:
            # Una fila suelta mal escrita se salta con un aviso en vez de dar
            # el archivo entero por perdido: perder una cuenta es malo, pero
            # dejar fuera a las otras diez por su culpa es peor.
            if not isinstance(fila, dict):
                log.warning("Entrada no valida en %s: %r", self.ruta, fila)
                continue
            nombre = str(fila.get("nombre", "") or "")
            clave_hash = str(fila.get("clave_hash", "") or "")
            if not nombre or not clave_hash:
                log.warning("Entrada sin nombre o sin clave en %s: %r", self.ruta, fila)
                continue
            cuentas.append(
                Usuario(
                    nombre=nombre,
                    # Un rol desconocido NO se corrige a nada: se guarda tal
                    # cual y `permisos` se encarga de no darle nada.
                    rol=str(fila.get("rol", "") or ""),
                    clave_hash=clave_hash,
                    creado=str(fila.get("creado", "") or ""),
                    ultimo_acceso=str(fila.get("ultimo_acceso", "") or ""),
                    # Los permisos sueltos se guardan CRUDOS, sin filtrar por
                    # PERMISOS: si un dia se retira un permiso, el archivo lo
                    # conserva y el usuario sigue cargando. El filtro esta en
                    # _permisos_de, que es quien decide.
                    permisos_mas=_lista_texto(fila.get("permisos_mas")),
                    permisos_menos=_lista_texto(fila.get("permisos_menos")),
                    alcance=Alcance.desde_dict(fila.get("alcance")),
                )
            )

        cuentas = self._ordenar(cuentas)

        if formato >= FORMATO:
            self._cache = cuentas
            return

        # Formato viejo: se migra y se deja escrito. Ver _migrar.
        self._cache = self._migrar(cuentas, formato)
        try:
            self._escribir(self._cache, self._roles)
        except ErrorUsuarios as exc:
            # No se propaga: `listar` y `obtener` tienen que poder responder
            # aunque el disco este lleno o el archivo sea de solo lectura. Los
            # datos ya estan migrados EN MEMORIA, asi que el panel funciona
            # igual; solo se reintentara la escritura en la siguiente lectura.
            log.error("No se pudo guardar la migracion de %s a formato %d: %s",
                      self.ruta, FORMATO, exc)

    def _migrar(self, cuentas: list[Usuario], formato: int) -> list[Usuario]:
        """Convierte lo leido de un formato viejo. Sin perder nada.

        De 1 a 2:
          - Los roles pasan a ser datos: se escriben ROLES_INICIALES, que son
            exactamente los tres que estaban en el codigo.
          - Cada usuario CONSERVA su rol y recibe `alcance.todo = True`, sin
            excepciones de permisos. Es justo lo que ya podia hacer: en el
            formato 1 no habia alcance, asi que todo el mundo veia todos los
            equipos. Una actualizacion no puede cambiarle los permisos a nadie
            por sorpresa, ni ampliandolos ni (peor) dejando a alguien fuera de
            los equipos que llevaba anos mirando.

        De 2 a 3:
          - Los tres permisos gruesos se cambian por los finos que equivalen a
            lo mismo, en los ROLES y en las excepciones de cada cuenta (ver
            EQUIVALENCIAS). Nadie gana ni pierde nada: quien podia entrar a
            Ajustes sigue pudiendo, y quien no, tampoco.
          - Esto es obligatorio, no cosmetico. Un permiso desconocido se ignora
            (es lo correcto cuando se retira uno), asi que sin esta traduccion
            'usuarios' dejaria de contar y el panel se quedaria sin ningun
            administrador: cerrado, y solo reabrible editando el JSON por SSH.

        Es idempotente porque solo se llama cuando el formato leido es menor
        que FORMATO, y porque _traducir deja pasar tal cual lo que ya es nuevo.
        """
        log.info("Migrando el archivo de usuarios %s del formato %d al %d",
                 self.ruta, formato, FORMATO)

        if formato < 2:
            for cuenta in cuentas:
                cuenta.permisos_mas = []
                cuenta.permisos_menos = []
                cuenta.alcance = Alcance(todo=True)

        if formato < 3:
            self._roles = {
                nombre: sorted(_traducir(permisos))
                for nombre, permisos in self._roles.items()
            }
            for cuenta in cuentas:
                cuenta.permisos_mas = sorted(_traducir(cuenta.permisos_mas))
                # Los 'menos' se traducen igual: quien tenia quitado 'ajustes'
                # tiene que seguir sin poder entrar a Ajustes, y eso ahora son
                # ocho permisos. Traducir solo los 'mas' le devolveria en
                # silencio un acceso que alguien le habia retirado a proposito.
                cuenta.permisos_menos = sorted(_traducir(cuenta.permisos_menos))

        return cuentas

    def _roles_desde(self, datos) -> dict[str, list[str]]:
        """Los roles del archivo, o los iniciales si no trae ninguno.

        No se inventa el rol 'admin' si el archivo no lo trae: un rol que no
        existe no da ningun permiso (ver _permisos_de), y eso es lo seguro. Si
        alguien borra 'admin' editando el JSON a mano, el resultado es un panel
        cerrado que se arregla desde SSH, no un panel abierto de mas.
        """
        if not isinstance(datos, dict) or not datos:
            return _roles_iniciales()

        roles: dict[str, list[str]] = {}
        for nombre, permisos in datos.items():
            clave = _clave_rol(str(nombre or ""))
            if not clave:
                continue
            # Igual que con los permisos sueltos del usuario: se guardan crudos
            # y el filtro por PERMISOS ocurre al calcular los efectivos.
            roles[clave] = _lista_texto(permisos)
        return roles or _roles_iniciales()

    def _texto(self, intentos: int = 6) -> str | None | bool:
        """Contenido del archivo, None si no existe, False si no se pudo leer.

        Los reintentos son por Windows, igual que en estado.leer(): alli
        os.replace bloquea el archivo un instante y un lector que caiga en esa
        ventana recibe PermissionError. Sin ellos, guardar una cuenta desde una
        pestana podria hacer que otra viera el archivo como "corrupto" durante
        un parpadeo, y eso aqui significa negarle el acceso a alguien.
        """
        espera = 0.005
        for intento in range(intentos):
            try:
                return self.ruta.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None  # reintentar no lo va a crear
            except OSError as exc:
                if intento == intentos - 1:
                    log.error("No se pudo leer el archivo de usuarios %s: %s",
                              self.ruta, exc)
                    return False
                time.sleep(espera)
                espera = min(espera * 1.7, 0.05)
        return False

    @staticmethod
    def _ordenar(cuentas: list[Usuario]) -> list[Usuario]:
        # Sin distinguir mayusculas: en una lista ordenada por bytes, 'Zoe'
        # saldria antes que 'ana' y quien busca un nombre no lo encuentra.
        return sorted(cuentas, key=lambda u: u.nombre.lower())

    @staticmethod
    def _copia(cuenta: Usuario) -> Usuario:
        """Copia PROFUNDA para entregar fuera.

        Usuario es mutable, y ahora ademas lleva listas y un Alcance dentro: si
        se devolviera el objeto del cache, cualquiera podria anadirse un
        permiso o una empresa al vuelo y el cambio viviria en memoria sin pasar
        por las validaciones ni por la invariante ni llegar al disco.
        """
        return Usuario(
            nombre=cuenta.nombre,
            rol=cuenta.rol,
            clave_hash=cuenta.clave_hash,
            creado=cuenta.creado,
            ultimo_acceso=cuenta.ultimo_acceso,
            permisos_mas=list(cuenta.permisos_mas),
            permisos_menos=list(cuenta.permisos_menos),
            alcance=Alcance.desde_dict(cuenta.alcance.como_dict()),
        )

    def _buscar(self, nombre: str) -> Usuario | None:
        """La cuenta viva del cache. Debe llamarse con el lock tomado."""
        clave = (nombre or "").strip().lower()
        if not clave:
            return None
        for cuenta in self._cache:
            if cuenta.nombre.lower() == clave:
                return cuenta
        return None

    def _exigir_sano(self) -> None:
        """Corta cualquier escritura si el archivo no se entiende.

        Guardar encima de un archivo corrupto seria borrar cuentas que quiza se
        pueden recuperar editandolo a mano. Mejor un error claro.
        """
        if self._corrupto:
            raise ErrorUsuarios(
                f"el archivo de usuarios {self.ruta} no se pudo leer o esta "
                "corrupto; revisalo (o borralo para empezar de cero) antes de "
                "tocar las cuentas"
            )

    # --- Escritura ----------------------------------------------------------

    @staticmethod
    def _fila(cuenta: Usuario) -> dict:
        return {
            "nombre": cuenta.nombre,
            "rol": cuenta.rol,
            "clave_hash": cuenta.clave_hash,
            "creado": cuenta.creado,
            "ultimo_acceso": cuenta.ultimo_acceso,
            "permisos_mas": list(cuenta.permisos_mas),
            "permisos_menos": list(cuenta.permisos_menos),
            "alcance": cuenta.alcance.como_dict(),
        }

    def _escribir(
        self,
        cuentas: list[Usuario],
        roles: dict[str, list[str]] | None = None,
    ) -> None:
        """Vuelca cuentas y roles. Debe llamarse con el lock tomado.

        Escritura atomica (temporal en la misma carpeta + os.replace), igual
        que inventory.guardar: el panel puede estar leyendo el archivo desde
        otro hilo o desde el otro proceso justo cuando se guarda un alta, y
        nadie debe encontrarse un JSON a medias. El temporal va en la misma
        carpeta porque os.replace solo es atomico dentro del mismo sistema de
        archivos.

        Un fallo de escritura SI se propaga (ErrorUsuarios): si el alta no se
        guardo, quien la hizo tiene que enterarse en vez de creer que ya tiene
        cuenta.
        """
        ordenadas = self._ordenar(cuentas)
        nuevos_roles = self._roles if roles is None else roles
        marca_previa = self._marca
        datos = {
            "formato": FORMATO,
            "roles": {k: list(v) for k, v in sorted(nuevos_roles.items())},
            "usuarios": [self._fila(u) for u in ordenadas],
        }

        temporal = ""
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.ruta.parent,
                prefix=self.ruta.name + ".",
                suffix=".tmp",
                delete=False,
            ) as fh:
                json.dump(datos, fh, ensure_ascii=False, indent=2)
                fh.flush()
                # Sin fsync, un corte de luz justo despues del replace puede
                # dejar el archivo nuevo vacio: el rename llega al disco antes
                # que los datos, y eso aqui es quedarse sin cuentas.
                os.fsync(fh.fileno())
                temporal = fh.name

            # El archivo lleva los hashes de las contrasenas de todo el mundo:
            # solo su dueno debe poder leerlo. Se hace sobre el TEMPORAL y
            # ANTES del replace porque si no, el archivo definitivo existiria
            # un instante con los permisos por defecto (0644 con el umask
            # habitual) y en ese hueco cualquiera del sistema podria copiarlo.
            try:
                os.chmod(temporal, 0o600)
            except OSError:
                # En Windows chmod es casi simbolico (solo toca el bit de solo
                # lectura) y puede fallar segun el sistema de archivos. No es
                # motivo para perder el cambio: donde esto importa de verdad,
                # que es el Debian del servicio, funciona.
                pass

            os.replace(temporal, self.ruta)
        except OSError as exc:
            if temporal:
                try:
                    os.unlink(temporal)
                except OSError:
                    pass
            raise ErrorUsuarios(
                f"no se pudo guardar el archivo de usuarios {self.ruta}: {exc}"
            ) from exc

        # El cache queda al dia sin releer, y con la marca del archivo que
        # acabamos de dejar para que _asegurar no lo relea por gusto.
        self._cache = ordenadas
        self._roles = {k: list(v) for k, v in nuevos_roles.items()}
        self._marca = self._forzar_marca(marca_previa)
        self._cargado = self._marca is not None
        self._corrupto = False

    def _forzar_marca(self, previa: tuple[int, int] | None) -> tuple[int, int] | None:
        """La marca del archivo recien escrito, garantizando que CAMBIO.

        Sin esto hay un caso que se cuela: cambiar una contrasena deja el
        archivo exactamente del mismo tamano (el hash pbkdf2 siempre mide lo
        mismo), y en Windows el reloj del sistema avanza a saltos de unos 15 ms,
        asi que dos escrituras seguidas pueden compartir el mtime al nanosegundo.
        Cuando coinciden las dos cosas, la marca queda IGUAL que antes y el otro
        proceso (el planificador, o el panel arrancado aparte) no se entera de
        nada: seguiria dando por buena la contrasena vieja hasta la siguiente
        escritura, que es justo lo contrario de lo que se pide al cambiarla.

        Se corrige empujando el mtime un microsegundo por delante del anterior.
        Es un adelanto invisible para cualquier otro uso de la fecha y deja la
        marca estrictamente distinta, que es lo unico que hace falta.
        """
        marca = self._marca_actual()
        if previa is None or marca is None or marca != previa:
            return marca
        try:
            adelante = previa[0] + 1_000
            os.utime(self.ruta, ns=(adelante, adelante))
        except OSError as exc:
            # No se pierde el cambio por esto: el archivo ya esta escrito. Lo
            # unico que puede pasar es que otro proceso tarde en verlo.
            log.warning("No se pudo adelantar la fecha de %s: %s", self.ruta, exc)
            return marca
        return self._marca_actual()

    # --- Consulta -----------------------------------------------------------

    def listar(self) -> list[Usuario]:
        """Todas las cuentas, ordenadas por nombre.

        Si el archivo esta corrupto devuelve vacio en vez de lanzar: el panel
        tiene que poder pintar la pagina y decir "no hay cuentas" en lugar de
        responder un error 500 que no explica nada. El motivo queda en el log.
        """
        with self._lock:
            self._asegurar()
            return [self._copia(u) for u in self._cache]

    def obtener(self, nombre: str) -> Usuario | None:
        """La cuenta con ese nombre (sin distinguir mayusculas), o None."""
        with self._lock:
            self._asegurar()
            cuenta = self._buscar(nombre)
            return self._copia(cuenta) if cuenta else None

    # --- Permisos -----------------------------------------------------------

    @staticmethod
    def _permisos_de(usuario: Usuario, roles: dict[str, list[str]]) -> set[str]:
        """Los permisos efectivos de una cuenta con ESE juego de roles.

        Es estatico y recibe los roles a proposito: asi sirve tanto para
        contestar "que puede hacer ahora" como para preguntar "que podria hacer
        SI guardara este rol", que es como se comprueba la invariante antes de
        tocar nada (ver _romperia).

        Un rol que no este en la tabla no da NADA. Es deliberado: el rol sale de
        un archivo JSON que se puede editar a mano y que puede corromperse, asi
        que si alguien escribe 'administrador' o 'Admin' en vez de 'admin', el
        fallo tiene que ser NEGAR el acceso y no concederlo.
        """
        if usuario is None:
            return set()
        base = set(roles.get(usuario.rol, ()))
        efectivos = base | set(usuario.permisos_mas or ())
        # permisos_menos se aplica AL FINAL y gana siempre, incluso sobre lo que
        # anade permisos_mas: quitar es la operacion segura, asi que ante un
        # conflicto se queda lo restrictivo.
        efectivos -= set(usuario.permisos_menos or ())
        # Un permiso viejo guardado en el archivo (o en las excepciones de una
        # cuenta) se traduce aqui tambien, y no solo al migrar el archivo: entre
        # que se actualiza el codigo y se reescribe el JSON hay un rato, y
        # durante ese rato nadie puede quedarse fuera.
        efectivos = _traducir(efectivos)
        # Lo que no esta en PERMISOS se ignora en vez de reventar: el dia que se
        # retire un permiso, quien lo tuviera guardado tiene que seguir
        # cargando y entrando (ver el docstring del modulo).
        efectivos = {p for p in efectivos if p in PERMISOS}
        # Y al final se arrastra lo que hace falta para poder USAR lo concedido:
        # quien puede editar equipos tiene que ver la lista de equipos. Va
        # DESPUES de permisos_menos a proposito; quitar 'equipos.ver' a quien
        # puede editar no es una restriccion, es una pantalla rota.
        for permiso in list(efectivos):
            efectivos.update(HERENCIA.get(permiso, ()))
        return efectivos

    def permisos(self, usuario: Usuario) -> set[str]:
        """Los permisos efectivos de esa cuenta, con los roles de ahora mismo."""
        with self._lock:
            self._asegurar()
            return self._permisos_de(usuario, self._roles)

    def puede(self, usuario: Usuario, permiso: str) -> bool:
        """Si esa cuenta tiene ese permiso.

        Recibe el USUARIO y no el nombre del rol: con roles editables y con
        excepciones por cuenta, el rol por si solo ya no contesta la pregunta.

        Un permiso desconocido siempre es False: un typo en una llamada
        ('usuario' por 'usuarios') deja la puerta cerrada, no abierta.
        """
        if not permiso:
            return False
        return permiso in self.permisos(usuario)

    # --- Autenticacion ------------------------------------------------------

    def _hash_descarte(self) -> str:
        """Hash de una clave que no es de nadie, para gastar tiempo.

        Se calcula una vez por proceso y sobre una clave aleatoria: no hace
        falta que verifique nunca, solo que cueste lo mismo que un hash de
        verdad.
        """
        if not self._descarte:
            self._descarte = hashear(
                secrets.token_urlsafe(16), iteraciones=self._iteraciones
            )
        return self._descarte

    def autenticar(self, nombre: str, clave: str) -> Usuario | None:
        """Comprueba usuario y clave. Devuelve la cuenta o None.

        Si acierta, se apunta el ultimo acceso.
        """
        with self._lock:
            self._asegurar()
            corrupto = self._corrupto
            cuenta = self._buscar(nombre)
            guardado = cuenta.clave_hash if cuenta else ""

        if corrupto:
            # Con el archivo roto NO se autentica a nadie. Es lo unico seguro:
            # la alternativa (dejar pasar, o caer de vuelta al usuario de
            # config.yaml) convertiria un archivo estropeado en una puerta
            # abierta, y borrar el archivo seria entonces la forma mas comoda
            # de entrar. Que el panel quede inaccesible es molesto, pero se
            # arregla desde SSH y queda dicho en el log.
            log.error("Login imposible: el archivo de usuarios %s no se puede leer",
                      self.ruta)
            return None

        # pbkdf2 se calcula FUERA del lock: son cientos de milisegundos, y con
        # el lock tomado un login dejaria congelado al resto del panel.
        if cuenta is None:
            # Si aqui devolvieramos None sin mas, la respuesta volveria mucho
            # mas rapido que con un usuario que si existe y la clave mal. Esa
            # diferencia se mide desde fuera y delata que cuentas hay, que es
            # justo lo primero que busca quien quiere entrar. Asi que se gasta
            # el mismo tiempo contra un hash de descarte.
            verificar(clave, self._hash_descarte())
            return None

        if not verificar(clave, guardado):
            return None

        with self._lock:
            # Se vuelve a buscar porque entre la comprobacion y esto pudo
            # cambiar el archivo (otro hilo borrando la cuenta, por ejemplo).
            self._asegurar()
            actual = self._buscar(cuenta.nombre)
            if actual is None:
                return None
            actual.ultimo_acceso = _ahora()
            try:
                self._escribir(self._cache)
            except ErrorUsuarios as exc:
                # Que no se pueda anotar la fecha no es motivo para no dejar
                # entrar: el ultimo acceso es informativo, la clave ya era
                # correcta. Se avisa en el log y se sigue.
                log.warning("No se pudo anotar el ultimo acceso de %s: %s",
                            actual.nombre, exc)
            return self._copia(actual)

    # --- Validacion ---------------------------------------------------------

    def _validar_nombre(self, nombre: str, original: str = "") -> list[str]:
        """Errores del nombre, incluido el duplicado. Con el lock tomado."""
        errores: list[str] = []
        if not nombre:
            errores.append("el nombre de usuario es obligatorio")
            return errores
        if " " in nombre:
            errores.append("el nombre de usuario no puede llevar espacios")
        elif not NOMBRE_MINIMO <= len(nombre) <= NOMBRE_MAXIMO:
            errores.append(
                f"el nombre de usuario debe tener entre {NOMBRE_MINIMO} y "
                f"{NOMBRE_MAXIMO} caracteres"
            )
        elif not NOMBRE_VALIDO.match(nombre):
            errores.append(
                "nombre de usuario invalido: solo letras, numeros, punto, "
                "guion y guion bajo"
            )
        elif nombre.lower() != (original or "").lower():
            # El duplicado se mira SIN distinguir mayusculas aunque el nombre
            # se guarde tal como se escribio: tener 'Admin' y 'admin' a la vez
            # es una trampa para quien administra (y para quien intenta entrar,
            # porque autenticar tampoco distingue).
            existente = self._buscar(nombre)
            if existente is not None:
                errores.append(
                    f"ya existe un usuario llamado '{existente.nombre}'; los "
                    "nombres no distinguen mayusculas de minusculas"
                )
        return errores

    @staticmethod
    def _validar_clave(clave: str) -> list[str]:
        # La clave NO se recorta: los espacios pueden formar parte de ella y
        # "arreglarsela" a quien la escribio provoca un fallo de login que no
        # hay forma de diagnosticar mirando nada.
        if len(clave or "") < MINIMO_CLAVE:
            return [f"la contrasena debe tener al menos {MINIMO_CLAVE} caracteres"]
        return []

    def _validar_rol(self, rol: str) -> list[str]:
        """Que el rol exista AHORA. Los roles son datos, no una constante."""
        if rol in self._roles:
            return []
        conocidos = ", ".join(sorted(self._roles)) or "(ninguno)"
        return [f"rol invalido '{rol}': los roles son {conocidos}"]

    @staticmethod
    def _validar_nombre_rol(rol: str) -> list[str]:
        if not rol:
            return ["el nombre del rol es obligatorio"]
        if not ROL_MINIMO <= len(rol) <= ROL_MAXIMO:
            return [
                f"el nombre del rol debe tener entre {ROL_MINIMO} y "
                f"{ROL_MAXIMO} caracteres"
            ]
        if not ROL_VALIDO.match(rol):
            return [
                f"nombre de rol invalido '{rol}': solo letras, numeros, punto, "
                "guion y guion bajo (sin espacios ni tildes)"
            ]
        return []

    @staticmethod
    def _validar_permisos(valores, etiqueta: str) -> tuple[list[str], list[str]]:
        """(lista limpia, errores).

        Aqui SI se rechaza un permiso desconocido, al reves que al leer el
        archivo. Es la misma diferencia que hay entre inventory.cargar y
        inventory.validar_equipo: leyendo hay que degradar para no dejar a
        nadie fuera, pero en un formulario hay alguien delante que puede
        corregir el typo ahora mismo, y guardarle en silencio un permiso que no
        existe le haria creer que concedio algo que no concedio.
        """
        limpios: list[str] = []
        errores: list[str] = []
        for valor in valores or ():
            permiso = str(valor or "").strip()
            if not permiso:
                continue
            if permiso not in PERMISOS:
                errores.append(
                    f"permiso desconocido '{permiso}' en {etiqueta}: los "
                    "permisos son " + ", ".join(PERMISOS)
                )
            elif permiso not in limpios:
                limpios.append(permiso)
        return limpios, errores

    # --- La invariante del ultimo administrador total -----------------------

    @classmethod
    def _es_total(cls, usuario: Usuario, roles: dict[str, list[str]]) -> bool:
        """Si esa cuenta tiene a la vez el permiso 'usuarios' y alcance a todo.

        Es la unica clase de cuenta que puede volver a repartirlo todo: alguien
        con 'usuarios' pero con alcance solo a Acme puede administrar, pero no
        puede devolverle el acceso a Bravo a nadie.
        """
        if not usuario.alcance.todo:
            return False
        tiene = cls._permisos_de(usuario, roles)
        # TODAS las llaves, no una cualquiera: repartir de nuevo exige poder
        # editar cuentas y ademas poder llegar a esa pantalla.
        return all(llave in tiene for llave in LLAVES)

    @classmethod
    def _hay_total(cls, cuentas: list[Usuario], roles: dict[str, list[str]]) -> bool:
        return any(cls._es_total(u, roles) for u in cuentas)

    def _romperia(
        self,
        cuentas: list[Usuario],
        roles: dict[str, list[str]] | None = None,
    ) -> bool:
        """Si ese estado hipotetico dejaria el sistema sin ningun admin total.

        Solo cuenta como "romper" si AHORA MISMO queda alguno. Si el archivo ya
        llego sin ninguno (alguien lo edito a mano y se equivoco), bloquear
        todas las operaciones dejaria el panel congelado sin arreglar nada:
        seria castigar dos veces por el mismo error.
        """
        roles = self._roles if roles is None else roles
        if not self._hay_total(self._cache, self._roles):
            return False
        return not self._hay_total(cuentas, roles)

    def _aviso_total(self, cabeza: str) -> str:
        """Mensaje unico de la invariante. `cabeza` dice que pasaria."""
        return (
            f"{cabeza} no quedaria ningun usuario con el permiso '{LLAVE}' y "
            "acceso a todas las empresas a la vez, que es el unico que puede "
            "crear cuentas y devolverle el acceso a cualquiera: el panel se "
            f"quedaria sin llaves y la unica salida seria editar {self.ruta} a "
            f"mano por SSH. Dale antes el permiso '{LLAVE}' y el alcance a "
            "todas las empresas a otra cuenta."
        )

    # --- Alta, cambios y baja -----------------------------------------------

    def crear(
        self,
        nombre: str,
        clave: str,
        rol: str,
        permisos_mas=(),
        permisos_menos=(),
        alcance=None,
    ) -> list[str]:
        """Da de alta una cuenta. Devuelve los errores; lista vacia = fue bien.

        `alcance=None` significa SIN alcance: la cuenta no ve ningun equipo
        hasta que alguien le diga cuales. Es lo unico defendible en un panel
        multitenant, donde el fallo por omision no puede ser "ve los routers de
        todos los clientes del ISP".

        La unica excepcion es la PRIMERA cuenta del archivo, que recibe alcance
        a todo. Esa es la de arranque (`mkbackup --crear-usuario`, o el rescate
        sobre un archivo vacio) y alguien tiene que quedarse las llaves: la
        invariante impide QUITAR al ultimo administrador total, pero no puede
        impedir crear un sistema que nunca tuvo ninguno. Se limita a "no hay
        ninguna cuenta todavia" a proposito: en cualquier otro momento habria
        alguien administrando, y ampliarle el alcance a una cuenta nueva por su
        cuenta seria darle mas de lo que quien la crea tiene.
        """
        with self._lock:
            self._asegurar()
            self._exigir_sano()

            nombre = (nombre or "").strip()
            rol = _clave_rol(rol)
            clave = clave or ""  # sin strip: ver _validar_clave

            errores = self._validar_nombre(nombre)
            errores += self._validar_clave(clave)
            errores += self._validar_rol(rol)
            mas, fallos = self._validar_permisos(permisos_mas, "permisos_mas")
            errores += fallos
            menos, fallos = self._validar_permisos(permisos_menos, "permisos_menos")
            errores += fallos
            if errores:
                return errores

            if alcance is None:
                nuevo_alcance = Alcance(todo=True) if not self._cache else Alcance()
            else:
                nuevo_alcance = Alcance.desde_dict(alcance)

            self._escribir(
                self._cache
                + [
                    Usuario(
                        nombre=nombre,
                        rol=rol,
                        clave_hash=hashear(clave, iteraciones=self._iteraciones),
                        creado=_ahora(),
                        permisos_mas=mas,
                        permisos_menos=menos,
                        alcance=nuevo_alcance,
                    )
                ]
            )
            log.info("Usuario creado: %s (%s)", nombre, rol)
            return []

    def cambiar_clave(self, nombre: str, clave: str) -> list[str]:
        """Pone una contrasena nueva. La anterior deja de valer al instante."""
        with self._lock:
            self._asegurar()
            self._exigir_sano()

            cuenta = self._buscar(nombre)
            if cuenta is None:
                return [f"no existe el usuario '{(nombre or '').strip()}'"]

            errores = self._validar_clave(clave or "")
            if errores:
                return errores

            cuenta.clave_hash = hashear(clave, iteraciones=self._iteraciones)
            self._escribir(self._cache)
            log.info("Contrasena cambiada para %s", cuenta.nombre)
            return []

    def actualizar(
        self,
        nombre: str,
        rol=None,
        permisos_mas=None,
        permisos_menos=None,
        alcance=None,
    ) -> list[str]:
        """Cambia rol, excepciones de permisos y/o alcance de una cuenta.

        Lo que llegue como None NO se toca: la interfaz puede mandar solo el
        campo que edito, y un formulario que se olvide de un campo no puede
        borrarlo en silencio.

        Aqui viven tres de los cuatro caminos por los que se podria dejar el
        sistema sin administrador total (cambiar el rol, quitar el permiso con
        permisos_menos y recortar el alcance), asi que el estado resultante se
        COMPRUEBA ENTERO antes de tocar nada: no se mira "que campo cambio",
        se calcula como quedaria y se pregunta si sigue habiendo llaves.
        """
        with self._lock:
            self._asegurar()
            self._exigir_sano()

            cuenta = self._buscar(nombre)
            if cuenta is None:
                return [f"no existe el usuario '{(nombre or '').strip()}'"]

            errores: list[str] = []

            nuevo_rol = cuenta.rol
            if rol is not None:
                nuevo_rol = _clave_rol(rol)
                errores += self._validar_rol(nuevo_rol)

            nuevos_mas = list(cuenta.permisos_mas)
            if permisos_mas is not None:
                nuevos_mas, fallos = self._validar_permisos(permisos_mas, "permisos_mas")
                errores += fallos

            nuevos_menos = list(cuenta.permisos_menos)
            if permisos_menos is not None:
                nuevos_menos, fallos = self._validar_permisos(
                    permisos_menos, "permisos_menos"
                )
                errores += fallos

            nuevo_alcance = cuenta.alcance
            if alcance is not None:
                nuevo_alcance = Alcance.desde_dict(alcance)

            if errores:
                return errores

            propuesta = self._copia(cuenta)
            propuesta.rol = nuevo_rol
            propuesta.permisos_mas = nuevos_mas
            propuesta.permisos_menos = nuevos_menos
            propuesta.alcance = nuevo_alcance

            hipotesis = [propuesta if u is cuenta else u for u in self._cache]
            if self._romperia(hipotesis):
                return [self._aviso_total(
                    f"'{cuenta.nombre}' es el unico administrador total que "
                    f"queda: si {self._describir(cuenta, propuesta)},"
                )]

            cuenta.rol = nuevo_rol
            cuenta.permisos_mas = nuevos_mas
            cuenta.permisos_menos = nuevos_menos
            cuenta.alcance = nuevo_alcance
            self._escribir(self._cache)
            log.info("Usuario actualizado: %s (rol %s, +%s, -%s, alcance %s)",
                     cuenta.nombre, cuenta.rol, nuevos_mas, nuevos_menos,
                     "todo" if nuevo_alcance.todo else "limitado")
            return []

    @staticmethod
    def _describir(antes: Usuario, despues: Usuario) -> str:
        """Que se estaba intentando cambiar, para el mensaje de la invariante.

        El mensaje tiene que decir exactamente que pasaria; "no se puede" a
        secas obliga a adivinar cual de los tres campos fue el problema.
        """
        cambios = []
        if antes.rol != despues.rol:
            cambios.append(f"le cambias el rol a '{despues.rol}'")
        if set(antes.permisos_mas) != set(despues.permisos_mas):
            cambios.append("le cambias los permisos anadidos")
        if set(antes.permisos_menos) != set(despues.permisos_menos):
            cambios.append("le quitas permisos")
        if antes.alcance != despues.alcance:
            cambios.append("le recortas el alcance")
        if not cambios:
            return "guardas ese cambio"
        return " y ".join(cambios)

    def cambiar_rol(self, nombre: str, rol: str) -> list[str]:
        """Cambia el rol de una cuenta. Atajo de `actualizar(nombre, rol=...)`.

        Se mantiene porque ya hay codigo que la llama; toda la logica (incluida
        la invariante) vive en `actualizar`.
        """
        return self.actualizar(nombre, rol=rol)

    def borrar(self, nombre: str) -> list[str]:
        """Da de baja una cuenta, sin dejar el sistema sin administrador total."""
        with self._lock:
            self._asegurar()
            self._exigir_sano()

            cuenta = self._buscar(nombre)
            if cuenta is None:
                return [f"no existe el usuario '{(nombre or '').strip()}'"]

            quedan = [u for u in self._cache if u is not cuenta]
            if self._romperia(quedan):
                return [self._aviso_total(
                    f"'{cuenta.nombre}' es el unico administrador total que "
                    "queda: si lo borras,"
                )]

            self._escribir(quedan)
            log.info("Usuario borrado: %s", cuenta.nombre)
            return []

    # --- Roles --------------------------------------------------------------

    def roles(self) -> dict[str, list[str]]:
        """Los roles y sus permisos, ordenados por nombre. Copia."""
        with self._lock:
            self._asegurar()
            return {k: list(v) for k, v in sorted(self._roles.items())}

    def guardar_rol(self, nombre: str, permisos) -> list[str]:
        """Crea o actualiza un rol. Devuelve los errores.

        Editar un rol es el camino INDIRECTO para quedarse sin llaves: quitarle
        el permiso 'usuarios' al unico rol que lo daba deja a todos sus
        usuarios sin poder gestionar nada, sin haber tocado ninguna cuenta. Por
        eso aqui tambien se comprueba la invariante, sobre el juego de roles que
        quedaria.
        """
        with self._lock:
            self._asegurar()
            self._exigir_sano()

            nombre = _clave_rol(nombre)
            errores = self._validar_nombre_rol(nombre)
            limpios, fallos = self._validar_permisos(permisos, f"el rol '{nombre}'")
            errores += fallos
            if errores:
                return errores

            propuesta = {k: list(v) for k, v in self._roles.items()}
            propuesta[nombre] = limpios

            if self._romperia(self._cache, propuesta):
                return [self._aviso_total(
                    f"el rol '{nombre}' es el unico que da el permiso "
                    f"'{LLAVE}' a un usuario con acceso a todas las empresas: "
                    "si lo guardas asi,"
                )]

            self._escribir(self._cache, propuesta)
            log.info("Rol guardado: %s (%s)", nombre, ", ".join(limpios) or "sin permisos")
            return []

    def borrar_rol(self, nombre: str) -> list[str]:
        """Borra un rol. Falla si alguien lo tiene puesto, y nunca borra 'admin'."""
        with self._lock:
            self._asegurar()
            self._exigir_sano()

            nombre = _clave_rol(nombre)
            if nombre not in self._roles:
                return [f"no existe el rol '{nombre}'"]

            if nombre == ROL_ADMIN:
                # Aunque ahora mismo no lo tuviera nadie: 'admin' es el punto de
                # partida conocido al que se vuelve cuando alguien se lia
                # editando roles, y recrearlo a mano permiso a permiso desde un
                # panel al que quiza ya no se puede entrar no es un plan.
                return [
                    f"el rol '{ROL_ADMIN}' no se puede borrar: es el rol de "
                    "referencia para recuperar el control del panel. Si no lo "
                    "quieres usar, quitaselo a los usuarios que lo tengan"
                ]

            usados = [u.nombre for u in self._cache if u.rol == nombre]
            if usados:
                return [
                    f"el rol '{nombre}' lo tienen {len(usados)} usuario(s) "
                    f"({', '.join(sorted(usados, key=str.lower))}): cambiales "
                    "el rol antes de borrarlo"
                ]

            propuesta = {k: list(v) for k, v in self._roles.items() if k != nombre}
            # Con la comprobacion de arriba esto no deberia dispararse nunca
            # (nadie tiene el rol, asi que quitarlo no cambia los permisos de
            # nadie), pero la invariante se comprueba en TODAS las operaciones
            # que tocan roles o cuentas: la proxima vez que alguien relaje la
            # regla de "esta en uso" no habra que acordarse de esta.
            if self._romperia(self._cache, propuesta):
                return [self._aviso_total(
                    f"si borras el rol '{nombre}',"
                )]

            self._escribir(self._cache, propuesta)
            log.info("Rol borrado: %s", nombre)
            return []

    # --- Migracion ----------------------------------------------------------

    def sembrar(self, nombre: str, clave_hash: str) -> bool:
        """Crea el archivo con la cuenta de config.yaml como admin total.

        Es la migracion automatica de quien ya tenia el panel funcionando con
        `web.usuario` / `web.clave_hash`: sin esto, actualizar dejaria a la
        gente fuera de su propio panel, con un archivo de usuarios vacio y
        ninguna forma de entrar a crear el primero.

        La cuenta se crea con el rol 'admin' y con `alcance.todo = True`: era la
        UNICA cuenta del panel y lo veia todo, asi que cualquier otra cosa seria
        quitarle acceso al actualizar. Ademas es la que sostiene la invariante
        del ultimo administrador total en una instalacion recien migrada.

        Devuelve True si creo el archivo y False si ya habia cuentas (entonces
        no toca nada). Es idempotente: llamarla en cada arranque es lo normal.

        El clave_hash que llega YA es un pbkdf2 de sesion.hashear, sacado del
        YAML: NO se vuelve a hashear.

        No se valida el nombre con las reglas de crear(): la cuenta que ya
        existia tiene que seguir funcionando aunque hoy no pasaria el alta (un
        nombre de dos letras, por ejemplo). Migrar no es dar de alta.
        """
        with self._lock:
            self._asegurar()
            # Si el archivo esta corrupto no se siembra encima: se perderian
            # las cuentas que quiza se pueden rescatar a mano.
            self._exigir_sano()

            if self._cache:
                return False

            nombre = (nombre or "").strip()
            clave_hash = (clave_hash or "").strip()
            if not nombre or not clave_hash:
                # Sin usuario en config.yaml no hay nada que migrar. El panel
                # ya se niega a arrancar sin clave_hash (ver web.py), asi que
                # esto solo pasa en pruebas o en una instalacion a medias.
                return False

            if not formato_valido(clave_hash):
                # Se siembra igual (es lo unico que hay), pero conviene que
                # quede dicho: con un hash mal copiado nadie va a poder entrar
                # y el motivo no se ve por ningun lado.
                log.warning(
                    "El clave_hash de config.yaml no tiene el formato esperado; "
                    "la cuenta '%s' se creara pero no podra entrar", nombre
                )

            self._escribir(
                [
                    Usuario(
                        nombre=nombre,
                        rol=ROL_ADMIN,
                        clave_hash=clave_hash,
                        creado=_ahora(),
                        alcance=Alcance(todo=True),
                    )
                ]
            )
            log.info("Usuarios sembrados desde config.yaml con la cuenta '%s'", nombre)
            return True


def _roles_iniciales() -> dict[str, list[str]]:
    """Copia fresca de ROLES_INICIALES.

    Copia y no la constante: los roles se editan en memoria antes de guardarse,
    y un descuido dejaria ROLES_INICIALES modificada para todo el proceso.
    """
    return {nombre: list(permisos) for nombre, permisos in ROLES_INICIALES.items()}


def _traducir(permisos) -> set[str]:
    """Cambia los permisos viejos por los nuevos. Idempotente.

    Un permiso que ya sea de los nuevos pasa tal cual, asi que se puede llamar
    tantas veces como haga falta sin que cambie el resultado. Eso importa
    porque se llama en dos sitios: al migrar el archivo (una vez) y al calcular
    los permisos efectivos (en cada peticion).
    """
    salida: set[str] = set()
    for permiso in permisos or ():
        salida.update(EQUIVALENCIAS.get(permiso, (permiso,)))
    return salida


def _lista_texto(valores) -> list[str]:
    """Una lista de cadenas no vacias, sin repetidos y tolerante con la basura."""
    if not isinstance(valores, (list, tuple)):
        return []
    limpia: list[str] = []
    for valor in valores:
        texto = str(valor or "").strip()
        if texto and texto not in limpia:
            limpia.append(texto)
    return limpia
