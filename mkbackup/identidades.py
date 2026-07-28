"""Preguntarle a los routers como se llaman, de uno en uno o toda la flota.

El nombre de un equipo en mkbackup no es un adorno: es la ruta de su archivo
dentro del repositorio. Cambiarlo mueve su historico. Por eso preguntarselo al
propio RouterOS (/system identity) es lo correcto -nadie se equivoca menos que
el equipo sobre como se llama- y por eso mismo hay que hacerlo con cuidado.

Este modulo existe por una razon concreta: preguntarselo a UNO cabe en una
peticion web (es lo que hace el boton de la ficha), pero preguntarselo a la
flota entera NO. Trescientos equipos, con los apagados agotando el timeout de
SSH uno detras de otro, son minutos. Un navegador se rinde antes, y el
manejador del panel corta a los 30 segundos. Asi que el sondeo masivo corre en
segundo plano y la pantalla lo mira; no lo espera.

Lo que NO hace, y es deliberado:

  - No renombra a ciegas. Cada nombre que llega de un router pasa por la misma
    validacion que un alta a mano (inventory.validar_equipo): un equipo que
    dice llamarse como otro que ya existe compartiria archivo en el repo y los
    dos se pisarian los respaldos.
  - No pierde el historico. Primero se mueve el archivo con git (Almacen.
    renombrar, que usa 'git mv' para que --follow siga el rastro hacia atras) y
    solo si eso sale bien se toca el inventario. Al reves, el inventario
    apuntaria a un respaldo que no esta.
  - No pierde las credenciales del equipo. Renombrar es cambiar UN campo; el
    usuario, la clave y el intervalo propios siguen ahi. Suena obvio y no lo
    era: el renombrado automatico que ya existia los dejaba en blanco, o sea
    que el primer respaldo bueno de un router con clave propia le quitaba la
    clave y el siguiente ciclo fallaba autenticando.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import inventory
from .planificador import estado_programador
from .store import Almacen

log = logging.getLogger(__name__)


# A que equipos se les pregunta. Son los dos unicos valores que acepta el
# panel; cualquier otro se trata como el primero, que es el prudente.
SOLO_SIN_NOMBRE = "provisionales"
TODOS = "todos"
ALCANCES = (SOLO_SIN_NOMBRE, TODOS)

# Cuantos equipos se nombran en el detalle de la pantalla. Con una flota de
# cientos, la lista completa de los que no contestaron no es informacion, es
# ruido: las cuentas van enteras y los nombres se cortan aqui. Quien necesite
# el listado largo lo tiene en Equipos, filtrando por los que fallan.
MAXIMO_DETALLE = 25


def es_provisional(equipo) -> bool:
    """Un equipo que se llama como su propia IP: nadie supo ponerle nombre.

    Se admiten las dos formas porque el alta las escribe distintas: una IPv4
    entra tal cual y una IPv6 con los dos puntos cambiados por guiones, que es
    lo unico que vale como nombre de archivo.
    """
    return equipo.nombre in (equipo.ip, equipo.ip.replace(":", "-"))


def a_quien_preguntar(equipos, alcance: str) -> list:
    """Los equipos del alcance pedido, en el orden en que estan."""
    if alcance == TODOS:
        return list(equipos)
    return [e for e in equipos if es_provisional(e)]


def renombrado(equipo, identidad: str, equipos) -> tuple:
    """El mismo equipo con el nombre que dijo el router. (None, errores) si no vale.

    Conserva TODOS los campos del equipo y cambia solo el nombre. La lista
    `equipos` es el inventario completo: de ahi salen los nombres ya ocupados,
    y `original` es lo que permite que el propio equipo no cuente como choque
    consigo mismo.
    """
    return inventory.validar_equipo(
        nombre=identidad,
        empresa=equipo.empresa,
        ip=equipo.ip,
        puerto=equipo.puerto,
        grupo=equipo.grupo,
        existentes=[e.nombre for e in equipos],
        original=equipo.nombre,
        intervalo=equipo.intervalo_minutos,
        usuario=equipo.usuario,
        clave=equipo.clave,
    )


# --- El sondeo masivo -------------------------------------------------------


@dataclass
class Sondeo:
    """Como va (o como fue) la ultima consulta masiva de nombres.

    Lo escribe el hilo que sondea y lo leen los hilos que sirven la pagina, asi
    que todo pasa por `_candado`. Es un objeto y no un archivo a proposito: si
    el panel se reinicia en medio, lo que se pierde es la barra de progreso, no
    los renombrados, que ya estan en el inventario y en git.
    """

    quien: str
    alcance: str
    total: int
    inicio: str = ""
    fin: str = ""
    preguntados: int = 0
    aplicados: int = 0
    # "preguntando" mientras se abren sesiones SSH, "renombrando" mientras se
    # mueven archivos y se reescribe el inventario. Existe porque la segunda
    # fase es serie y con una flota grande tarda: sin decir en cual va, la
    # pantalla se queda en N de N sin moverse, que no se distingue de un hilo
    # colgado.
    fase: str = "preguntando"
    corriendo: bool = True
    error: str = ""
    aviso: str = ""

    # Si el motivo de un rechazo se puede contar entero. Cuando quien lanzo el
    # sondeo no alcanza a la flota entera, NO: el motivo mas comun es "ese
    # nombre ya lo tiene otro equipo", y ese texto, con el nombre dentro,
    # confirma que existe un equipo de OTRO cliente. Es la misma razon por la
    # que el panel contesta 404 y no 403 sobre un equipo fuera de alcance.
    detallado: bool = True

    # Las CUENTAS van siempre enteras; las listas son solo el detalle que se
    # ensena, y se cortan en MAXIMO_DETALLE. Estan separadas a proposito: un
    # contador que se parara en 25 mentiria sobre el tamano de la flota, y esa
    # cifra es justo la que se mira para decidir si el sondeo salio bien.
    n_renombrados: int = 0
    n_mudos: int = 0
    n_rechazados: int = 0
    iguales: int = 0
    renombrados: list = field(default_factory=list)
    mudos: list = field(default_factory=list)
    rechazados: list = field(default_factory=list)

    _candado: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @staticmethod
    def _apuntar(lista: list, dato) -> None:
        if len(lista) < MAXIMO_DETALLE:
            lista.append(dato)

    def contestado(self) -> None:
        with self._candado:
            self.preguntados += 1

    def renombrando(self) -> None:
        with self._candado:
            self.fase = "renombrando"

    def aplicado(self) -> None:
        with self._candado:
            self.aplicados += 1

    def avisar(self, texto: str) -> None:
        with self._candado:
            self.aviso = texto

    def igual(self) -> None:
        with self._candado:
            self.iguales += 1

    def mudo(self, nombre: str) -> None:
        with self._candado:
            self.n_mudos += 1
            self._apuntar(self.mudos, nombre)

    def renombro(self, viejo: str, nuevo: str) -> None:
        with self._candado:
            self.n_renombrados += 1
            self._apuntar(self.renombrados, (viejo, nuevo))

    def rechazo(self, nombre: str, motivo: str, reservado: bool = False) -> None:
        """Un equipo que dijo un nombre que no se le pudo poner.

        `reservado` marca los motivos que hablan del resto de la flota (un
        choque de nombres). Se guardan enteros para el log y se recortan al
        pintarlos si quien mira no alcanza a toda la flota.
        """
        with self._candado:
            self.n_rechazados += 1
            if reservado and not self.detallado:
                motivo = "el nombre que dijo no se puede usar"
            self._apuntar(self.rechazados, (nombre, motivo))

    def fallo(self, mensaje: str) -> None:
        with self._candado:
            self.error = mensaje

    def terminar(self, cuando: str = "") -> None:
        with self._candado:
            self.corriendo = False
            self.fase = ""
            self.fin = cuando

    def instantanea(self) -> dict:
        """Copia de todo lo que hay ahora, para el JSON del panel."""
        with self._candado:
            return {
                "quien": self.quien,
                "alcance": self.alcance,
                "total": self.total,
                "inicio": self.inicio,
                "fin": self.fin,
                "preguntados": self.preguntados,
                "aplicados": self.aplicados,
                "fase": self.fase,
                "corriendo": self.corriendo,
                "error": self.error,
                "aviso": self.aviso,
                "iguales": self.iguales,
                "renombrados": self.n_renombrados,
                "mudos": self.n_mudos,
                "rechazados": self.n_rechazados,
                "detalle_renombrados": [list(p) for p in self.renombrados],
                "detalle_mudos": list(self.mudos),
                "detalle_rechazados": [list(p) for p in self.rechazados],
            }


_CANDADO_ESTADO = threading.Lock()
_ACTUAL: Sondeo | None = None


def en_curso() -> Sondeo | None:
    """El sondeo de ahora mismo, o None si no hay ninguno corriendo."""
    with _CANDADO_ESTADO:
        if _ACTUAL is not None and _ACTUAL.corriendo:
            return _ACTUAL
    return None


def ultimo() -> Sondeo | None:
    """El ultimo sondeo, este corriendo o ya terminado."""
    with _CANDADO_ESTADO:
        return _ACTUAL


def olvidar() -> None:
    """Borra el ultimo sondeo. Lo usan las pruebas y el borrado de una cuenta.

    Un sondeo terminado se guarda a nombre de quien lo lanzo y se le ensena por
    ese nombre; si la cuenta desaparece, lo que colgaba de ella tambien.
    """
    global _ACTUAL
    with _CANDADO_ESTADO:
        _ACTUAL = None


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def lanzar(cfg, candado, hechos, equipos, quien: str, alcance: str,
           detallado: bool = True) -> Sondeo | None:
    """Arranca el sondeo en segundo plano. None si ya habia uno corriendo.

    `candado` es el mismo cerrojo con el que el panel serializa las escrituras
    del inventario: sin el, un alta a mano hecha mientras esto corre se
    perderia (los dos leen la misma lista y el ultimo que guarda gana).

    Quien llama TIENE que mirar el valor devuelto. El None no es un detalle:
    es la unica forma de saber que el sondeo es de otro, y darlo por bueno
    significa decirle a alguien "preguntando a 120 equipos" cuando no se
    arranco nada con su alcance.
    """
    global _ACTUAL
    with _CANDADO_ESTADO:
        if _ACTUAL is not None and _ACTUAL.corriendo:
            return None
        sondeo = Sondeo(
            quien=quien, alcance=alcance, total=len(equipos),
            inicio=_ahora(), detallado=detallado,
        )
        hilo = threading.Thread(
            target=_correr,
            args=(cfg, candado, hechos, list(equipos), sondeo),
            name="sondeo-identidades",
            daemon=True,
        )
        try:
            hilo.start()
        except RuntimeError:
            # Sin hilos libres (o con el interprete apagandose) no arranca. Si
            # _ACTUAL ya estuviera puesto, se quedaria con corriendo=True y sin
            # nadie detras que lo cierre: en_curso() no volveria a decir None
            # nunca y el boton quedaria muerto hasta reiniciar el servicio.
            # Por eso se publica DESPUES de que el hilo exista de verdad.
            log.exception("No se pudo arrancar el hilo del sondeo")
            return None
        _ACTUAL = sondeo
    return sondeo


def _correr(cfg, candado, hechos, equipos, sondeo: Sondeo) -> None:
    try:
        respuestas = _preguntar_a_todos(cfg, equipos, sondeo)
        _aplicar(cfg, candado, hechos, respuestas, sondeo)
    except Exception:  # noqa: BLE001
        # Este hilo no tiene a nadie encima que recoja la excepcion: si se
        # escapa, muere en silencio y la pantalla se queda con la barra a
        # medias para siempre. Se anota y se cierra el sondeo.
        log.exception("El sondeo de identidades se corto")
        sondeo.fallo("Se corto por un error inesperado. Mira el registro del servicio.")
    finally:
        sondeo.terminar(_ahora())


def _preguntar_a_todos(cfg, equipos, sondeo: Sondeo) -> list:
    """[(equipo, identidad)] para todos. La identidad es '' si no contesto.

    En paralelo con el mismo tope que los respaldos: son sesiones SSH contra
    los mismos equipos y no hay razon para ser mas agresivo aqui que en el
    ciclo normal.
    """
    if not equipos:
        return []

    # Import perezoso: device arrastra paramiko. El panel lo carga solo cuando
    # de verdad va a hablar con un router.
    from . import device

    def preguntar(equipo):
        try:
            nombre = device.identidad(equipo, cfg) or ""
        except Exception:  # noqa: BLE001
            # device.identidad promete no lanzar; esto es por si esa promesa se
            # rompe algun dia. Un equipo raro no puede llevarse el sondeo por
            # delante y dejar sin preguntar a los 200 que van detras.
            log.exception("%s: fallo inesperado preguntando la identidad", equipo)
            nombre = ""
        sondeo.contestado()
        return equipo, nombre

    hilos = max(1, min(int(getattr(cfg, "concurrencia", 1) or 1), len(equipos)))
    with ThreadPoolExecutor(max_workers=hilos, thread_name_prefix="identidad") as pool:
        return list(pool.map(preguntar, equipos))


def _mismo_aparato(a, b) -> bool:
    """Si dos fichas con el mismo nombre son el mismo equipo de verdad.

    Buscar por nombre a secas no basta, y no es un caso rebuscado: el nombre
    provisional de un equipo es SU IP, y entre que se pregunto y llega el turno
    de aplicar pueden pasar minutos. En ese rato alguien puede dar de baja ese
    equipo y dar de alta otro, de otro cliente, que entre tambien sin nombre.
    La fila se llamaria igual y se le pondria encima el nombre que dijo un
    router que no es ese: un renombrado cruzado entre dos empresas.
    """
    return (a.ip, a.puerto, a.empresa) == (b.ip, b.puerto, b.empresa)


def _aplicar(cfg, candado, hechos, respuestas, sondeo: Sondeo) -> None:
    """Renombra en el inventario y en el repo lo que haya que renombrar.

    Va de uno en uno y cogiendo el cerrojo por equipo, no una vez para todos.
    Con la flota entera, un cerrojo tomado durante todos los 'git mv' dejaria
    el panel sin poder dar de alta nada durante ese rato; y si algo revienta a
    la mitad, lo ya hecho queda guardado en vez de perderse.
    """
    almacen = Almacen(cfg)
    sondeo.renombrando()

    for equipo, identidad in respuestas:
        if not identidad:
            sondeo.mudo(equipo.nombre)
            continue
        if identidad == equipo.nombre:
            sondeo.igual()
            continue

        # Antes de CADA equipo, no una vez al empezar. El panel comprueba que
        # no haya un ciclo en marcha al pulsar el boton, pero el sondeo dura
        # minutos y el programador es otro proceso: puede vencerle el intervalo
        # a mitad. Si eso pasa, los dos reescriben el inventario entero desde su
        # propia copia en memoria y el ultimo que guarda borra el trabajo del
        # otro. Se para: dejar equipos sin renombrar es recuperable (se vuelve a
        # pulsar), perder renombrados ya commiteados en git no.
        if estado_programador(cfg).get("corriendo"):
            log.warning("Sondeo detenido: arranco un ciclo de respaldo")
            sondeo.avisar(
                "Se paro a la mitad porque arranco un respaldo: los dos escriben "
                "el mismo inventario. Vuelve a darle cuando termine el ciclo."
            )
            return

        try:
            with candado:
                _renombrar_uno(cfg, almacen, hechos, equipo, identidad, sondeo)
        except Exception:  # noqa: BLE001
            log.exception("%s: fallo el renombrado", equipo.nombre)
            sondeo.rechazo(equipo.nombre, "error al renombrar, mira el registro")
        finally:
            sondeo.aplicado()


def _renombrar_uno(cfg, almacen, hechos, equipo, identidad: str, sondeo: Sondeo) -> None:
    # El inventario se relee para CADA equipo y no se usa la copia con la que
    # se empezo: entre que arranco el sondeo y llega el turno de este equipo
    # pueden haber pasado minutos, y en ese rato alguien pudo dar de alta,
    # editar o borrar desde el panel. Renombrar sobre una lista vieja
    # resucitaria lo borrado y perderia lo nuevo.
    equipos = inventory.cargar(cfg.inventario, cfg.ssh.puerto_defecto)[0]
    indice = next((i for i, e in enumerate(equipos) if e.nombre == equipo.nombre), None)
    if indice is None:
        sondeo.rechazo(equipo.nombre, "ya no esta en el inventario")
        return

    actual = equipos[indice]
    if not _mismo_aparato(actual, equipo):
        log.warning(
            "%s cambio mientras se sondeaba (%s:%s de '%s' -> %s:%s de '%s'): "
            "no se renombra con lo que contesto el de antes",
            equipo.nombre, equipo.ip, equipo.puerto, equipo.empresa,
            actual.ip, actual.puerto, actual.empresa,
        )
        sondeo.rechazo(equipo.nombre, "cambio mientras se le preguntaba")
        return

    nuevo, errores = renombrado(actual, identidad, equipos)
    if nuevo is None:
        # El caso que mas se ve aqui es el choque de nombres: dos routers de
        # clientes distintos llamados los dos 'MikroTik'. El motivo entero va
        # al log siempre; a la pantalla, solo si quien lo lanzo alcanza a toda
        # la flota (ver Sondeo.detallado).
        motivo = "; ".join(errores) or "el nombre no vale"
        log.warning("%s dice llamarse '%s', y no se puede usar: %s",
                    actual.nombre, identidad, motivo)
        sondeo.rechazo(actual.nombre, motivo, reservado=True)
        return

    # Primero el archivo y despues el inventario, por el mismo motivo de
    # siempre: si el orden fuera el inverso y el 'git mv' fallara, el
    # inventario apuntaria a un respaldo que no existe.
    movido = ""
    if actual.ruta_relativa != nuevo.ruta_relativa:
        # Un equipo puede no tener archivo todavia: entro sin nombre con el
        # router apagado y aun no ha habido un respaldo suyo con exito. Ese es
        # JUSTO el caso para el que existe este boton, y sin esta distincion
        # nunca se renombraria: Almacen.renombrar devuelve False cuando no hay
        # origen, igual que cuando el git mv falla de verdad, y el equipo se
        # rechazaba una y otra vez con "no se pudo mover su historico".
        if not (Path(cfg.almacen.git) / actual.ruta_relativa).is_file():
            log.info("%s no tiene respaldo todavia: no hay nada que mover",
                     actual.nombre)
        elif almacen.renombrar(actual.ruta_relativa, nuevo.ruta_relativa):
            movido = nuevo.ruta_relativa
        else:
            sondeo.rechazo(actual.nombre,
                           "no se pudo mover su historico en el repositorio")
            return

    equipos[indice] = nuevo
    try:
        inventory.guardar(cfg.inventario, equipos)
    except Exception:
        # El archivo ya esta movido y COMMITEADO. Dejarlo asi parte el equipo en
        # dos: el CSV sigue diciendo el nombre viejo, el proximo respaldo no
        # encuentra ese archivo y lo commitea como un alta, y el historico de
        # antes queda huerfano bajo el nombre nuevo sin que --follow llegue a
        # el. Que es exactamente lo que este modulo existe para que no pase. Se
        # deshace el movimiento y el equipo se queda como estaba, entero.
        if movido:
            log.error("%s: no se pudo guardar el inventario; se devuelve el "
                      "respaldo a su sitio", actual.nombre)
            almacen.renombrar(movido, actual.ruta_relativa)
        raise

    # Lo observado (modelo, version, ultimo respaldo) esta indexado por el
    # nombre. Sin esto, un equipo recien renombrado aparece en el panel como si
    # fuera de alta hoy y sin la marca de que llevaba dias fallando.
    if hechos is not None:
        try:
            hechos.renombrar(actual.nombre, nuevo.nombre)
        except Exception:  # noqa: BLE001
            log.exception("%s: no se pudo mover lo observado a '%s'",
                          actual.nombre, nuevo.nombre)

    log.info("Sondeo: %s se llama '%s' (%s -> %s)",
             actual.nombre, nuevo.nombre, actual.ruta_relativa, nuevo.ruta_relativa)
    sondeo.renombro(actual.nombre, nuevo.nombre)
