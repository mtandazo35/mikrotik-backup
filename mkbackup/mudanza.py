"""Llevarse el sistema entero a otro servidor: empaquetar y restaurar.

Esto NO es "otro respaldo mas". Los respaldos que hace este programa son de la
configuracion de los ROUTERS; lo de aqui es la copia del propio servidor: el
historico completo, el inventario, las cuentas del panel, los ajustes y el
registro de auditoria. Sin ello, cambiar de maquina significa volver a dar de
alta la flota a mano y empezar el historial de cero, que es exactamente lo que
este proyecto existe para no tener que hacer.

Que va dentro y por que va TODO
-------------------------------
La tentacion es dejar fuera lo que se puede reconstruir. No se hace: una copia
que hay que completar a mano no es una mudanza, es una lista de tareas. Y la
mitad de lo "reconstruible" no lo es en la practica -las claves de los routers
del inventario, quien tenia que permiso, cuando empezo a fallar un equipo-.

Por eso mismo el paquete es material sensible en estado puro: lleva las claves
SSH de la flota EN CLARO (asi las guarda el inventario, por decision del
despliegue), los hashes de las claves del panel y, si export.mostrar_secretos
esta puesto, las passwords PPPoE y las PSK de wifi de los clientes dentro del
historico. Un solo archivo con todo eso dentro es la pieza mas peligrosa que
produce este programa. De ahi tres decisiones:

  - Se crea con permisos 0600 y el temporal nace ya privado, sin ventana.
  - Tiene su propio permiso ('datos.mudanza'), separado de los demas de
    Ajustes: quien puede cambiar el intervalo de respaldo no tiene por que
    poder descargarse las credenciales de la empresa entera.
  - Cada descarga se anota en la auditoria. Es lo unico que convierte "alguien
    se llevo una copia" en un dato y no en una sospecha.

Se restaura desde los dos sitios, y el terminal no sobra
--------------------------------------------------------
Descargar se hace desde el panel, porque es donde esta quien lo necesita.
Restaurar se puede desde el panel tambien, pero el caso que de verdad importa
-la maquina nueva- solo lo cubre el terminal: alli todavia no hay ninguna
cuenta con la que entrar, porque las cuentas estan justo dentro del paquete que
se quiere restaurar. Por eso `mkbackup --restaurar-datos` no es un extra, es el
camino principal, y el panel escribe la orden exacta en pantalla.

Restaurar desde la web va en DOS peticiones -subir y confirmar- por dos razones
distintas. Una: se lee de que servidor viene el paquete y de cuando es antes de
que reemplace nada, porque equivocarse de archivo aqui sustituye la flota
entera. Y dos: el formulario de subida lleva un unico campo, asi que el cuerpo
se puede volcar a disco segun llega en vez de juntarlo en memoria; el historico
de un cliente grande son cientos de megas y este servidor no tiene ni swap.

Restaurar pisa ademas el archivo de usuarios por debajo de la sesion que lo
esta pidiendo, asi que al terminar se cierran todas las sesiones: los tokens
que quedaban en memoria pertenecen a cuentas que quiza ya no existen.

Formato
-------
Un .tar.gz con un manifiesto.json dentro y las piezas al lado, con nombres
LOGICOS y relativos ('usuarios.json', 'configs.git/...'). Nunca rutas
absolutas: al restaurar, cada pieza va a donde diga la configuracion de la
maquina de destino, no a donde estaba en la de origen. Eso permite mudarse
entre servidores con distinta distribucion de carpetas, y de paso quita de en
medio la familia entera de agujeros de "extraer un tar te escribe en /etc".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import __version__
from .planificador import ruta_programador
from .store import ruta_replica

log = logging.getLogger("mkbackup.mudanza")


class ErrorMudanza(Exception):
    """No se pudo empaquetar o restaurar."""


# Version del formato del paquete. Sube si cambia la ESTRUCTURA (nombres de
# las piezas, significado del manifiesto), no cada vez que se anade una pieza:
# una pieza que el destino no conoce se ignora, y una que falta ya se avisa.
FORMATO = 1

NOMBRE_MANIFIESTO = "manifiesto.json"

# Se lee y se escribe por trozos y no de golpe. El historico de una flota
# grande no cabe comodo en la memoria de un VPS pequeno, y este servidor en
# concreto no tiene ni swap.
TROZO = 256 * 1024

# Holgura sobre lo que el manifiesto dice que ocupa, al desempaquetar. Existe
# porque el tar redondea cada archivo a bloques y las carpetas tambien cuentan:
# ajustarlo al byte haria fallar paquetes buenos.
MARGEN = 16 * 1024 * 1024


@dataclass(frozen=True)
class Pieza:
    """Una cosa que se lleva. `clave` es estable; `nombre` es lo que va en el tar.

    Son dos campos y no uno por la imagen de fondo: su clave siempre es
    'fondo', pero el nombre depende de la extension que tuviera el archivo que
    subieron. Al restaurar hace falta la clave para saber QUE es, y el nombre
    para saber como se llamaba.
    """

    clave: str
    nombre: str
    ruta: Path
    carpeta: bool
    que: str
    # Sin ella la copia no sirve para levantar el sistema en otra maquina.
    esencial: bool = False


def piezas(cfg) -> list[Pieza]:
    """Todo lo que compone este servidor, exista o no todavia en el disco.

    Se devuelven tambien las que faltan: la pantalla tiene que poder decir
    "esto no esta" en vez de callarselo. Un paquete al que le falta el archivo
    de usuarios es un paquete que al restaurar deja el panel sin cuentas, y eso
    hay que verlo ANTES de descargarlo, no despues de mudarse.
    """
    fondo = (cfg.web.fondo_login or "").strip()
    lado = Path(cfg.almacen.ajustes).parent

    lista = [
        Pieza("config", "config.yaml", Path(cfg.ruta) if cfg.ruta else lado / "config.yaml",
              False, "La configuracion: rutas, credenciales SSH y Telegram", True),
        Pieza("inventario", "inventory.csv", Path(cfg.inventario), False,
              "El inventario: la flota entera con sus credenciales", True),
        Pieza("usuarios", "usuarios.json", Path(cfg.almacen.usuarios), False,
              "Las cuentas del panel, sus roles y su alcance", True),
        Pieza("ajustes", "ajustes.json", Path(cfg.almacen.ajustes), False,
              "Lo que se cambio desde el panel", False),
        Pieza("estado", "estado.json", Path(cfg.almacen.estado), False,
              "Como fue el ultimo ciclo", False),
        Pieza("hechos", "equipos.json", Path(cfg.almacen.hechos), False,
              "Lo ultimo que se supo de cada equipo: modelo y version", False),
        Pieza("programador", "programador.json", ruta_programador(cfg), False,
              "Cuando toca el proximo ciclo y cuando se vio cada equipo", False),
        Pieza("replica", "replica.json", ruta_replica(cfg), False,
              "Como fue la ultima subida al repositorio remoto", False),
        Pieza("auditoria", "auditoria.log", Path(cfg.almacen.auditoria), False,
              "Quien entro y quien toco que", False),
    ]

    if fondo:
        # Con su nombre real: puede ser .jpg, .png, .webp o .avif, y al
        # restaurar hay que dejarlo con la extension que ajustes.json espera.
        lista.append(Pieza("fondo", Path(fondo).name, Path(fondo), False,
                           "La imagen de la pantalla de entrada", False))

    lista.append(Pieza("git", "configs.git", Path(cfg.almacen.git), True,
                       "EL HISTORICO: todas las versiones de todos los equipos", True))
    lista.append(Pieza("binarios", "binarios", Path(cfg.almacen.binarios), True,
                       "Los .backup binarios de cada equipo", False))
    return lista


# --- Medir antes de empaquetar ----------------------------------------------

def _pesar(ruta: Path) -> tuple[int, int]:
    """(archivos, bytes) de un archivo o de un arbol. Nunca lanza."""
    try:
        if ruta.is_file():
            return 1, ruta.stat().st_size
        if not ruta.is_dir():
            return 0, 0
    except OSError:
        return 0, 0

    archivos = total = 0
    for actual, _, nombres in os.walk(ruta, onerror=lambda _: None):
        for nombre in nombres:
            try:
                sitio = Path(actual) / nombre
                # Los enlaces no se siguen ni al medir ni al empaquetar: si
                # alguien colgo uno dentro del repositorio, seguirlo puede
                # llevarse medio disco y ademas escribir fuera al restaurar.
                if sitio.is_symlink():
                    continue
                total += sitio.stat().st_size
                archivos += 1
            except OSError:
                continue
    return archivos, total


def resumen(cfg) -> dict:
    """Que se llevaria y cuanto pesa, para pintarlo antes de descargar.

    Es una estimacion del tamano EN CRUDO: el .tar.gz sale bastante mas
    pequeno porque el historico son textos de configuracion, que comprimen
    mucho. Se dice asi en la pantalla en vez de mentir con una cifra exacta que
    obligaria a empaquetarlo entero para saberla.
    """
    detalle = []
    total = archivos = 0
    faltan = []
    for pieza in piezas(cfg):
        n, bytes_ = _pesar(pieza.ruta)
        hay = n > 0 or pieza.ruta.exists()
        detalle.append({
            "clave": pieza.clave, "nombre": pieza.nombre, "que": pieza.que,
            "carpeta": pieza.carpeta, "archivos": n, "bytes": bytes_, "hay": hay,
        })
        total += bytes_
        archivos += n
        if not hay and pieza.esencial:
            faltan.append(pieza.nombre)
    return {"piezas": detalle, "bytes": total, "archivos": archivos,
            "faltan": faltan}


def nombre_sugerido(cfg=None) -> str:
    """mkbackup-<servidor>-<fecha>.tar.gz, y nada raro dentro del nombre."""
    momento = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    crudo = socket.gethostname() or "servidor"
    limpio = "".join(c if (c.isalnum() or c in "-_") else "-" for c in crudo)[:40]
    return f"mkbackup-{limpio.strip('-') or 'servidor'}-{momento}.tar.gz"


def _sha256(ruta: Path) -> str:
    resumen_ = hashlib.sha256()
    with ruta.open("rb") as fh:
        for trozo in iter(lambda: fh.read(TROZO), b""):
            resumen_.update(trozo)
    return resumen_.hexdigest()


def _version_del_codigo() -> str:
    """El commit del programa que hizo el paquete, si esto es un clon de git.

    Sirve para lo unico que importa al restaurar: saber si la maquina de
    destino corre una version mas VIEJA que la que produjo los datos. Es un
    dato informativo, no una comprobacion: si falla se deja vacio.
    """
    raiz = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(raiz), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --- Empaquetar --------------------------------------------------------------

def _sanear(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Quita del tar todo lo que describe a ESTA maquina y no a los datos.

    El usuario y el grupo del origen no le sirven de nada al destino y, en un
    paquete que va a viajar, son informacion sobre el servidor de mas. Los
    permisos se fuerzan tambien: dentro va material que no puede quedar legible
    para todo el mundo por haber tenido un umask distendido en el origen.
    """
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o700 if info.isdir() else 0o600
    return info


