"""Punto de entrada: orquesta el respaldo de toda la flota.

Estructura de la ejecucion:

  1. Los equipos se consultan EN PARALELO (SSH es lo lento: red y espera).
  2. Los resultados se guardan EN SERIE (git no admite dos escrituras a la vez).

Esa separacion es lo que permite subir la concurrencia sin corromper el indice
de git.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__, device, identidades, inventory
from .config import Config, ErrorConfig
from .device import ErrorEquipo, Resultado, TipoError, respaldar
from .estado import CAMBIO, FALLO, SIN_CAMBIOS, Estado
from .hechos import Hechos
from .inventory import Equipo, ErrorInventario, cargar
from .notify import Notificador
from .sesion import hashear
from .store import Almacen, ErrorAlmacen

log = logging.getLogger("mkbackup")


def _configurar_log(verboso: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # paramiko es muy ruidoso en INFO
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    # Y transport, ademas de ruidoso, es un agujero. Cuando su hilo de fondo se
    # encuentra una excepcion la vuelca ENTERA -mensaje y traceback, una linea
    # de log por linea de pila- desde dentro de paramiko, sin pasar por
    # _censurar. Todo device.py esta escrito sobre la premisa de que nada llega
    # a un log sin pasar por el censor; esto la rompia por detras, y justo en el
    # camino que mas cerca esta de la contrasena (el saludo y la autenticacion).
    #
    # Ademas no aporta: cada fallo que vuelca aqui ya sale por nuestro log
    # clasificado, con el nombre del equipo y en una linea. Con la flota entera
    # caida (un enlace de subida que se va) esto multiplicaba el journal por
    # tres tracebacks * 300 equipos y enterraba las lineas que si se leen.
    #
    # Con -v se deja hablar: ahi hay alguien mirando a proposito. Aun asi NUNCA
    # a DEBUG (ver la cabecera de device.py: vuelca los paquetes en crudo).
    logging.getLogger("paramiko.transport").setLevel(
        logging.ERROR if verboso else logging.CRITICAL
    )


MINIMO_CLAVE = 8


def _pedir_clave(titulo: str = "Clave para el panel: ") -> str:
    """Pide una clave dos veces y la devuelve, o "" si no sirve.

    Cadena vacia en vez de excepcion: el motivo ya queda en el log y quien
    llama solo necesita saber que no siga. Lo usan todos los flags que piden
    una clave por terminal, para que las reglas (que coincida, que tenga un
    minimo) sean las mismas se entre por donde se entre.
    """
    try:
        clave = getpass.getpass(titulo)
        repetida = getpass.getpass("Repitela: ")
    except (EOFError, KeyboardInterrupt):
        # getpass lanza EOFError si no hay terminal (por ejemplo en un pipe):
        # mejor decirlo que hashear una cadena vacia sin que nadie se entere.
        print()
        log.error("Hace falta un terminal para escribir la clave sin que se vea.")
        return ""

    if clave != repetida:
        log.error("Las dos claves no coinciden.")
        return ""
    if len(clave) < MINIMO_CLAVE:
        log.error("Usa al menos %d caracteres.", MINIMO_CLAVE)
        return ""
    return clave


def _hash_clave() -> int:
    """Pide la clave del panel dos veces e imprime la linea para el YAML."""
    clave = _pedir_clave()
    if not clave:
        return 1

    print("\nPega esto en la seccion web: de tu config.yaml\n")
    print(f"  clave_hash: {hashear(clave)}\n")
    return 0


# --- Cuentas del panel desde el terminal ------------------------------------
# Las cuentas se administran desde el panel, que es donde se ven los roles y
# quien entro cuando. Estos tres flags existen para los dos momentos en los que
# el panel no sirve: el primer arranque (todavia no hay ninguna cuenta con la
# que entrar) y el rescate (el unico admin olvido su clave). Por eso van
# despues de cargar la configuracion: la ruta del archivo de cuentas sale de
# almacen.usuarios.


def _fecha_corta(marca: str) -> str:
    """La marca ISO del archivo de cuentas, recortada a lo que se lee.

    Se recorta en vez de reformatear con datetime: aqui solo se esta pintando
    una tabla, y un valor raro (o vacio) tiene que salir tal cual y no tumbar
    el listado con una excepcion de parseo.
    """
    if not marca:
        return "nunca"
    return marca[:19].replace("T", " ")


def _listar_usuarios(cfg: Config) -> int:
    """Imprime las cuentas del panel: nombre, rol y ultimo acceso."""
    # Perezoso como --web o --planificador: quien solo respalda no tiene por
    # que cargar el modulo de cuentas.
    from .usuarios import ErrorUsuarios, Usuarios

    try:
        cuentas = Usuarios(cfg.almacen.usuarios).listar()
    except ErrorUsuarios as exc:
        log.error("Usuarios: %s", exc)
        return 2

    if not cuentas:
        print(f"No hay ninguna cuenta en {cfg.almacen.usuarios}.")
        print("Crea la primera con:  mkbackup --crear-usuario NOMBRE --rol admin")
        return 0

    ancho_nombre = max([len("NOMBRE")] + [len(c.nombre) for c in cuentas])
    ancho_rol = max([len("ROL")] + [len(c.rol) for c in cuentas])
    print(f"{'NOMBRE':<{ancho_nombre}}  {'ROL':<{ancho_rol}}  ULTIMO ACCESO (UTC)")
    for c in cuentas:
        print(f"{c.nombre:<{ancho_nombre}}  {c.rol:<{ancho_rol}}  "
              f"{_fecha_corta(c.ultimo_acceso)}")
    print(f"\n{len(cuentas)} cuenta(s) en {cfg.almacen.usuarios}")
    return 0


def _crear_usuario(cfg: Config, nombre: str, rol: str) -> int:
    """Da de alta una cuenta pidiendo la clave por terminal."""
    from .usuarios import ROLES, ErrorUsuarios, Usuarios

    if rol not in ROLES:
        log.error("Rol '%s' desconocido. Usa uno de: %s", rol, ", ".join(ROLES))
        return 2

    clave = _pedir_clave(f"Clave para '{nombre}': ")
    if not clave:
        return 1

    try:
        usuarios = Usuarios(cfg.almacen.usuarios)
        errores = usuarios.crear(nombre, clave, rol)
    except ErrorUsuarios as exc:
        log.error("Usuarios: %s", exc)
        return 2

    if errores:
        for error in errores:
            log.error("%s", error)
        return 1

    print(f"\nCuenta '{nombre}' creada con rol {rol} en {cfg.almacen.usuarios}.")
    print("A partir de aqui, administra las cuentas desde el panel.\n")
    return 0


def _clave_usuario(cfg: Config, nombre: str) -> int:
    """Cambia la clave de una cuenta que ya existe (el rescate)."""
    from .usuarios import ErrorUsuarios, Usuarios

    try:
        usuarios = Usuarios(cfg.almacen.usuarios)
        # Se comprueba antes de pedir la clave: escribirla dos veces para que
        # luego resulte que el nombre estaba mal es tiempo tirado.
        if usuarios.obtener(nombre) is None:
            log.error(
                "No hay ninguna cuenta llamada '%s' en %s "
                "(mirala con --listar-usuarios)",
                nombre, cfg.almacen.usuarios,
            )
            return 1
    except ErrorUsuarios as exc:
        log.error("Usuarios: %s", exc)
        return 2

    clave = _pedir_clave(f"Nueva clave para '{nombre}': ")
    if not clave:
        return 1

    try:
        errores = usuarios.cambiar_clave(nombre, clave)
    except ErrorUsuarios as exc:
        log.error("Usuarios: %s", exc)
        return 2

    if errores:
        for error in errores:
            log.error("%s", error)
        return 1

    print(f"\nClave de '{nombre}' cambiada.")
    # Las sesiones abiertas viven en la memoria del proceso del panel, que es
    # otro: desde aqui no se pueden cerrar. Si la clave se cambio porque se
    # sospecha que alguien la sabia, hay que reiniciar el panel.
    print("Si el panel esta levantado, reinicialo para cerrar las sesiones "
          "abiertas con la clave anterior.\n")
    return 0


def _respaldar_con_reintentos(
    equipo: Equipo, cfg: Config, estado: Estado
) -> tuple[Equipo, Resultado | None, ErrorEquipo | None]:
    ultimo: ErrorEquipo | None = None

    for intento in range(1, cfg.reintentos + 1):
        try:
            # Se marca aqui, no al encolar: los equipos se encolan todos a la
            # vez pero solo corren <concurrencia> a la vez. Marcarlos al
            # encolar pintaria los 300 "en curso" desde el primer segundo.
            estado.equipo_inicia(equipo.nombre, intento)
            return equipo, respaldar(equipo, cfg), None
        except ErrorEquipo as exc:
            fallo = exc
        except Exception as exc:  # noqa: BLE001
            # Un equipo raro no debe tumbar la ejecucion de los otros 299.
            #
            # El traceback solo en el primer intento: si lo que falla es un bug
            # nuestro, fallara los tres, y volcar la misma pila tres veces por
            # equipo llena el log de copias sin anadir un dato.
            if intento == 1:
                log.exception("%s: error inesperado", equipo)
            fallo = ErrorEquipo(
                TipoError.DESCONOCIDO, f"{type(exc).__name__}: {exc}"
            )

        ultimo = fallo

        # LA UNICA excepcion a la regla de reintentar. Reintentar unas
        # credenciales rechazadas solo gasta tiempo y acerca el equipo a un
        # bloqueo por intentos fallidos: la respuesta no va a cambiar entre el
        # primer intento y el tercero, porque el que dice que no es el equipo y
        # lo dice a proposito.
        if fallo.tipo is TipoError.AUTENTICACION:
            log.warning("%s: %s (no se reintenta)", equipo, fallo.mensaje)
            break

        # Ojo con lo de arriba: DESCONOCIDO tambien se reintenta, y antes no.
        # El camino del 'except Exception' hacia un break, o sea que un fallo
        # que NO supimos clasificar era el unico, junto con el de credenciales,
        # que se rendia al primer golpe. Esta al reves: un error sin clasificar
        # es justo del que menos se sabe, y el mas comun de todos ellos es una
        # sesion que se corta a media conversacion (paramiko lanza EOFError
        # pelado), que es el caso en el que reintentar SI arregla las cosas.
        # Con un enlace WAN inestable, rendirse ahi es perder el respaldo de un
        # equipo que estaba perfectamente accesible dos segundos despues.
        if intento < cfg.reintentos:
            espera = cfg.espera_reintento * intento
            log.warning(
                "%s: %s (intento %d/%d, reintento en %ds)",
                equipo, fallo.mensaje, intento, cfg.reintentos, espera,
            )
            estado.equipo_reintenta(equipo.nombre, intento, fallo.mensaje)
            time.sleep(espera)
        else:
            log.error("%s: %s (agotados %d intentos)", equipo, fallo.mensaje, cfg.reintentos)

    return equipo, None, ultimo


# --- Renombrado de los equipos dados de alta sin nombre ---------------------
# Regla acordada con el panel: un equipo cuyo nombre es su propia IP es un alta
# a la que nadie supo ponerle nombre. RouterOS si sabe como se llama, asi que
# en cuanto uno de esos responde se le pone su identidad real. Es un extra que
# corre DESPUES de guardar el respaldo: si algo falla aqui, el respaldo ya esta
# a salvo y el equipo simplemente se renombrara en el proximo ciclo.


def _es_provisional(equipo: Equipo) -> bool:
    # La regla vive en identidades.py, que es donde la usa tambien el sondeo
    # masivo del panel. Aqui se deja el nombre para no tocar las llamadas.
    return identidades.es_provisional(equipo)


def _identidad_reportada(resultado: Resultado, equipo: Equipo, cfg: Config) -> str:
    """Nombre que reporta el propio RouterOS, saneado, o cadena vacia.

    Se mira primero el Resultado: si el respaldo ya trajo la identidad, volver
    a conectar solo para preguntarla seria un SSH de mas por equipo, y en una
    flota recien dada de alta serian cientos.
    """
    nombre = str(getattr(resultado, "identidad", "") or "").strip()
    if nombre:
        return nombre

    obtener = getattr(device, "identidad", None)
    if obtener is None:
        log.debug("No hay device.identidad: %s se queda con su IP", equipo.nombre)
        return ""
    return str(obtener(equipo, cfg) or "").strip()


def _renombrar_provisionales(
    cfg: Config,
    almacen: Almacen,
    pendientes: list[tuple[Equipo, Resultado]],
    inventario: list[Equipo],
) -> int:
    """Pone nombre real a los equipos que quedaron con su IP. Devuelve cuantos.

    Nunca lanza: el renombrado es cosmetico comparado con el respaldo.
    """
    renombrar = getattr(almacen, "renombrar", None)
    if renombrar is None:
        log.warning(
            "%d equipo(s) sin identificar, pero el almacen no sabe renombrar "
            "todavia: se quedan con su IP",
            len(pendientes),
        )
        return 0

    equipos = list(inventario)
    renombrados = 0

    for equipo, resultado in pendientes:
        try:
            identidad = _identidad_reportada(resultado, equipo, cfg)
            if not identidad or identidad == equipo.nombre:
                log.info(
                    "%s: el equipo no reporto una identidad usable, sigue con su IP",
                    equipo.nombre,
                )
                continue

            # Se valida con las mismas reglas que el alta desde el panel: la
            # identidad viene del equipo, no de una persona, y puede chocar con
            # otro nombre del inventario. Dos equipos con el mismo nombre
            # comparten archivo en el repo y se pisan los respaldos.
            #
            # Va por identidades.renombrado y no por validar_equipo a pelo
            # porque renombrar es cambiar UN campo, y aqui se estaban perdiendo
            # tres: al no pasar usuario, clave e intervalo, el equipo salia con
            # ellos en blanco. O sea que un router con credenciales propias, el
            # dia que se le renombraba solo, se quedaba sin ellas y el ciclo
            # siguiente fallaba autenticando. Un fallo que llega un ciclo tarde
            # y en otro sitio no se relaciona nunca con el renombrado.
            nuevo, errores = identidades.renombrado(equipo, identidad, equipos)
            if nuevo is None:
                log.warning(
                    "%s: no se renombra a '%s': %s",
                    equipo.nombre, identidad, "; ".join(errores),
                )
                continue

            # Primero el archivo (git mv + commit, conserva el historico) y
            # solo despues el CSV: si el orden fuera el inverso y el mv
            # fallara, el inventario apuntaria a un respaldo que no existe.
            if not renombrar(equipo.ruta_relativa, nuevo.ruta_relativa):
                log.warning(
                    "%s: no se pudo mover el respaldo a '%s', se deja como estaba",
                    equipo.nombre, nuevo.ruta_relativa,
                )
                continue

            equipos = [nuevo if e.nombre == equipo.nombre else e for e in equipos]
            # Se guarda equipo a equipo (la escritura es atomica) en vez de una
            # sola vez al final: si el siguiente renombrado falla, los ya
            # hechos quedan reflejados y no se repiten en el proximo ciclo.
            inventory.guardar(cfg.inventario, equipos)

            renombrados += 1
            log.info(
                "%s: identificado como '%s' (%s -> %s)",
                equipo.nombre, nuevo.nombre, equipo.ruta_relativa, nuevo.ruta_relativa,
            )
        except Exception:  # noqa: BLE001
            log.exception("%s: fallo el renombrado (el respaldo si se guardo)", equipo)

    return renombrados


# --- Lo que se sabe de cada equipo ------------------------------------------
# El panel necesita mostrar, por equipo, que modelo es, que version de RouterOS
# corre y cuando se respaldo bien por ultima vez. Nada de eso se puede sacar de
# donde estaba: el modelo y la version se leian durante el respaldo y se tiraban,
# y la fecha del ultimo respaldo bueno no esta en git (un equipo sin cambios no
# deja commit). Se anota aqui, que es el unico sitio que ve las tres cosas a la
# vez: el resultado del equipo, lo que hizo el almacen y si fallo.


def _anotar(hechos: Hechos, nombre: str, **campos) -> None:
    """Anota lo observado de un equipo. NUNCA lanza.

    Esto es un extra para el panel: un fallo escribiendo el archivo de hechos no
    puede tumbar un ciclo de respaldo ni cambiar su codigo de salida. Hechos ya
    se traga sus propios errores de escritura; este envoltorio esta por lo que no
    se pueda prever (un disco lleno, un permiso raro) y para que quede en el log.
    """
    try:
        hechos.anotar(nombre, **campos)
    except Exception:  # noqa: BLE001
        log.exception("%s: no se pudo anotar lo observado (el respaldo sigue)", nombre)


def ejecutar(
    cfg: Config,
    solo: str = "",
    sin_binario: bool = False,
    seleccion: list[Equipo] | None = None,
) -> int:
    """Respalda la flota (o el subconjunto que se pida) y devuelve el codigo.

    `solo` es el filtro de la linea de ordenes: un unico equipo por nombre.

    `seleccion` es el subconjunto que ya eligio el programador, que respalda
    solo a los equipos a los que les toca segun su intervalo. Se recibe la
    seleccion en vez de dejar que el planificador llame una vez por equipo
    porque un ciclo por equipo serian N repos abiertos, N estados y N commits.

    El inventario se lee SIEMPRE completo, tambien cuando viene una seleccion,
    por dos motivos que no son negociables:

      - los avisos del inventario (lineas descartadas, puertos raros) se
        publican en el estado y hay que darlos aunque el ciclo sea de 3 equipos;
      - el renombrado automatico reescribe el CSV ENTERO. Si se guardara solo
        el subconjunto, el resto de la flota desapareceria del inventario.

    La seleccion se usa solo para FILTRAR ese inventario, por nombre. Asi los
    equipos respaldados son siempre un subconjunto de lo que hay hoy en el CSV:
    uno dado de baja mientras el programador esperaba su turno no se respalda,
    y ningun equipo puede colarse en el guardado sin estar en el inventario.
    """
    inicio = time.monotonic()

    try:
        equipos, avisos = cargar(cfg.inventario, cfg.ssh.puerto_defecto)
    except ErrorInventario as exc:
        log.error("Inventario: %s", exc)
        return 2

    # Sin credenciales generales, solo se puede entrar a los equipos que traigan
    # las suyas. Si no hay ninguno asi, el ciclo entero seria 300 fallos de
    # autenticacion: se dice el motivo real UNA vez y no se toca ningun equipo.
    # La configuracion ya no aborta por esto (se puede arreglar desde el panel),
    # asi que el que tiene que negarse es el respaldo.
    hay_generales = bool(cfg.ssh.password or cfg.ssh.clave_privada)
    if not hay_generales and not any(e.usuario or e.clave for e in equipos):
        log.error(
            "No hay credenciales SSH: ni generales (ssh.password / "
            "ssh.clave_privada) ni propias de ningun equipo. Ponlas en el "
            "panel, en Ajustes, antes de respaldar."
        )
        return 2

    for aviso in avisos:
        log.warning("Inventario %s", aviso)

    # El inventario COMPLETO, antes de filtrar por --solo o por la seleccion:
    # reescribir el CSV tras un renombrado con la lista filtrada borraria a
    # todos los demas.
    inventario = list(equipos)

    if seleccion is not None:
        # Se aceptan Equipo o nombres sueltos: al que llama le da igual, y aqui
        # lo unico que importa es el nombre para cruzarlo con el inventario.
        nombres = {getattr(e, "nombre", e) for e in seleccion}
        equipos = [e for e in equipos if e.nombre in nombres]
        if not equipos:
            # Ni error ni ejecucion vacia: los equipos pedidos se dieron de baja
            # entre la decision y el ciclo. No hay nada que respaldar y tampoco
            # nada roto, asi que no se toca el estado ni git.
            log.warning(
                "Ninguno de los %d equipos pedidos sigue en el inventario: "
                "no hay nada que respaldar",
                len(nombres),
            )
            return 0

    if solo:
        equipos = [e for e in equipos if e.nombre == solo]
        if not equipos:
            log.error("No hay ningun equipo llamado '%s' en el inventario", solo)
            return 2

    if sin_binario:
        cfg.binario.activo = False

    # El estado se publica desde ya: asi el panel muestra la ejecucion en
    # marcha aunque el primer equipo tarde en contestar.
    estado = Estado(cfg.almacen.estado)
    with estado:
        estado.iniciar(
            equipos,
            concurrencia=cfg.concurrencia,
            binario_activo=cfg.binario.activo,
            avisos=avisos,
        )

        almacen = Almacen(cfg)
        try:
            almacen.inicializar()
        except ErrorAlmacen as exc:
            log.error("Almacen: %s", exc)
            estado.terminar(time.monotonic() - inicio, 3)
            return 3

        notificador = Notificador(cfg.telegram)
        hechos = Hechos(cfg.almacen.hechos)

        log.info(
            "Respaldando %d de los %d equipos del inventario (concurrencia %d, "
            "binario %s)",
            len(equipos), len(inventario), cfg.concurrencia,
            "si" if cfg.binario.activo else "no",
        )

        ok = 0
        cambios = 0
        fallidos: list[tuple[str, str]] = []
        # Equipos dados de alta sin nombre que hoy si contestaron: se les pone
        # su identidad real cuando termine la fase de guardado, nunca en medio
        # (git no admite dos escrituras sobre el mismo indice).
        provisionales: list[tuple[Equipo, Resultado]] = []

        # Fase 1: consultar equipos en paralelo.
        with ThreadPoolExecutor(max_workers=cfg.concurrencia) as pool:
            futuros = {
                pool.submit(_respaldar_con_reintentos, e, cfg, estado): e
                for e in equipos
            }

            # Fase 2: guardar en serie, segun van llegando.
            for futuro in as_completed(futuros):
                equipo, resultado, error = futuro.result()

                if error or resultado is None:
                    motivo = error.mensaje if error else "sin resultado"
                    tipo = error.tipo.value if error else "desconocido"
                    fallidos.append((equipo.nombre, f"{tipo}: {motivo}"))
                    estado.equipo_falla(equipo.nombre, tipo, motivo)
                    notificador.fallo_equipo(equipo.nombre, equipo.ip, tipo, motivo)
                    # Cadenas vacias, no None: al equipo no se le pudo preguntar
                    # nada, y Hechos conserva el modelo y la version que ya se
                    # sabian. Un router apagado no deja de ser un RB4011, y es
                    # justo cuando lleva fallando cuando hace falta saberlo.
                    _anotar(
                        hechos, equipo.nombre, modelo="", version="",
                        resultado=FALLO, commit="", ok=False,
                    )
                    continue

                ok += 1
                try:
                    guardado = almacen.guardar(resultado)
                except ErrorAlmacen as exc:
                    log.error("%s: no se pudo guardar: %s", equipo, exc)
                    fallidos.append((equipo.nombre, f"almacen: {exc}"))
                    estado.equipo_falla(equipo.nombre, "almacen", str(exc))
                    # El equipo SI contesto (el que fallo fue el almacen), asi
                    # que su modelo y su version se aprovechan aunque el ciclo
                    # cuente como fallo y ultimo_ok no avance.
                    _anotar(
                        hechos, equipo.nombre,
                        modelo=getattr(resultado, "modelo", "") or "",
                        version=resultado.version or "",
                        resultado=FALLO, commit="", ok=False,
                    )
                    continue

                if _es_provisional(equipo):
                    provisionales.append((equipo, resultado))

                _anotar(
                    hechos, equipo.nombre,
                    # getattr por lo mismo que en _identidad_reportada: aqui
                    # tambien llegan resultados de prueba que traen lo minimo.
                    modelo=getattr(resultado, "modelo", "") or "",
                    version=resultado.version or "",
                    resultado=CAMBIO if guardado.cambio else SIN_CAMBIOS,
                    # Solo hay commit cuando hubo cambio; en un ciclo sin cambios
                    # se pasa vacio y se conserva el del ultimo cambio real.
                    commit=guardado.commit if guardado.cambio else "",
                    ok=True,
                )

                if guardado.cambio:
                    cambios += 1
                    extra = " + binario" if guardado.binario_guardado else ""
                    log.info("%s: CAMBIO (%s)%s", equipo.nombre, guardado.commit, extra)
                    estado.equipo_cambio(
                        equipo.nombre, guardado.commit, guardado.binario_guardado
                    )
                    notificador.cambio_equipo(equipo.nombre, guardado.commit)
                else:
                    log.debug("%s: sin cambios", equipo.nombre)
                    estado.equipo_sin_cambios(equipo.nombre)

        renombrados = 0
        if provisionales:
            renombrados = _renombrar_provisionales(
                cfg, almacen, provisionales, inventario
            )

        # Se olvida lo de los equipos dados de baja. Con el inventario COMPLETO,
        # NUNCA con `equipos`: ese es solo el subconjunto que se respaldo hoy
        # (--solo, o los que le tocaban al planificador), y limpiar con el
        # borraria lo que se sabe del resto de la flota en cada ciclo. Es el
        # mismo cuidado que hay que tener con el renombrado automatico, que
        # reescribe el CSV entero y por eso tambien recibe `inventario`.
        #
        # Un equipo renombrado en esta misma vuelta sigue en `inventario` con su
        # nombre viejo, asi que su fila se conserva hoy y la limpia el proximo
        # ciclo, que ya lee el CSV nuevo. Se prefiere eso a releer el inventario
        # aqui: borrar de mas es perder historia, borrar tarde no cuesta nada.
        try:
            olvidados = hechos.limpiar([e.nombre for e in inventario])
            if olvidados:
                log.info(
                    "%d equipo(s) ya no estan en el inventario: se olvidan sus datos",
                    olvidados,
                )
        except Exception:  # noqa: BLE001
            log.exception("No se pudo limpiar el archivo de hechos (el ciclo sigue)")

        duracion = time.monotonic() - inicio

        # Los renombrados tambien son commits: si solo se replicara cuando hay
        # cambios de configuracion, el remoto se quedaria con los nombres viejos.
        if cfg.almacen.remoto and (cambios or renombrados):
            try:
                log.info("Replicacion: %s", almacen.replicar())
            except ErrorAlmacen as exc:
                log.error("Replicacion fallo: %s", exc)
                fallidos.append(("__replicacion__", str(exc)))

        log.info(
            "Terminado en %.0fs - %d/%d correctos, %d con cambios, %d fallidos",
            duracion, ok, len(equipos), cambios, len(fallidos),
        )

        notificador.resumen(len(equipos), ok, cambios, fallidos, duracion)

        codigo = 1 if fallidos else 0
        estado.terminar(duracion, codigo)
        return codigo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mkbackup",
        description=(
            "Respaldo de configuraciones MikroTik a git (texto) y disco (binario). "
            "Sin opciones hace un ciclo y termina; con --planificador se queda "
            "residente y los repite cada planificador.intervalo_minutos, que se "
            "cambia desde el panel."
        ),
    )
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="archivo de configuracion"
    )
    parser.add_argument(
        "--solo", default="", metavar="NOMBRE",
        help="respaldar un unico equipo del inventario (util para probar)",
    )
    parser.add_argument(
        "--sin-binario", action="store_true",
        help="omitir /system backup save, solo el export en texto",
    )
    parser.add_argument(
        "--probar-config", action="store_true",
        help="validar configuracion e inventario sin conectar a ningun equipo",
    )
    parser.add_argument(
        "--planificador", action="store_true",
        help="quedarse residente y respaldar cada intervalo (lo que arranca "
             "mkbackup.service; el intervalo se cambia desde el panel)",
    )
    parser.add_argument(
        "--web", action="store_true",
        help="levantar el panel de estado (no respalda: solo muestra el avance)",
    )
    parser.add_argument(
        "--puerto", type=int, default=0, metavar="N",
        help="con --web, puerto a usar en vez del de la configuracion",
    )
    parser.add_argument(
        "--hash-clave", action="store_true",
        help="generar el hash de la clave del panel para pegarlo en el YAML",
    )
    parser.add_argument(
        "--listar-usuarios", action="store_true",
        help="listar las cuentas del panel (nombre, rol y ultimo acceso)",
    )
    parser.add_argument(
        "--crear-usuario", default="", metavar="NOMBRE",
        help="crear una cuenta del panel pidiendo la clave por terminal; es "
             "para el primer arranque y para el rescate, lo normal es crear "
             "las cuentas desde el panel",
    )
    parser.add_argument(
        "--clave-usuario", default="", metavar="NOMBRE",
        help="cambiar la clave de una cuenta que ya existe (la salida cuando "
             "alguien se queda fuera del panel)",
    )
    parser.add_argument(
        "--rol", default="admin", metavar="ROL",
        help="rol para --crear-usuario: admin, operador o lector. Por defecto "
             "admin, porque la cuenta que se crea desde el terminal suele ser "
             "la primera o la de rescate",
    )
    parser.add_argument("-v", "--verboso", action="store_true")
    parser.add_argument("--version", action="version", version=f"mkbackup {__version__}")

    args = parser.parse_args(argv)
    _configurar_log(args.verboso)

    # Antes de cargar la configuracion: para poner la clave del panel hay que
    # poder generar el hash aunque el YAML todavia no este completo.
    if args.hash_clave:
        return _hash_clave()

    try:
        cfg = Config.desde_archivo(args.config)
    except ErrorConfig as exc:
        log.error("Configuracion: %s", exc)
        return 2

    # Las cuentas viven en el archivo que dice almacen.usuarios, asi que estos
    # tres van DESPUES de cargar la configuracion (al contrario que
    # --hash-clave, que solo necesita la clave que escribes).
    if args.listar_usuarios:
        return _listar_usuarios(cfg)

    if args.crear_usuario:
        return _crear_usuario(cfg, args.crear_usuario, args.rol)

    if args.clave_usuario:
        return _clave_usuario(cfg, args.clave_usuario)

    if args.web:
        # Importado aqui y no arriba: quien solo respalda no necesita cargar
        # el servidor HTTP, y viceversa.
        from .web import servir

        if args.puerto:
            cfg.web.puerto = args.puerto
            cfg.validar()
        return servir(cfg)

    if args.planificador:
        # Perezoso por lo mismo que --web: quien lanza un ciclo suelto no
        # necesita el bucle residente, y ademas planificador importa de vuelta
        # ejecutar() de este modulo (ver el comentario alli).
        from .planificador import planificar

        return planificar(cfg)

    if args.probar_config:
        try:
            equipos, avisos = cargar(cfg.inventario, cfg.ssh.puerto_defecto)
        except ErrorInventario as exc:
            log.error("Inventario: %s", exc)
            return 2
        for aviso in avisos:
            log.warning("Inventario %s", aviso)
        log.info("Configuracion valida. %d equipos en el inventario.", len(equipos))
        grupos: dict[str, int] = {}
        for e in equipos:
            grupos[e.grupo] = grupos.get(e.grupo, 0) + 1
        for grupo, n in sorted(grupos.items()):
            log.info("  grupo %-20s %d equipos", grupo, n)
        return 0

    return ejecutar(cfg, solo=args.solo, sin_binario=args.sin_binario)


if __name__ == "__main__":
    sys.exit(main())
