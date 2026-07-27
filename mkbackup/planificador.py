"""Programador residente: decide CADA CUANTO se busca cambios en la flota.

Antes de esto el disparo lo hacia un timer de systemd. Se cambio porque el
intervalo ahora se configura desde el panel web, y para que el panel pudiera
reescribir una unidad de systemd y recargar el demonio habria que darle root:
demasiado poder para un panel de consulta al que se entra con usuario y clave.

La alternativa es este proceso, que vive siempre, relee la configuracion en
cada vuelta y dispara los ciclos el mismo. Cambiar el intervalo pasa a ser
escribir un JSON (ver Config.guardar_ajustes), algo que el panel puede hacer
sin privilegios y que entra en vigor sin reiniciar nada.

Cada equipo puede llevar su propio ritmo. Un router de una sucursal que no se
toca en meses no tiene por que preguntarse a la misma velocidad que un core:
el inventario admite una columna intervalo_minutos por equipo y, cuando vale
mas que cero, MANDA sobre planificador.intervalo_minutos. Por eso un ciclo ya
no respalda la flota entera, sino solo a los equipos a los que les toca (ver
equipos_pendientes). Si no le toca a nadie no se lanza ciclo: escribir un
estado de "0 equipos" y tocar git para nada solo confunde al que mire el panel.

Reglas que sostienen el diseno:

  - NUNCA dos ciclos a la vez: git no admite dos escrituras sobre el mismo
    indice (index.lock). Por eso el bucle es de un solo hilo y espera a que el
    ciclo termine antes de contar el tiempo del siguiente.
  - La espera se calcula hacia el PRIMER vencimiento de la flota, no hacia el
    intervalo global: con un equipo a 30 minutos y un global de 4 horas, dormir
    4 horas dejaria a ese equipo sin respaldar a tiempo.
  - Cuando se consulto cada equipo por ultima vez se guarda en programador.json
    (clave `ultimas`): un equipo sin cambios no deja ningun commit en git del
    que deducir la fecha, y sin esa memoria un reinicio del servicio volveria a
    respaldar la flota entera.
  - La espera se hace en trozos cortos: asi un SIGTERM para el servicio en
    segundos, y un cambio de intervalo desde el panel se nota enseguida.
  - Si releer la configuracion o el inventario falla (alguien esta editando el
    YAML y lo dejo a medias), se sigue con lo anterior: un typo no puede dejar
    sin respaldos a toda la flota hasta que alguien se de cuenta.

El programador publica su propio estado en programador.json, al lado del
estado de la ejecucion. Lo LEE el panel para poder decir "proximo ciclo en
1h 12m"; nadie mas lo escribe.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("mkbackup.planificador")

# Cada cuanto despierta la espera para mirar si hay que parar o si cambio el
# intervalo. 5 segundos es el compromiso: systemd corta el servicio a los
# pocos segundos de pedirlo (en vez de esperar el timeout con un sleep de 4
# horas colgando) y el coste es una comprobacion barata cada 5s, nada al lado
# de un ciclo que dura minutos.
PASO_ESPERA = 5.0

# Pausa minima entre ciclos cuando el anterior duro MAS que el intervalo. Sin
# ella el programador encadenaria ciclos sin respirar: el servidor no bajaria
# nunca de carga y el log seria ilegible. Con ella se degrada a "lo mas
# seguido que se pueda" y queda constancia en el log de que el intervalo
# configurado no da para la flota que hay.
PAUSA_MINIMA = 60.0

# Suelo del tic: por muy corto que sea el intervalo mas corto del inventario,
# el bucle no vuelve a mirar vencimientos antes de esto. Un equipo puesto a 1
# minuto por error no puede convertir al programador en un bucle que abre SSH
# sin parar; y despertar cada minuto para recorrer una lista en memoria no le
# cuesta nada al servidor.
TIC_MINIMO = 60.0

# Cada cuanto se vuelve a mirar el inventario MIENTRAS no se haya hecho ni un
# ciclo todavia.
#
# Sin esto, la instalacion recien hecha se quedaba muerta cuatro horas. El
# instalador crea inventory.csv con solo la cabecera a proposito (sembrar
# equipos de ejemplo llena el panel de fallos que no son de nadie), asi que el
# programador arranca SIEMPRE con el inventario vacio, no encuentra
# vencimientos que mirar y se duerme el intervalo global. Quien acababa de
# instalar daba de alta su flota desde el panel, no pasaba nada, y lo unico
# que lo desatascaba era reiniciar el servicio. Es el primer cuarto de hora de
# CADA instalacion: el peor sitio posible para que parezca que no funciona.
#
# Solo aplica hasta el primer ciclo. Despues manda el intervalo global otra
# vez, que es lo que evita que un CSV roto en un sistema en marcha deje al
# programador despertando cada minuto para nada.
ESPERA_SIN_ESTRENAR = 30.0

NOMBRE_ARCHIVO = "programador.json"

# Estado que se devuelve cuando todavia no hay archivo (o esta corrupto). Las
# claves son siempre las mismas para que el panel no tenga que preguntar si
# existen antes de leerlas.
ESTADO_VACIO = {
    "proxima": None,
    "ultima": None,
    "intervalo_minutos": 0,
    "corriendo": False,
    "ciclos": 0,
    "ultimo_codigo": None,
    # nombre de equipo -> marca ISO de la ultima vez que se le consulto. Es lo
    # que permite que cada equipo lleve su propio ritmo entre reinicios.
    "ultimas": {},
    # Cuantos equipos entraron en el ultimo ciclo: el panel necesita distinguir
    # "respaldo de 4 equipos" de "respaldo de la flota entera".
    "ultimo_lote": 0,
}


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def ruta_programador(cfg) -> Path:
    """Archivo de estado del programador: al lado del estado de la ejecucion.

    Se deriva de almacen.estado en vez de ser otra opcion de configuracion
    porque son la misma cosa vista de dos maneras, y porque asi basta un
    ReadWritePaths en la unidad de systemd para los dos.
    """
    return Path(cfg.almacen.estado).parent / NOMBRE_ARCHIVO


# --- "Respaldar ahora" ------------------------------------------------------
# El panel y el programador son DOS PROCESOS. El panel no puede lanzar el ciclo
# el mismo: git no admite dos escrituras a la vez sobre el mismo indice
# (index.lock), y todo el diseno de este proyecto se apoya en que solo hay un
# escritor. Un boton que arrancara su propio respaldo corromperia el
# repositorio justo cuando mas gente lo esta usando.
#
# Asi que el panel deja una peticion en disco y el programador la recoge en su
# siguiente vistazo. Ya despierta cada PASO_ESPERA segundos para mirar si hay
# que parar o si cambio el intervalo, o sea que esto no cuesta ni un temporizador
# mas: el retardo maximo son esos mismos segundos.
#
# El archivo se BORRA al recogerlo, y ese borrado es lo que evita que dos clicks
# seguidos encadenen dos ciclos.
NOMBRE_PETICION = "respaldar-ahora"


def ruta_peticion(cfg) -> Path:
    return Path(cfg.almacen.estado).parent / NOMBRE_PETICION


def pedir_ciclo(cfg, quien: str = "") -> bool:
    """Deja pedido un ciclo inmediato. La escribe el PANEL."""
    ruta = ruta_peticion(cfg)
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(
            json.dumps({"quien": quien, "cuando": _ahora()}, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        log.warning("No se pudo dejar la peticion de respaldo: %s", exc)
        return False


def recoger_peticion(cfg) -> str | None:
    """Si hay un ciclo pedido, lo consume y dice quien lo pidio. Lo lee el PROGRAMADOR.

    Devuelve None si no habia nada. El nombre puede venir vacio si el archivo
    estaba a medio escribir: eso NO cancela la peticion, porque el archivo
    existiendo ya significa que alguien le dio al boton, y perder el ciclo por
    no saber el nombre seria cambiar lo importante por lo accesorio.
    """
    ruta = ruta_peticion(cfg)
    try:
        crudo = ruta.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("No se pudo leer %s: %s", ruta, exc)
        return None
    finally:
        # Se borra SIEMPRE que se haya llegado a mirar, incluso si el contenido
        # no se entiende: si no, un archivo ilegible dejaria al programador
        # arrancando un ciclo cada 5 segundos para siempre.
        try:
            ruta.unlink()
        except OSError:
            pass

    try:
        return str(json.loads(crudo).get("quien") or "")
    except (ValueError, AttributeError):
        return ""


def espera_hasta_el_turno(duracion_ciclo: float, intervalo_minutos: int) -> float:
    """Segundos a esperar tras un ciclo que duro `duracion_ciclo` segundos.

    Funcion pura y sin estado a proposito: es la unica cuenta del modulo que
    se puede equivocar de forma silenciosa (un respaldo cada 8 horas cuando se
    pidieron 4 no lo nota nadie hasta que hace falta un respaldo), asi que se
    aisla para poder probarla sin esperar horas.

      - el tiempo del ciclo SE DESCUENTA: con intervalo 240 min y un ciclo de
        6, se esperan 234, no 240. Si no, cada vuelta se retrasaria un poco
        mas y el respaldo iria corriendose de hora.
      - si el ciclo duro tanto o mas que el intervalo, el turno ya se paso: se
        devuelve PAUSA_MINIMA en lugar de 0 para no encadenar ciclos sin
        pausa (ver PAUSA_MINIMA).
    """
    # El intervalo puede venir de un JSON escrito a mano: no fiarse de el.
    intervalo = max(1, int(intervalo_minutos)) * 60.0
    restante = intervalo - max(0.0, duracion_ciclo)
    return restante if restante > 0 else PAUSA_MINIMA


# --- A quien le toca --------------------------------------------------------
# Todo lo de este bloque es PURO: recibe la hora en vez de mirar el reloj y no
# lee ni escribe nada. Es a proposito, porque es la decision que determina si
# un equipo se respalda o no y probarla de verdad exigiria esperar horas.


def _momento(marca) -> datetime | None:
    """Marca ISO -> datetime con zona, o None si no se entiende.

    Devolver None en vez de lanzar es deliberado: la marca sale de un JSON que
    se puede editar a mano, y una fecha corrupta no puede dejar a un equipo sin
    respaldo (quien la llama trata el None como "nunca se le consulto").
    """
    if not marca:
        return None
    try:
        momento = datetime.fromisoformat(str(marca))
    except (TypeError, ValueError):
        return None
    # Marcas sin zona (escritas a mano) se toman como UTC, que es lo que
    # escribe este modulo. Sin esto, restar dos datetime reventaria.
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento


def intervalo_efectivo(equipo, intervalo_global: int) -> int:
    """Minutos entre consultas de UN equipo: el suyo si tiene, si no el global.

    El del equipo manda porque lo puso alguien mirando ese equipo en concreto;
    el global es solo el valor por defecto de la flota. 0 (o vacio, o basura en
    el CSV) significa "usa el global", no "consultalo sin parar".
    """
    # getattr y no equipo.intervalo_minutos: aqui tambien llegan dobles de
    # prueba, y un inventario viejo puede traer objetos sin la columna.
    try:
        propio = int(getattr(equipo, "intervalo_minutos", 0) or 0)
    except (TypeError, ValueError):
        propio = 0
    if propio > 0:
        return propio

    try:
        general = int(intervalo_global)
    except (TypeError, ValueError):
        general = 0
    # Nunca menos de 1 minuto: el global puede venir de un JSON escrito a mano.
    return max(1, general)


def toca_ahora(equipo, ultima_iso, ahora: datetime, intervalo_global: int) -> bool:
    """Le toca ya a este equipo?

    Le toca si nunca se le consulto o si desde la ultima vez ha pasado su
    intervalo efectivo. Justo en el limite SI le toca: al esperar el tic
    siguiente se acumularia un retraso en cada vuelta.

    Dos casos raros se resuelven consultando (True) en vez de bloqueando:

      - marca ilegible: si un JSON corrupto pudiera saltarse un equipo, ese
        equipo se quedaria sin respaldo para siempre y en silencio.
      - marca en el futuro (el reloj del servidor se corrigio hacia atras, o
        alguien la escribio a mano): esperar a que el futuro llegue dejaria al
        equipo parado horas o dias. Se le consulta y la marca queda arreglada.
    """
    if ahora.tzinfo is None:
        ahora = ahora.replace(tzinfo=timezone.utc)

    momento = _momento(ultima_iso)
    if momento is None:
        return True

    transcurrido = (ahora - momento).total_seconds()
    if transcurrido < 0:
        return True
    return transcurrido >= intervalo_efectivo(equipo, intervalo_global) * 60


def equipos_pendientes(
    equipos, ultimas: dict, ahora: datetime, intervalo_global: int
) -> list:
    """Los equipos del inventario a los que les toca ahora, en su mismo orden."""
    ultimas = ultimas or {}
    return [
        e for e in equipos
        if toca_ahora(e, ultimas.get(e.nombre), ahora, intervalo_global)
    ]


def limpiar_ultimas(ultimas: dict, equipos) -> dict:
    """Quita de `ultimas` los equipos que ya no estan en el inventario.

    Sin esto el archivo crece para siempre: cada equipo dado de baja deja su
    marca ahi, y en una flota que rota clientes acabaria pesando mas que el
    propio inventario. Devuelve un diccionario nuevo en vez de mutar el que se
    le pasa, para que siga siendo facil de probar.
    """
    vivos = {e.nombre for e in equipos}
    return {k: v for k, v in (ultimas or {}).items() if k in vivos}


def segundos_hasta_el_proximo(
    equipos, ultimas: dict, ahora: datetime, intervalo_global: int
) -> float:
    """Cuanto falta para que le toque al PRIMER equipo de la flota.

    0.0 si ya le toca a alguno, infinito si no hay equipos. Es lo que fija el
    ritmo del bucle: dormir hasta el primer vencimiento y no hasta el intervalo
    global, que es lo que dejaba fuera de plazo a los equipos mas frecuentes.
    """
    if ahora.tzinfo is None:
        ahora = ahora.replace(tzinfo=timezone.utc)
    ultimas = ultimas or {}

    plazos: list[float] = []
    for equipo in equipos:
        momento = _momento(ultimas.get(equipo.nombre))
        if momento is None:
            return 0.0  # nunca consultado (o marca corrupta): le toca ya
        transcurrido = (ahora - momento).total_seconds()
        if transcurrido < 0:
            return 0.0  # marca de futuro: mismo criterio que toca_ahora
        plazos.append(intervalo_efectivo(equipo, intervalo_global) * 60 - transcurrido)

    if not plazos:
        return float("inf")
    return max(0.0, min(plazos))


# --- Estado publicado -------------------------------------------------------


def _escribir_estado(ruta: Path, datos: dict) -> None:
    """Vuelca el estado del programador de forma atomica.

    Mismo enfoque que estado.py (temporal en la misma carpeta + os.replace):
    el panel lee este archivo mientras el programador lo escribe y no debe
    encontrarse nunca un JSON a medias. Nunca lanza: un panel sin datos es un
    problema menor que un programador que se cae y deja de respaldar.
    """
    temporal = ""
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=ruta.parent,
            prefix=ruta.name + ".",
            suffix=".tmp",
            delete=False,
        ) as fh:
            json.dump(datos, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
            temporal = fh.name
        os.replace(temporal, ruta)
    except OSError as exc:
        log.warning("No se pudo escribir %s: %s", ruta, exc)
        if temporal:
            try:
                os.unlink(temporal)
            except OSError:
                pass


def estado_programador(cfg) -> dict:
    """Lo que sabe el programador de si mismo, para el panel.

    NUNCA lanza: lo llama un servidor web mientras se pinta una pagina. Si el
    archivo no existe (el programador todavia no arranco) o esta corrupto, se
    devuelve el mismo diccionario con valores por defecto, para que el panel
    pueda pintar "sin datos" sin comprobar claves una a una.
    """
    # Copia por valor de los diccionarios: si se devolviera el mismo objeto que
    # hay en ESTADO_VACIO, quien lo modificara (el bucle guarda ahi `ultimas`)
    # cambiaria el defecto para todo el proceso.
    datos = {c: (dict(v) if isinstance(v, dict) else v) for c, v in ESTADO_VACIO.items()}
    try:
        crudo = json.loads(ruta_programador(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError, TypeError):
        return datos
    if not isinstance(crudo, dict):
        return datos

    for clave, defecto in ESTADO_VACIO.items():
        valor = crudo.get(clave, defecto)
        # El tipo manda: este archivo se puede editar a mano y el panel hace
        # cuentas con proxima/intervalo. Mejor "sin datos" que una excepcion
        # en mitad de una plantilla.
        if isinstance(defecto, bool):
            datos[clave] = bool(valor)
        elif isinstance(defecto, int) and not isinstance(defecto, bool):
            try:
                datos[clave] = int(valor)
            except (TypeError, ValueError):
                datos[clave] = defecto
        elif isinstance(defecto, dict):
            # `ultimas` lo recorre el panel para pintar una columna: se filtra a
            # pares texto->texto para que una lista o un numero metidos a mano
            # no revienten la plantilla a mitad de pagina.
            datos[clave] = (
                {str(k): str(v) for k, v in valor.items()}
                if isinstance(valor, dict)
                else dict(defecto)
            )
        else:
            datos[clave] = valor
    # ultimo_codigo es int o None y no tiene defecto entero: se normaliza aparte.
    codigo = crudo.get("ultimo_codigo")
    try:
        datos["ultimo_codigo"] = None if codigo is None else int(codigo)
    except (TypeError, ValueError):
        datos["ultimo_codigo"] = None
    return datos


# --- Senales ----------------------------------------------------------------


def _instalar_senales(parar: threading.Event) -> None:
    """SIGTERM (systemd stop) y SIGINT (Ctrl+C) piden parada ordenada.

    Solo se marca el evento: si la senal llega a mitad de un ciclo, ese ciclo
    termina. Cortar un respaldo por la mitad deja a medias el commit de git y
    el estado diciendo "en curso" para siempre; esperar unos minutos es mas
    barato que limpiar eso.

    En Windows no existe SIGTERM: se consulta antes de registrar en vez de
    asumirlo, para que el programador se pueda probar en el portatil de quien
    lo desarrolla.
    """
    def manejar(numero, _marco):  # noqa: ANN001
        log.info(
            "Senal %s recibida: se para al terminar el ciclo en curso.", numero
        )
        parar.set()

    for nombre in ("SIGTERM", "SIGINT"):
        numero = getattr(signal, nombre, None)
        if numero is None:
            continue
        try:
            signal.signal(numero, manejar)
        except (ValueError, OSError):
            # signal.signal solo funciona en el hilo principal. Si alguien
            # arranca el programador en un hilo, se pierde la parada ordenada
            # pero no hay por que impedirle correr.
            log.debug("No se pudo registrar %s (no es el hilo principal)", nombre)


# --- Bucle ------------------------------------------------------------------


def _recargar(cfg):
    """Relee la configuracion desde su archivo; si falla, devuelve la actual.

    Releer en cada vuelta es lo que hace que un cambio de intervalo, un equipo
    nuevo en el inventario o un ajuste del panel entren sin reiniciar el
    servicio. Y tolerar el fallo es lo que evita que un YAML a medio guardar
    deje la flota sin respaldos hasta que alguien mire el log.
    """
    ruta = getattr(cfg, "ruta", None)
    if ruta is None:
        # Configuracion construida en memoria (pruebas): no hay de donde
        # releer, pero si ajustes del panel que aplicar.
        try:
            cfg.aplicar_ajustes()
        except Exception:  # noqa: BLE001
            pass
        return cfg

    # Import local: config no depende de este modulo, pero mantenerlo aqui
    # deja claro que la recarga es cosa del bucle y no de la importacion.
    from .config import Config

    try:
        return Config.desde_archivo(ruta)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "No se pudo releer %s (%s): se sigue con la configuracion anterior",
            ruta, exc,
        )
        return cfg


def _inventario(cfg) -> list:
    """Equipos del inventario, o lista vacia si no se pudo leer.

    Tolerante por lo mismo que _recargar: un CSV a medio guardar (el panel lo
    reescribe en cada alta) no puede matar al programador. Los avisos del
    inventario no se miran aqui: los publica ejecutar() en el estado, que es
    donde el panel los busca, y repetirlos cada tic llenaria el log.
    """
    # Import local: el planificador solo necesita el inventario para decidir a
    # quien le toca, y asi este modulo se puede importar sin arrastrar csv.
    from .inventory import cargar

    try:
        equipos, _avisos = cargar(cfg.inventario, cfg.ssh.puerto_defecto)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "No se pudo leer el inventario (%s): en esta vuelta no se consulta "
            "a nadie, se reintenta en la siguiente",
            exc,
        )
        return []
    return equipos


def _un_ciclo(cfg, equipos) -> int:
    """Respalda los equipos indicados y devuelve el codigo de salida del ciclo."""
    # Import perezoso para romper el ciclo cli -> planificador -> cli. El
    # sentido "bueno" de la dependencia es cli -> planificador (cli es el
    # punto de entrada), asi que es este modulo el que importa tarde, dentro
    # de la funcion y no al cargarse. Ademas cuesta lo mismo: en la segunda
    # vuelta el modulo ya esta en sys.modules.
    from .cli import ejecutar

    try:
        return ejecutar(cfg, seleccion=equipos)
    except Exception:  # noqa: BLE001
        # Un ciclo que revienta no puede matar al programador: dentro de 4
        # horas el equipo puede estar arreglado y hay que volver a intentarlo.
        log.exception("El ciclo de respaldo fallo de forma inesperada")
        return 1


def planificar(cfg) -> int:
    """Bucle residente: en cada tic respalda a los equipos que les toca.

    Solo vuelve cuando llega una senal de parada. Devuelve 0: el codigo de
    cada ciclo se publica en programador.json, pero un ciclo con equipos
    caidos no es motivo para que systemd reinicie el servicio.

    Sobre el ritmo del bucle: en vez de dormir el intervalo global entero se
    duerme hasta el PRIMER vencimiento de la flota (segundos_hasta_el_proximo),
    con un suelo de TIC_MINIMO. Se eligio asi, y no un tic fijo corto, porque
    con un tic fijo hay que escoger entre despertar cada pocos segundos a no
    hacer nada (300 dias al ano de trabajo inutil) o perder precision con los
    equipos mas frecuentes. Calcular el vencimiento cuesta un recorrido de la
    lista y da la hora exacta, que ademas es la que el panel pinta como
    "proximo ciclo". El suelo evita que un equipo con intervalo 1 (o una marca
    corrupta) convierta el bucle en una rueda.
    """
    parar = threading.Event()
    _instalar_senales(parar)

    ciclos = 0
    ultima: str | None = None
    ultimo_codigo: int | None = None
    ultimo_lote = 0
    duracion = 0.0
    primera = True
    # Un ciclo pedido desde el panel. Se limpia al arrancar por si el servicio
    # murio con una peticion sin atender: al volver, lo primero que hace es la
    # vuelta con al_arrancar, que ya respalda todo, y arrastrar la peticion
    # encadenaria un segundo ciclo identico detras.
    a_mano = False
    try:
        ruta_peticion(cfg).unlink()
        log.info("Habia un respaldo pedido sin atender: lo cubre el arranque.")
    except OSError:
        pass

    # Se arranca con lo que dejo la ejecucion anterior: sin esto, cada reinicio
    # del servicio (o cada despliegue) volveria a respaldar la flota entera,
    # que es justo lo que se quiere evitar con los intervalos por equipo.
    ultimas: dict[str, str] = dict(estado_programador(cfg).get("ultimas", {}))

    def publicar(proxima: str | None, corriendo: bool) -> None:
        _escribir_estado(
            ruta_programador(cfg),
            {
                "proxima": proxima,
                "ultima": ultima,
                "intervalo_minutos": int(cfg.planificador.intervalo_minutos),
                "corriendo": corriendo,
                "ciclos": ciclos,
                "ultimo_codigo": ultimo_codigo,
                "ultimas": dict(ultimas),
                "ultimo_lote": ultimo_lote,
            },
        )

    log.info(
        "Programador en marcha: intervalo por defecto %d min (cada equipo puede "
        "traer el suyo en el inventario)%s. Se mira si hay que parar o si cambio "
        "el intervalo cada %ds.",
        cfg.planificador.intervalo_minutos,
        ", empezando ya" if cfg.planificador.al_arrancar else "",
        int(PASO_ESPERA),
    )

    while not parar.is_set():
        cfg = _recargar(cfg)
        equipos = _inventario(cfg)
        intervalo_global = cfg.planificador.intervalo_minutos

        if equipos:
            # Los equipos dados de baja no se arrastran para siempre. Solo con
            # el inventario legible: si la lectura fallo, `equipos` viene vacio
            # y limpiar aqui borraria la memoria de la flota entera, con lo que
            # el proximo ciclo bueno respaldaria los 300 equipos de golpe.
            ultimas = limpiar_ultimas(ultimas, equipos)

        # Primera vuelta con al_arrancar: se respalda ya, toda la flota. Tras un
        # reinicio del servidor esto es lo que evita que una maquina que se
        # reinicia cada noche no respalde nunca.
        if primera and cfg.planificador.al_arrancar:
            pendientes = list(equipos)
        else:
            pendientes = equipos_pendientes(
                equipos, ultimas, datetime.now(timezone.utc), intervalo_global
            )
        # Si el inventario no se pudo leer, esta no cuenta como primera vuelta:
        # el arranque inmediato sigue pendiente para cuando el CSV se arregle.
        if equipos:
            primera = False

        if pendientes:
            ciclos += 1
            ultimo_lote = len(pendientes)
            publicar(proxima=None, corriendo=True)

            log.info(
                "Ciclo %d: le toca a %d de %d equipos",
                ciclos, len(pendientes), len(equipos),
            )
            inicio = time.monotonic()
            ultimo_codigo = _un_ciclo(cfg, pendientes)
            duracion = time.monotonic() - inicio
            ultima = _ahora()

            # Se marca a TODOS los consultados, les fuera bien o mal. Un equipo
            # caido reintentado en cada tic no se arreglaria antes y si llenaria
            # el log, gastaria conexiones y acercaria al equipo a un bloqueo por
            # intentos fallidos: esperar su intervalo es mejor politica que
            # insistir. Los reintentos dentro del ciclo ya los hace ejecutar().
            for equipo in pendientes:
                ultimas[equipo.nombre] = ultima

            log.info(
                "Ciclo %d terminado en %.0fs (codigo %s)",
                ciclos, duracion, ultimo_codigo,
            )

            # Que un ciclo dure mas que el intervalo mas corto de los equipos
            # que entraron en el significa que ese equipo nunca se consultara a
            # su ritmo. No se puede arreglar solo (git no admite dos ciclos a la
            # vez), pero tiene que quedar dicho en el log.
            mas_corto = min(intervalo_efectivo(e, intervalo_global) for e in pendientes)
            if duracion >= mas_corto * 60:
                log.warning(
                    "El ciclo tardo %.0f min y el intervalo mas corto de la "
                    "tanda son %d min: no da tiempo. El siguiente arranca en "
                    "%.0fs; sube el intervalo de ese equipo o la concurrencia.",
                    duracion / 60, mas_corto, TIC_MINIMO,
                )
        else:
            # Ni ciclo, ni estado de "0 equipos", ni git: solo una linea de
            # debug para quien este mirando por que no pasa nada.
            duracion = 0.0
            log.debug(
                "No le toca a ninguno de los %d equipos del inventario", len(equipos)
            )

        # Instante de referencia comun para la espera y para sus recalculos
        # (ver _espera_tic): los dos tienen que medir desde el mismo sitio.
        # Un ciclo pedido a mano toca a TODOS. Quien le da al boton quiere ver
        # su flota respaldada ahora, no "a los tres que les tocaba".
        if a_mano:
            pendientes = list(equipos)
            a_mano = False

        arranque = datetime.now(timezone.utc)
        # `primera` sigue en True mientras no haya entrado ni un equipo: es
        # justo la instalacion recien hecha, y ahi el inventario vacio se
        # vuelve a mirar en segundos y no dentro de un intervalo entero.
        espera = _espera_tic(
            equipos, ultimas, intervalo_global, duracion, arranque, primera
        )
        if espera > 0:
            motivo = _esperar(
                cfg,
                parar,
                espera,
                publicar,
                lambda: _espera_tic(
                    equipos,
                    ultimas,
                    cfg.planificador.intervalo_minutos,
                    duracion,
                    arranque,
                    primera,
                ),
            )
            if motivo == "parar":
                break
            a_mano = motivo == "ahora"
        else:
            # Sin espera tampoco se pierde una peticion: puede haber entrado
            # mientras corria el ciclo anterior.
            a_mano = recoger_peticion(cfg) is not None

    log.info("Programador parado tras %d ciclo(s).", ciclos)
    publicar(proxima=None, corriendo=False)
    return 0


def _espera_tic(
    equipos,
    ultimas: dict,
    intervalo_global: int,
    duracion: float,
    ahora: datetime | None = None,
    sin_estrenar: bool = False,
) -> float:
    """Segundos hasta la proxima comprobacion de vencimientos.

    `ahora` se recibe (y no se mira el reloj) para que la espera se pueda
    recalcular a mitad, cuando el panel cambia el intervalo, SIN que el
    resultado se mueva solo por haber pasado el tiempo: quien la compara con la
    espera en curso necesita las dos medidas desde el mismo instante.

    Sin inventario (vacio o ilegible) no hay vencimientos que mirar y se cae al
    intervalo global; asi un CSV roto no deja al programador despertando cada
    minuto para nada.

    `sin_estrenar` es la excepcion, y es la que hace util a una instalacion
    nueva: mientras no se haya completado ni un ciclo, un inventario vacio se
    vuelve a mirar en ESPERA_SIN_ESTRENAR segundos en lugar de dentro de cuatro
    horas. Es una condicion que solo se da al principio, asi que no cambia el
    ritmo de un sistema en marcha.
    """
    if not equipos:
        if sin_estrenar:
            return ESPERA_SIN_ESTRENAR
        return espera_hasta_el_turno(duracion, intervalo_global)

    faltan = segundos_hasta_el_proximo(
        equipos, ultimas, ahora or datetime.now(timezone.utc), intervalo_global
    )
    if faltan == float("inf"):
        return espera_hasta_el_turno(duracion, intervalo_global)
    return max(TIC_MINIMO, faltan)


def _en(segundos: float) -> str:
    """Marca ISO de dentro de `segundos`, para que el panel cuente hacia atras."""
    return (datetime.now(timezone.utc) + timedelta(seconds=segundos)).isoformat()


def _esperar(cfg, parar: threading.Event, espera: float, publicar, recalcular) -> str:
    """Espera hasta el proximo tic. Dice POR QUE se desperto.

    Devuelve "parar" si hay que terminar, "ahora" si alguien pidio un ciclo
    desde el panel, y "" si simplemente se cumplio el plazo. Antes devolvia un
    si/no; hace falta el tercer caso porque un ciclo pedido a mano respalda la
    flota ENTERA, y no solo a los equipos que les tocaba.

    La espera se parte en trozos de PASO_ESPERA en vez de un unico sleep
    largo por dos motivos: un SIGTERM se atiende en segundos (systemd no se
    queda esperando el timeout para matar el proceso) y, entre trozo y trozo,
    se releen los ajustes del panel. Asi bajar el intervalo de 4 horas a 30
    minutos surte efecto en el momento, y no dentro de 4 horas, que es
    justo cuando alguien esta esperando a ver si funciono.

    `recalcular` devuelve lo que deberia durar la espera con los ajustes de
    ahora mismo. Se recibe como funcion y no como numero porque quien decide
    eso (el vencimiento del primer equipo) necesita el inventario y las marcas,
    que son cosa del bucle y no de esta espera.
    """
    publicar(proxima=_en(espera), corriendo=False)
    log.debug("Esperando %.0fs hasta el proximo ciclo", espera)

    inicio = time.monotonic()
    while True:
        restante = espera - (time.monotonic() - inicio)
        if restante <= 0:
            return ""
        if parar.wait(min(PASO_ESPERA, restante)):
            return "parar"

        # Antes que nada: si alguien le dio al boton, se deja de esperar. Va
        # primero para que un ciclo pedido a mano no tenga que aguantar a que
        # se relean los ajustes.
        quien = recoger_peticion(cfg)
        if quien is not None:
            log.info("Respaldo pedido desde el panel%s: se arranca ya",
                     f" por {quien}" if quien else "")
            return "ahora"

        # Solo los ajustes del panel: es un JSON diminuto y no lanza (ver
        # Config.aplicar_ajustes). El YAML completo se relee al empezar el
        # ciclo, que es donde un error se puede reportar y absorber.
        try:
            cfg.aplicar_ajustes()
        except Exception:  # noqa: BLE001
            continue

        try:
            nueva = recalcular()
        except Exception:  # noqa: BLE001
            continue
        # Un segundo de margen: sin el, un ajuste identico reescribiria el
        # archivo de estado cada 5 segundos sin aportar nada.
        if abs(nueva - espera) >= 1:
            transcurrido = time.monotonic() - inicio
            log.info(
                "El intervalo cambio a %d min mientras se esperaba: el proximo "
                "ciclo pasa a estar a %.0f min",
                cfg.planificador.intervalo_minutos,
                max(0.0, nueva - transcurrido) / 60,
            )
            espera = nueva
            publicar(proxima=_en(max(0.0, nueva - transcurrido)), corriendo=False)