def empaquetar(cfg, destino) -> dict:
    """Escribe el .tar.gz en `destino` y devuelve el manifiesto.

    El archivo se crea privado desde el primer byte -no se crea y luego se
    arregla con chmod-, porque en esa ventana cabe entero el inventario con las
    claves de la flota.
    """
    destino = Path(destino)
    lista = piezas(cfg)

    # Un paquete que se guarda DENTRO de lo que esta empaquetando se persigue
    # la cola: el tar se lee a si mismo mientras crece. Se caza aqui porque el
    # sintoma -un archivo enorme y una maquina sin disco- no se parece en nada
    # a la causa.
    for pieza in lista:
        if pieza.carpeta and _dentro_de(destino, pieza.ruta):
            raise ErrorMudanza(
                f"no se puede guardar el paquete dentro de {pieza.ruta}: "
                "seria parte de si mismo. Elige otra carpeta."
            )

    anotadas = []
    destino.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as bruto:
            with tarfile.open(fileobj=bruto, mode="w:gz") as tar:
                for pieza in lista:
                    anotada = _meter(tar, pieza)
                    if anotada:
                        anotadas.append(anotada)

                manifiesto = {
                    "formato": FORMATO,
                    "programa": "mkbackup",
                    "version": __version__,
                    "commit": _version_del_codigo(),
                    "cuando": datetime.now(timezone.utc).isoformat(),
                    "servidor": socket.gethostname(),
                    "piezas": anotadas,
                }
                crudo = json.dumps(manifiesto, ensure_ascii=False, indent=1)
                info = tarfile.TarInfo(NOMBRE_MANIFIESTO)
                info.size = len(crudo.encode("utf-8"))
                info.mtime = int(datetime.now(timezone.utc).timestamp())
                tar.addfile(_sanear(info), _memoria(crudo))
    except Exception:
        # Un paquete a medias es peor que ninguno: parece una copia y no lo es.
        try:
            destino.unlink()
        except OSError:
            pass
        raise

    return manifiesto


def _memoria(texto: str):
    import io

    return io.BytesIO(texto.encode("utf-8"))


def _dentro_de(hijo: Path, padre: Path) -> bool:
    try:
        hijo.resolve().relative_to(padre.resolve())
        return True
    except (ValueError, OSError):
        return False


def _meter(tar: tarfile.TarFile, pieza: Pieza) -> dict | None:
    """Mete una pieza en el tar y devuelve su linea del manifiesto.

    Lo que no existe no se mete y no es un error: un servidor recien instalado
    no tiene todavia replica.json ni imagen de fondo, y negarse a hacer la
    copia por eso seria negarse justo cuando mas barata es.
    """
    if not pieza.ruta.exists():
        log.info("No hay %s (%s): no va en el paquete", pieza.nombre, pieza.ruta)
        return None

    linea = {"clave": pieza.clave, "nombre": pieza.nombre,
             "carpeta": pieza.carpeta, "origen": str(pieza.ruta)}

    if not pieza.carpeta:
        if not pieza.ruta.is_file():
            return None
        linea["bytes"] = pieza.ruta.stat().st_size
        linea["sha256"] = _sha256(pieza.ruta)
        tar.add(pieza.ruta, arcname=pieza.nombre, filter=_sanear)
        return linea

    archivos = total = 0
    for actual, carpetas, nombres in os.walk(pieza.ruta, onerror=_quejarse):
        carpetas.sort()
        base = Path(actual)
        relativa = base.relative_to(pieza.ruta)
        interno = PurePosixPath(pieza.nombre) / PurePosixPath(relativa.as_posix())
        # TAMBIEN la carpeta raiz de la pieza, y ese "tambien" importa: una
        # carpeta vacia -binarios en un servidor que aun no ha guardado ningun
        # .backup- no tiene archivos que la metan en el tar, asi que sin esta
        # entrada el manifiesto la declaraba y el paquete no la traia. Al
        # restaurar eso se lee, con razon, como un paquete truncado, y la copia
        # entera se rechaza por una carpeta que no tenia nada dentro.
        info = tar.gettarinfo(str(base), arcname=str(interno))
        tar.addfile(_sanear(info))
        for nombre in sorted(nombres):
            sitio = base / nombre
            # Un enlace simbolico dentro del repositorio se salta a proposito.
            # Meterlo tal cual convierte el paquete en algo que al extraerlo
            # escribe donde apunte el enlace; seguirlo puede meter en la copia
            # cualquier archivo del servidor.
            if sitio.is_symlink() or not sitio.is_file():
                continue
            try:
                tar.add(sitio, arcname=str(interno / nombre), filter=_sanear)
                total += sitio.stat().st_size
                archivos += 1
            except OSError as exc:
                raise ErrorMudanza(
                    f"no se pudo leer {sitio}: {exc}"
                ) from exc

    linea["archivos"] = archivos
    linea["bytes"] = total
    return linea


def _quejarse(exc: OSError) -> None:
    raise ErrorMudanza(f"no se pudo recorrer {getattr(exc, 'filename', '?')}: {exc}")


# --- Leer un paquete ---------------------------------------------------------

def _nombre_seguro(nombre: str) -> bool:
    """Nombres relativos, sin subir de carpeta y sin unidades de Windows.

    Extraer un tar es la operacion en la que un archivo elige donde escribirse.
    Este paquete lo produce este mismo programa, pero llega por la red o por un
    pendrive y se restaura como root: no se da por bueno nada.
    """
    if not nombre or nombre.startswith("/") or nombre.startswith("\\"):
        return False
    if ":" in nombre.split("/")[0]:
        return False
    partes = PurePosixPath(nombre).parts
    return bool(partes) and ".." not in partes


def leer_manifiesto(ruta) -> dict:
    """El manifiesto de un paquete, sin extraer nada. Para poder mirarlo antes."""
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorMudanza(f"no existe el archivo: {ruta}")
    try:
        with tarfile.open(ruta, "r:gz") as tar:
            try:
                miembro = tar.getmember(NOMBRE_MANIFIESTO)
            except KeyError:
                raise ErrorMudanza(
                    f"{ruta} no lleva {NOMBRE_MANIFIESTO}: no es un paquete de "
                    "mkbackup, o esta incompleto"
                ) from None
            fuente = tar.extractfile(miembro)
            datos = json.loads(fuente.read().decode("utf-8")) if fuente else None
    except tarfile.TarError as exc:
        raise ErrorMudanza(f"{ruta} no se puede leer como .tar.gz: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ErrorMudanza(f"el manifiesto de {ruta} no se entiende: {exc}") from exc

    if not isinstance(datos, dict) or datos.get("programa") != "mkbackup":
        raise ErrorMudanza(f"{ruta} no es un paquete de mkbackup")
    if int(datos.get("formato") or 0) > FORMATO:
        raise ErrorMudanza(
            f"{ruta} es de formato {datos.get('formato')} y este mkbackup "
            f"entiende hasta el {FORMATO}. Actualiza el programa en este "
            "servidor antes de restaurar."
        )
    return datos


# --- Restaurar ---------------------------------------------------------------

def restaurar(cfg, ruta, sobrescribir: bool = False) -> dict:
    """Deja este servidor como estaba el que produjo el paquete.

    Se hace en dos tiempos a proposito. Primero se extrae ENTERO a una carpeta
    de paso, comprobando los sha256 por el camino; solo si todo el paquete
    estaba bien se empieza a pisar nada. Extraer encima de los datos buenos y
    descubrir a mitad que el archivo estaba truncado deja el servidor sin lo
    viejo y sin lo nuevo, que es el peor resultado posible de una restauracion.

    Lo que ya haya no se borra: se aparta con el sufijo '.antes-de-restaurar'.
    Ocupa el doble un rato, y a cambio equivocarse de paquete se deshace.
    """
    ruta = Path(ruta)
    manifiesto = leer_manifiesto(ruta)
    destinos = {p.clave: p for p in piezas(cfg)}

    # Donde estan los datos de ESTA maquina. La carpeta de paso va ahi al lado
    # para que mover sea un rename y no una copia: entre sistemas de archivos
    # distintos, mover el historico entero seria copiarlo otra vez.
    lado = Path(cfg.almacen.estado).parent
    lado.mkdir(parents=True, exist_ok=True)

    marca = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    paso = Path(tempfile.mkdtemp(prefix=".mudanza-", dir=str(lado)))
    os.chmod(paso, 0o700)

    puestas, ignoradas = [], []
    # (destino, donde se aparto lo que habia). En pares para poder deshacerlo:
    # ver el except de mas abajo.
    movidas: list[tuple[Path, Path]] = []
    try:
        traidas = _extraer(ruta, manifiesto, paso)

        # Antes de tocar nada: si hay algo en medio y no se pidio sobrescribir,
        # se para aqui con el disco intacto.
        if not sobrescribir:
            estorban = [
                str(destinos[clave].ruta)
                for clave in traidas
                if clave in destinos and destinos[clave].ruta.exists()
            ]
            if estorban:
                raise ErrorMudanza(
                    "este servidor ya tiene datos y no se ha pedido "
                    "sobrescribir:\n  " + "\n  ".join(estorban) +
                    "\nSi de verdad quieres reemplazarlos, repite la orden con "
                    "--sobrescribir: lo de ahora se aparta con el sufijo "
                    "'.antes-de-restaurar' y no se borra."
                )

        for clave, origen in traidas.items():
            if clave == "fondo":
                # Caso aparte, y hace falta que lo sea: en el servidor de
                # destino no hay ninguna imagen configurada todavia -es nuevo-,
                # asi que 'fondo' no aparece entre sus piezas y buscarla ahi la
                # daria por desconocida y la tiraria. El destino se compone del
                # nombre que trae el paquete, que es al que apunta el
                # ajustes.json que se esta restaurando al lado.
                final = Path(cfg.almacen.ajustes).parent / Path(origen).name
            else:
                pieza = destinos.get(clave)
                if pieza is None:
                    # Una pieza que este mkbackup no conoce (paquete de una
                    # version mas nueva del mismo formato). Se dice y se sigue:
                    # es mejor restaurar lo que se entiende que negarse del todo.
                    ignoradas.append(clave)
                    continue
                final = pieza.ruta

            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                aparte = final.with_name(f"{final.name}.antes-de-restaurar-{marca}")
                os.replace(final, aparte)
                movidas.append((final, aparte))
            os.replace(origen, final)
            puestas.append(str(final))
    except OSError as exc:
        # Se rompio a mitad de colocar las piezas. Lo que queda es lo peor
        # posible: unas cosas del paquete y otras de antes, y encima el
        # archivo de cuentas puede haberse quedado solo con el sufijo. Ese
        # estado concreto ENTREGA EL PANEL, porque al arrancar sin usuarios.json
        # se siembra una cuenta de administrador con el hash que traiga el
        # config.yaml, que es justo la pieza que ya se cambio (va la primera).
        #
        # Asi que se deshace: cada archivo apartado vuelve a su sitio, en orden
        # inverso. Lo que ya se habia colocado del paquete se pierde, que es lo
        # que se quiere -se estaba abortando-, y el servidor se queda como
        # estaba.
        devueltas, rotas = 0, []
        for final, aparte in reversed(movidas):
            try:
                os.replace(aparte, final)
                devueltas += 1
            except OSError:
                rotas.append(str(final))
        log.error("La restauracion fallo a mitad (%s); devueltas %d piezas",
                  exc, devueltas)
        if rotas:
            raise ErrorMudanza(
                f"la restauracion fallo ({exc}) y ADEMAS no se pudo dejar todo "
                "como estaba. Sin arreglar se quedaron: " + ", ".join(rotas) +
                ". Sus copias llevan el sufijo '.antes-de-restaurar-"
                f"{marca}'; hay que devolverlas a mano antes de arrancar los "
                "servicios."
            ) from exc
        raise ErrorMudanza(
            f"la restauracion fallo ({exc}). El servidor se ha dejado como "
            "estaba: no se cambio ninguna pieza."
        ) from exc
    finally:
        _borrar_arbol(paso)

    avisos = _revisar_rutas(cfg, puestas)
    return {"manifiesto": manifiesto, "puestas": puestas,
            "apartadas": [str(a) for _, a in movidas],
            "ignoradas": ignoradas, "avisos": avisos}


def _extraer(ruta: Path, manifiesto: dict, paso: Path) -> dict[str, Path]:
    """Saca el paquete a la carpeta de paso. Devuelve {clave: ruta extraida}.

    Comprueba los sha256 de los archivos sueltos contra el manifiesto. No es
    paranoia: un .tar.gz truncado a mitad de descarga sigue leyendose bien
    hasta donde llego, y el archivo que estaba a medias es justo el que hay que
    no dar por bueno. Las carpetas no llevan hash una a una -serian miles de
    lineas de manifiesto-; ahi responde el CRC del propio gzip, que si detecta
    un corte.
    """
    # El nombre que dice el MANIFIESTO se valida igual que el de un miembro del
    # tar, y esto no es por simetria.
    #
    # Toda la validacion de rutas estaba en el bucle de los miembros, pero
    # despues se recorre esta otra lista, que sale del JSON del manifiesto, y
    # con ella se compone `paso / nombre`. Ahi hay una trampa de pathlib: unir
    # con una ruta ABSOLUTA no la mete dentro, la sustituye entera, o sea que
    # `paso / "/etc/shadow"` es `/etc/shadow` a secas. Como no era un miembro
    # del tar no habia archivo que extraer, pero `sitio.exists()` decia que si
    # -claro, existe en el sistema- y el `os.replace` de mas abajo MOVIA ese
    # archivo a la carpeta de datos. Corriendo como root, eso es borrar
    # cualquier cosa del disco y ademas leersela.
    por_nombre = {}
    for pieza in manifiesto.get("piezas") or []:
        if not isinstance(pieza, dict):
            continue
        nombre = pieza.get("nombre")
        if not isinstance(nombre, str) or not _nombre_seguro(nombre):
            raise ErrorMudanza(
                f"el manifiesto declara una ruta que no se acepta: {nombre!r}"
            )
        por_nombre[nombre] = pieza
    if not por_nombre:
        raise ErrorMudanza("el paquete no declara ninguna pieza")

    # Tope de lo que se puede escribir al desempaquetar. Sin el, un .tar.gz de
    # dos megas que dice tener dentro un archivo de dos gigas se extrae entero:
    # comprimir ceros sale casi gratis, asi que subir cien megas basta para
    # llenar el disco del servidor. Y no se queda en un susto pasajero, porque
    # al terminar la extraccion esos gigas se mueven a su sitio definitivo.
    #
    # Se acota por lo que el propio manifiesto dice que ocupa (con holgura) y
    # ademas por el disco que queda: un manifiesto tambien se puede inflar.
    declarado = sum(int(p.get("bytes") or 0) for p in por_nombre.values())
    tope = declarado + MARGEN
    try:
        libre = shutil.disk_usage(paso).free
    except OSError:
        libre = None
    if libre is not None:
        tope = min(tope, max(0, libre - MARGEN))
    if tope <= 0:
        raise ErrorMudanza(
            "no queda disco suficiente para desempaquetar la copia aqui"
        )
    escrito = [0]

    with tarfile.open(ruta, "r:gz") as tar:
        for miembro in tar:
            if miembro.name == NOMBRE_MANIFIESTO:
                continue
            if not _nombre_seguro(miembro.name):
                raise ErrorMudanza(
                    f"el paquete lleva una ruta que no se acepta: {miembro.name!r}"
                )
            # Solo archivos y carpetas. Un enlace, un dispositivo o un fifo
            # dentro de un tar que se extrae como root no tienen ninguna razon
            # legitima de estar aqui, y si tienen varias de no estarlo.
            if not (miembro.isfile() or miembro.isdir()):
                raise ErrorMudanza(
                    f"el paquete lleva algo que no es un archivo ni una "
                    f"carpeta: {miembro.name!r}"
                )
            raiz = PurePosixPath(miembro.name).parts[0]
            if raiz not in por_nombre:
                raise ErrorMudanza(
                    f"el paquete lleva {miembro.name!r}, que no esta declarado "
                    "en el manifiesto"
                )
            _sacar(tar, miembro, paso, tope, escrito)

    traidas: dict[str, Path] = {}
    for nombre, declarada in por_nombre.items():
        sitio = paso / nombre
        # Cinturon ademas del tirante: aunque `nombre` ya se valido arriba, lo
        # que se compone aqui es la ruta que se va a mover, y esa es la que
        # tiene que estar dentro de la carpeta de paso.
        if not _dentro_de_o_igual(sitio, paso):
            raise ErrorMudanza(f"ruta fuera de sitio en el manifiesto: {nombre!r}")
        if not sitio.exists():
            raise ErrorMudanza(
                f"el manifiesto declara {nombre} pero el paquete no lo trae: "
                "esta incompleto"
            )
        esperado = declarada.get("sha256")
        if esperado and sitio.is_file() and _sha256(sitio) != esperado:
            raise ErrorMudanza(
                f"{nombre} no coincide con lo que dice el manifiesto: el "
                "paquete esta corrupto o se modifico. No se ha tocado nada."
            )
        traidas[str(declarada.get("clave") or nombre)] = sitio
    return traidas


def _sacar(tar: tarfile.TarFile, miembro: tarfile.TarInfo, paso: Path,
           tope: int, escrito: list) -> None:
    """Escribe un miembro ya validado, siempre con permisos privados.

    Se escribe a mano en vez de usar extract() porque asi los permisos son los
    que decide este codigo y no los que traiga el tar, y porque la ruta final
    se compone aqui: dentro del paso y en ningun otro sitio.
    """
    final = paso / Path(*PurePosixPath(miembro.name).parts)
    if not _dentro_de_o_igual(final, paso):
        raise ErrorMudanza(f"ruta fuera de sitio en el paquete: {miembro.name!r}")

    if miembro.isdir():
        final.mkdir(parents=True, exist_ok=True)
        os.chmod(final, 0o700)
        return

    final.parent.mkdir(parents=True, exist_ok=True)
    fuente = tar.extractfile(miembro)
    if fuente is None:
        raise ErrorMudanza(f"no se pudo leer {miembro.name!r} del paquete")
    descriptor = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as fh:
        for trozo in iter(lambda: fuente.read(TROZO), b""):
            escrito[0] += len(trozo)
            # Se corta EN MEDIO de escribir, no despues: la gracia de una bomba
            # de descompresion es justo que lo que ocupa se sabe cuando ya se
            # escribio. El sha256 del manifiesto no sirve aqui por lo mismo, y
            # ademas lo elige quien fabrica el paquete.
            if escrito[0] > tope:
                raise ErrorMudanza(
                    f"el paquete se expande mas de lo que declara (mas de "
                    f"{tope // (1024 * 1024)} MB al llegar a {miembro.name!r}): "
                    "esta manipulado o no cabe aqui. No se ha tocado nada."
                )
            fh.write(trozo)


def _dentro_de_o_igual(hijo: Path, padre: Path) -> bool:
    try:
        # Sin resolve() en el hijo: todavia no existe. Se normaliza el texto,
        # que es suficiente porque los '..' ya se rechazaron antes.
        Path(os.path.normpath(hijo)).relative_to(Path(os.path.normpath(padre)))
        return True
    except ValueError:
        return False


def _borrar_arbol(raiz: Path) -> None:
    try:
        shutil.rmtree(raiz, ignore_errors=True)
    except OSError:
        pass


def _revisar_rutas(cfg, puestas: list[str]) -> list[str]:
    """Avisa si el config.yaml restaurado apunta a otro sitio del que se uso.

    Puede pasar de verdad: los datos se colocan donde dice la configuracion de
    ESTA maquina, y acto seguido se restaura el config.yaml del origen, que
    manda a partir del proximo arranque. Si los dos servidores usan la
    distribucion de siempre (/root/mkbackup) coinciden y no hay nada que decir.
    Si no, el sistema arrancaria mirando carpetas vacias y pareceria que la
    restauracion no hizo nada. Se dice; arreglarlo es editar una ruta.
    """
    if not cfg.ruta or str(cfg.ruta) not in puestas:
        return []
    try:
        from .config import Config

        nueva = Config.desde_archivo(cfg.ruta)
    except Exception as exc:  # noqa: BLE001
        return [f"El config.yaml restaurado no se pudo releer: {exc}"]

    avisos = []
    for campo, antes, ahora in (
        ("del repositorio", cfg.almacen.git, nueva.almacen.git),
        ("de los binarios", cfg.almacen.binarios, nueva.almacen.binarios),
        ("del inventario", cfg.inventario, nueva.inventario),
        ("de las cuentas", cfg.almacen.usuarios, nueva.almacen.usuarios),
    ):
        if str(antes) != str(ahora):
            avisos.append(
                f"Los datos {campo} se pusieron en {antes}, pero el "
                f"config.yaml restaurado dice {ahora}. Muevelos o corrige esa "
                "ruta antes de arrancar los servicios."
            )
    return avisos
