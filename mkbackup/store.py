"""Almacenamiento: git para el texto, disco con retencion para los binarios.

Decision de diseno: el .rsc va a git y el .backup NO.

El binario de RouterOS cambia en cada ejecucion aunque la configuracion sea
identica (lleva estado interno y marcas de tiempo), y git no sabe deltificar
binarios. Con 300 equipos serian gigas al ano de objetos irrepetibles, y se
perderia justo lo que se busca: que el repo guarde solo cambios reales.

Por eso el binario se guarda aparte, con retencion por equipo, y solo se
regenera cuando el export en texto delato un cambio de verdad.

Ambos arboles, el de git y el de binarios, usan el mismo camino: empresa/equipo.
La empresa va primero porque cada cliente del ISP debe quedar aislado: su
historico se ve entero con `git log -- <empresa>/` y dos clientes pueden tener
un equipo con el mismo nombre sin pisarse.

El grupo NO esta en la ruta: es una etiqueta para filtrar y ver la flota
("bts", "core"), no un dato de identidad. Cuando estaba en la ruta, mover un
equipo de grupo lo sacaba de su carpeta y partia en dos su historico por un
cambio que no tiene nada que ver con el equipo. Ahora ese cambio no toca el
repo. Consecuencia buscada: el nombre debe ser unico DENTRO de la empresa, y de
eso ya se encarga el inventario.

Para que los dos arboles no puedan divergir, la carpeta de binarios de un
equipo se deriva SIEMPRE de su ruta en el repo (ver guardar_binario): no se
vuelve a componer a mano en ningun sitio.

Nota de concurrencia: git no admite operaciones simultaneas sobre el mismo
indice (index.lock). Esta clase debe usarse desde un solo hilo; el trabajo
paralelo es el SSH, que ocurre antes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .config import Config
from .device import Resultado
from .inventory import Equipo

log = logging.getLogger("mkbackup.almacen")


class ErrorAlmacen(Exception):
    """No se pudo guardar o versionar."""


# --- Como fue la ultima subida al remoto ------------------------------------
# Lo escribe el proceso que respalda y lo lee el panel, que es otro proceso: por
# eso va a un archivo y no a una variable. Es pequeno a proposito -cuando, si
# salio bien y que dijo- porque se lee en cada pintado de Ajustes.
#
# Sin esto, saber si los respaldos estan llegando al repositorio remoto exige
# entrar por SSH al servidor y leer el journal. Una copia fuera que lleva tres
# semanas fallando y nadie lo sabe es lo mismo que no tener copia fuera.

NOMBRE_REPLICA = "replica.json"


def ruta_replica(cfg) -> Path:
    return Path(cfg.almacen.estado).parent / NOMBRE_REPLICA


def leer_replica(cfg) -> dict:
    """Lo ultimo que se sabe de la subida. NUNCA lanza: lo llama el panel."""
    try:
        datos = json.loads(ruta_replica(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def anotar_replica(cfg, ok: bool, detalle: str, pendientes: int = 0) -> None:
    """Deja constancia de como fue. NUNCA lanza: no puede tumbar un ciclo."""
    ruta = ruta_replica(cfg)
    datos = {
        "cuando": datetime.now(timezone.utc).isoformat(),
        "ok": bool(ok),
        "detalle": detalle,
        "pendientes": int(pendientes),
    }
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        temporal = ruta.with_suffix(".tmp")
        temporal.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        os.replace(temporal, ruta)
    except OSError as exc:
        log.warning("No se pudo anotar el resultado de la subida: %s", exc)


@dataclass
class Guardado:
    equipo: Equipo
    cambio: bool
    commit: str = ""
    binario_guardado: bool = False


def _git(repo: Path, *args: str, permitir_fallo: bool = False,
         previos: tuple = (), tapar: tuple = (), entorno: dict | None = None) -> str:
    """Ejecuta git. `previos` van ANTES del subcomando (las opciones -c ...).

    `tapar` son cadenas que no pueden salir en el mensaje de error. Existe por
    una razon concreta: la credencial del repositorio remoto viaja como opcion
    de git, y el mensaje de error de un push fallido acaba en el log del
    servicio, en el panel y en el correo de aviso. Un token que se filtra por
    el mensaje de error de la operacion que lo usa es la forma clasica de
    perderlo.
    """
    orden = ["git", "-C", str(repo), *previos, *args]
    ambiente = {**os.environ, **(entorno or {})}
    proc = subprocess.run(
        orden,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ambiente,
    )
    if proc.returncode != 0 and not permitir_fallo:
        # Los argumentos NO se vuelcan tal cual: entre ellos puede ir la
        # cabecera de autorizacion. Se nombra el subcomando y ya.
        detalle = (proc.stderr or proc.stdout).strip()
        for secreto in tapar:
            if secreto:
                detalle = detalle.replace(secreto, "«oculto»")
        raise ErrorAlmacen(f"git {args[0] if args else '?'} fallo "
                           f"({proc.returncode}): {detalle}")
    return proc.stdout


def _abrir_privado(ruta, flags: int) -> int:
    """opener para open(): crea el archivo con permisos 0600 desde el principio.

    Un .backup de RouterOS lleva certificados, claves SSH y las passwords de los
    usuarios del equipo. Crearlo con los permisos por defecto y arreglarlo
    despues con chmod deja una ventana, corta pero real, en la que cualquiera
    con acceso al servidor puede leerlo.
    """
    return os.open(ruta, flags, 0o600)


def _sin_extension(ruta: str) -> str:
    """'acme/router.rsc' -> 'acme/router'.

    Es a la vez lo que se escribe en el mensaje de commit (igual que en guardar)
    y la carpeta de binarios del equipo, que copia el arbol del repo.
    """
    p = PurePosixPath(ruta)
    return str(p.with_suffix("")) if p.suffix else ruta


def _ruta_repo_valida(ruta: str) -> bool:
    """Ruta relativa, dentro del repo y que git no pueda leer como una opcion.

    Estas rutas se arman con datos que vienen de fuera (el nombre que el panel
    escribio en el inventario, la identidad que contesto un router), asi que se
    revisan antes de pasarlas a un proceso: '../..' escribiria fuera del repo y
    un nombre que empiece por '-' lo tomaria git como un modificador. El '--'
    de los comandos cubre lo segundo, pero se valida igual: el dia que alguien
    escriba un git sin '--' esto sigue tapando el agujero.
    """
    if not ruta or ruta.startswith("-") or ruta.endswith("/"):
        return False
    # Una ruta de Windows ('C:/...') no es relativa aunque no empiece por barra.
    if ":" in ruta:
        return False
    p = PurePosixPath(ruta)
    if p.is_absolute():
        return False
    return bool(p.parts) and all(parte not in ("", ".", "..") for parte in p.parts)


class Almacen:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.repo = Path(cfg.almacen.git)
        self.dir_binarios = Path(cfg.almacen.binarios)
        # carpeta de binarios -> ultima marca de tiempo que se le asigno a un
        # archivo. Sirve para que el sello nunca retroceda; ver _archivo_nuevo.
        self._ultima_marca: dict[str, datetime] = {}

    def inicializar(self) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        if not (self.repo / ".git").is_dir():
            _git(self.repo, "init", "-q")
            # Identidad local al repo: no depende de la config global del host.
            _git(self.repo, "config", "user.name", self.cfg.almacen.autor)
            _git(self.repo, "config", "user.email", self.cfg.almacen.email)
            # Los .rsc son texto: forzar LF para que el diff sea estable
            # aunque alguien edite el repo desde Windows.
            (self.repo / ".gitattributes").write_text(
                "*.rsc text eol=lf\n", encoding="utf-8"
            )
            _git(self.repo, "add", ".gitattributes")
            _git(self.repo, "commit", "-q", "-m", "Repositorio de configuraciones")

        if self.cfg.binario.activo:
            self.dir_binarios.mkdir(parents=True, exist_ok=True)

    # --- Texto (git) --------------------------------------------------------

    def guardar(self, resultado: Resultado) -> Guardado:
        equipo = resultado.equipo
        # ruta_relativa ya es empresa/nombre.rsc: el primer nivel es el cliente
        # para que su historico completo salga con un solo
        # `git log -- <empresa>/` y para que dos clientes con equipos
        # homonimos no compartan archivo.
        destino = self.repo / equipo.ruta_relativa
        destino.parent.mkdir(parents=True, exist_ok=True)

        anterior = ""
        if destino.is_file():
            anterior = destino.read_text(encoding="utf-8", errors="replace")

        if anterior == resultado.export:
            # Sin cambios: ni se escribe ni se commitea. Esto es lo que
            # mantiene el repo limpio y las alertas creibles.
            return Guardado(equipo=equipo, cambio=False)

        # newline="\n" explicito: si esto corriera en Windows, el texto se
        # guardaria con CRLF y cada respaldo pareceria un cambio completo.
        destino.write_text(resultado.export, encoding="utf-8", newline="\n")

        _git(self.repo, "add", "--", equipo.ruta_relativa)

        primero = not anterior
        accion = "Alta" if primero else "Cambio"
        # El mensaje reproduce la ruta real del archivo (sin la extension), asi
        # un `git log --oneline` dice de que cliente es cada cambio y se puede
        # filtrar por empresa con un simple `grep acme/`. Si el mensaje solo
        # dijera el nombre, dos clientes con un equipo homonimo darian lineas
        # de historial indistinguibles.
        #
        # Se deriva de ruta_relativa en vez de componerlo con los campos del
        # equipo: asi el mensaje no puede describir una ruta que no existe (el
        # grupo estuvo aqui hasta que salio de la ruta, y el mensaje siguio
        # anunciandolo un rato mas).
        mensaje = f"{accion}: {_sin_extension(equipo.ruta_relativa)}"
        if resultado.version:
            mensaje += f" (RouterOS {resultado.version})"

        _git(self.repo, "commit", "-q", "-m", mensaje, "--", equipo.ruta_relativa)
        commit = _git(self.repo, "rev-parse", "--short", "HEAD").strip()

        guardado = Guardado(equipo=equipo, cambio=True, commit=commit)

        if resultado.binario:
            guardado.binario_guardado = self.guardar_binario(equipo, resultado.binario)

        return guardado

    def renombrar(self, ruta_vieja: str, ruta_nueva: str) -> bool:
        """Mueve un respaldo dentro del repo conservando su historico.

        Lo usa el alta sin nombre: el equipo entra con la IP como nombre
        provisional y, cuando el primer respaldo con exito revela su
        /system identity, su archivo pasa a llamarse como el router.

        Se hace con 'git mv' y no borrando el archivo viejo y creando el nuevo
        porque git guarda contenidos, no renombrados: si el archivo se borra y
        se crea aparte, 'git log --follow' corta ahi y el historico anterior
        deja de verse desde la ruta nueva. Ese historico es exactamente lo que
        este proyecto existe para guardar: perderlo al renombrar seria tirar los
        meses de cambios de configuracion del equipo. Con git mv el cambio se
        registra como borrado+alta con el MISMO contenido, que es la pista que
        --follow usa para seguir el rastro hacia atras.

        Devuelve True solo si el commit se hizo. Nunca lanza: quien llama es el
        panel, y un renombrado fallido no puede tumbar un respaldo que ya se
        guardo bien.
        """
        vieja = (ruta_vieja or "").strip().replace("\\", "/")
        nueva = (ruta_nueva or "").strip().replace("\\", "/")

        for etiqueta, ruta in (("origen", vieja), ("destino", nueva)):
            if not _ruta_repo_valida(ruta):
                log.error("renombrar: ruta de %s no valida: %r", etiqueta, ruta)
                return False

        if vieja == nueva:
            log.error("renombrar: origen y destino son la misma ruta (%s)", vieja)
            return False

        origen = self.repo / vieja
        destino = self.repo / nueva

        if not origen.is_file():
            log.error("renombrar: no existe el origen %s", vieja)
            return False
        # No se pisa nunca: el destino ocupado es el respaldo de OTRO equipo, y
        # sobrescribirlo mezclaria dos historicos en un mismo archivo.
        if destino.exists():
            log.error(
                "renombrar: el destino %s ya existe; seria el respaldo de otro "
                "equipo y no se pisa", nueva
            )
            return False

        # El equipo puede cambiar de nombre y de empresa a la vez, asi que la
        # carpeta de destino puede no existir todavia. (Cambiar de grupo ya no
        # mueve nada: el grupo salio de la ruta.)
        destino.parent.mkdir(parents=True, exist_ok=True)

        # Mismo formato que los mensajes de guardar: ruta real sin extension,
        # empresa incluida, para que un 'git log --oneline' siga diciendo de que
        # cliente es cada linea.
        mensaje = (
            f"Renombrado: {_sin_extension(vieja)} -> {_sin_extension(nueva)}"
        )

        try:
            _git(self.repo, "mv", "--", vieja, nueva)
        except ErrorAlmacen as exc:
            log.error("renombrar: git mv fallo (%s -> %s): %s", vieja, nueva, exc)
            return False

        try:
            _git(self.repo, "commit", "-q", "-m", mensaje, "--", vieja, nueva)
            commit = _git(self.repo, "rev-parse", "--short", "HEAD").strip()
        except ErrorAlmacen as exc:
            log.error("renombrar: no se pudo commitear %s: %s", mensaje, exc)
            # Deshacer el mv: si no, el arbol de trabajo queda con el archivo
            # movido y el indice a medias, y el proximo respaldo escribiria el
            # nombre viejo creyendo que es un alta.
            _git(self.repo, "mv", "--", nueva, vieja, permitir_fallo=True)
            return False

        log.info("Renombrado %s -> %s (%s)", vieja, nueva, commit)

        # Los binarios van fuera de git, asi que ahi no hay historico que
        # preservar, pero si se quedan en la carpeta vieja quedan huerfanos:
        # nadie los rota (la retencion mira la carpeta del nombre actual) y el
        # panel no los encuentra. Se mueven despues del commit, a proposito: si
        # fallan, el renombrado en git ya esta hecho y sigue siendo valido.
        self._mover_binarios(vieja, nueva)
        return True

    # --- Binario (disco con retencion) --------------------------------------

    def guardar_binario(self, equipo: Equipo, datos: bytes) -> bool:
        if not self.cfg.binario.activo:
            return False

        # La carpeta se saca de la ruta del .rsc en el repo, no se compone con
        # los campos del equipo. La empresa encabeza la ruta igual que en git:
        # cada cliente del ISP queda aislado, dos clientes pueden tener un
        # equipo homonimo sin pisarse, y entregar o borrar todo lo de un
        # cliente es copiar o borrar un solo directorio. Derivarla de
        # ruta_relativa es lo que garantiza que los dos arboles sean identicos
        # hoy y despues del proximo cambio de estructura: _mover_binarios hace
        # exactamente la misma derivacion, asi que un renombrado siempre
        # encuentra la carpeta donde este metodo la dejo.
        carpeta = self.dir_binarios / PurePosixPath(
            _sin_extension(equipo.ruta_relativa)
        )
        carpeta.mkdir(parents=True, exist_ok=True)

        destino = self._archivo_nuevo(carpeta, equipo.nombre, datos)
        if destino is None:
            return False

        self._rotar(carpeta)
        return True

    def _archivo_nuevo(self, carpeta: Path, nombre: str, datos: bytes) -> Path | None:
        """Escribe el .backup con un nombre nuevo y creciente. None si no pudo.

        El nombre es '<equipo>_AAAAMMDD_HHMMSS_uuuuuu.backup' y tiene que
        cumplir DOS cosas a la vez:

        1. SER UNICO. El reloj no basta por mucha resolucion que se le pida: en
           Windows datetime.now() avanza a saltos de ~15 ms (hasta Python
           3.13), asi que varias escrituras seguidas leen LA MISMA hora. Con el
           sello a milisegundos eso ya ocurria: dos respaldos del mismo equipo
           demasiado juntos generaban el mismo nombre y el segundo pisaba al
           primero. Una copia se perdia sin aviso y la retencion conservaba
           menos copias de las configuradas.

        2. ORDENAR POR NOMBRE == ORDENAR POR FECHA. De ahi saca _rotar cual es
           la copia mas vieja (ver alli por que no sirve el mtime). Esto
           descarta la solucion facil: un sufijo aleatorio haria los nombres
           unicos pero dejaria el orden alfabetico a suertes, y la rotacion
           borraria las copias equivocadas.

        La marca es un instante que solo avanza: se lee el reloj y, si no ha
        pasado de la ultima marca que se uso en esta carpeta, se toma esa
        ultima mas un microsegundo. El ancho del campo es fijo (6 digitos con
        ceros), asi que comparar los nombres como texto es compararlos como
        numeros y (2) se cumple sola. Lo que se falsea son microsegundos en una
        marca cuya unica funcion es ordenar: no engana a nadie.

        La ultima marca se recuerda EN MEMORIA y no se deduce de los archivos
        que hay en disco, aunque parezca lo obvio. En disco no estan todos:
        _rotar acaba de borrar los mas viejos, y sus nombres vuelven a quedar
        libres. Con el reloj parado en el mismo tic, la siguiente escritura
        reocuparia uno de esos nombres recien liberados y aterrizaria ANTES que
        las copias que se guardaron justo antes que ella: (2) rota, y la
        rotacion siguiente descarta la copia nueva creyendola vieja. Es
        exactamente el fallo que se estaba arreglando, reaparecido por otra
        puerta.

        El diccionario tiene una entrada por carpeta, o sea por equipo del
        inventario: unos cientos como mucho, y viven lo que el proceso.

        Aparte de eso, el archivo se crea en modo exclusivo ('x') en lugar de
        mirar antes con exists(): entre la comprobacion y la escritura cabe
        otro proceso creando ese mismo nombre. Quien decide es el sistema de
        archivos, que es el unico que puede decidirlo sin carreras.
        """
        clave = str(carpeta)
        marca = datetime.now(timezone.utc)
        ultima = self._ultima_marca.get(clave)
        if ultima is not None and marca <= ultima:
            marca = ultima + timedelta(microseconds=1)

        # 1000 intentos son 1000 sellos consecutivos ya ocupados en la carpeta
        # de un solo equipo: no ocurre. El tope esta para que un error raro que
        # se presente como FileExistsError no deje al respaldo nocturno dando
        # vueltas para siempre.
        for _ in range(1000):
            # %f son los microsegundos con 6 digitos y ceros a la izquierda.
            destino = carpeta / f"{nombre}_{marca:%Y%m%d_%H%M%S_%f}.backup"
            try:
                # El opener crea el archivo ya con 0600: escribir primero y
                # hacer chmod despues deja una ventana en la que los
                # certificados y claves de dentro los puede leer cualquiera.
                with open(destino, "xb", opener=_abrir_privado) as fh:
                    fh.write(datos)
            except FileExistsError:
                marca += timedelta(microseconds=1)
                continue
            except OSError as exc:
                # No se propaga: el respaldo en git, que es lo que de verdad no
                # se puede perder, ya esta commiteado cuando se llega aqui.
                log.error("no se pudo guardar el binario en %s: %s", carpeta, exc)
                return None
            self._ultima_marca[clave] = marca
            return destino

        log.error(
            "no se encontro un nombre libre para el binario en %s; se descarta",
            carpeta,
        )
        return None

    def _rotar(self, carpeta: Path) -> None:
        # Se ordena por NOMBRE, no por mtime: el nombre lleva el sello
        # AAAAMMDD_HHMMSS_uuuuuu, de ancho fijo, que ordena cronologicamente de
        # forma determinista (ver _archivo_nuevo). El mtime puede empatar entre
        # archivos escritos en el mismo tic del reloj, y entonces la rotacion
        # borraria al azar.
        copias = sorted(carpeta.glob("*.backup"), key=lambda p: p.name, reverse=True)
        for vieja in copias[self.cfg.binario.retencion:]:
            try:
                vieja.unlink()
            except OSError:
                pass

    def _mover_binarios(self, ruta_vieja: str, ruta_nueva: str) -> None:
        """Acompana al renombrado del repo con la carpeta de binarios.

        El arbol de binarios copia el del repo (empresa/nombre, ver
        guardar_binario), asi que la carpeta se saca de la propia ruta del .rsc
        quitandole la extension. Es la MISMA derivacion que hace
        guardar_binario, a proposito: mientras las dos salgan de ruta_relativa,
        un cambio en la estructura de rutas no puede dejar aqui una carpeta que
        alli no exista. Con el grupo en la ruta esto eran tres niveles y ahora
        son dos, y ni este metodo ni guardar_binario tuvieron que enterarse.

        Es best-effort: cualquier fallo se registra y ya. El respaldo en git,
        que es lo que no se puede perder, ya esta commiteado.
        """
        origen = self.dir_binarios / PurePosixPath(_sin_extension(ruta_vieja))
        destino = self.dir_binarios / PurePosixPath(_sin_extension(ruta_nueva))

        if not origen.is_dir():
            return  # equipo sin binarios (o binario desactivado): nada que mover

        if destino.exists():
            log.warning(
                "renombrar: %s ya existe, los binarios se quedan en %s",
                destino, origen,
            )
            return

        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            origen.replace(destino)
        except OSError as exc:
            log.warning("renombrar: no se pudieron mover los binarios: %s", exc)
            return

        # Renombrar tambien el prefijo de cada archivo, no es cosmetica: _rotar
        # ordena por NOMBRE para decidir que copia es la mas vieja, y el nombre
        # es '<equipo>_<sello>.backup'. Si en la carpeta conviven dos prefijos
        # distintos, el orden pasa a ser alfabetico por prefijo y no
        # cronologico, y la retencion borraria las copias equivocadas.
        viejo = PurePosixPath(ruta_vieja).stem
        nuevo = PurePosixPath(ruta_nueva).stem
        if viejo == nuevo:
            return
        for archivo in destino.glob(f"{viejo}_*.backup"):
            sello = archivo.name[len(viejo):]  # '_AAAAMMDD_HHMMSS_uuuuuu.backup'
            try:
                archivo.replace(destino / f"{nuevo}{sello}")
            except OSError as exc:
                log.warning("renombrar: no se pudo renombrar %s: %s", archivo, exc)

    # --- Replicacion --------------------------------------------------------

    def _credencial(self) -> tuple[list, list, dict]:
        """(opciones -c, secretos a tapar, variables de entorno) para hablar con el remoto.

        La credencial NO se mete en la URL ni se guarda en .git/config, que es
        lo que hace todo el mundo y es justo lo que no hay que hacer aqui: el
        repositorio se copia, se clona y se mira, y una URL con el token dentro
        viaja en cada una de esas copias, ademas de salir en el log de cualquier
        error de red. Va como cabecera en la linea de comandos de ESTA orden y
        muere con ella.

        Para SSH no hay token: manda la llave del sistema, y lo unico que se
        hace es no dejar que git pare a preguntar nada. Un push que se queda
        esperando una contrasena en un servicio desatendido no falla: se cuelga.
        """
        entorno = {
            # Sin esto, un remoto que pide credenciales deja el proceso parado
            # esperando a alguien que no hay. Con esto falla, que se puede ver.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
        }
        # Y esto apaga el ayudante de credenciales del sistema. Las variables de
        # arriba tapan el aviso por terminal, pero NO un credential.helper puesto
        # en el gitconfig de la maquina: ese abre su propia ventana (o consulta
        # un llavero) y el proceso se queda esperando a nadie. En el Debian del
        # despliegue no suele haber ninguno, o sea que esto no arregla nada hoy;
        # esta para el dia que lo haya, porque el sintoma seria un servicio
        # colgado sin una sola linea en el log.
        siempre = ["-c", "credential.helper="]

        token = self.cfg.almacen.remoto_token
        if not token:
            return siempre, [], entorno

        usuario = self.cfg.almacen.remoto_usuario or "x-access-token"
        basica = base64.b64encode(
            f"{usuario}:{token}".encode("utf-8")
        ).decode("ascii")
        return (
            [*siempre, "-c", f"http.extraheader=Authorization: Basic {basica}"],
            (token, basica),
            entorno,
        )

    def _rama(self) -> str:
        pedida = (self.cfg.almacen.remoto_rama or "").strip()
        if pedida:
            return pedida
        return _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def _apuntar_remoto(self) -> None:
        actual = _git(self.repo, "remote", "get-url", "origin",
                      permitir_fallo=True).strip()
        if not actual:
            _git(self.repo, "remote", "add", "origin", self.cfg.almacen.remoto)
        elif actual != self.cfg.almacen.remoto:
            _git(self.repo, "remote", "set-url", "origin", self.cfg.almacen.remoto)

    def probar_remoto(self) -> str:
        """Comprueba que se llega al remoto y que la credencial vale.

        Es 'git ls-remote': pregunta que ramas hay y no escribe nada. Existe
        porque la alternativa para saber si la configuracion esta bien es
        esperar al proximo ciclo y mirar el log, y quien acaba de pegar un
        token quiere saberlo AHORA. Lanza ErrorAlmacen con el motivo.
        """
        if not self.cfg.almacen.remoto:
            raise ErrorAlmacen("no hay ninguna direccion de repositorio puesta")

        previos, tapar, entorno = self._credencial()
        salida = _git(self.repo, "ls-remote", "--heads", self.cfg.almacen.remoto,
                      previos=previos, tapar=tapar, entorno=entorno)
        ramas = [l.split("refs/heads/")[-1] for l in salida.splitlines() if l.strip()]
        if not ramas:
            return "se llega al repositorio, y esta vacio (el primer push lo llena)"
        return f"se llega al repositorio. Ramas: {', '.join(sorted(ramas)[:8])}"

    def replicar(self) -> str:
        """Empuja al remoto configurado. Devuelve texto descriptivo."""
        if not self.cfg.almacen.remoto:
            return "sin remoto configurado"

        self._apuntar_remoto()
        rama = self._rama()
        previos, tapar, entorno = self._credencial()
        local = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        _git(self.repo, "push", "-q", "origin", f"{local}:{rama}",
             previos=previos, tapar=tapar, entorno=entorno)
        return f"replicado a {self.cfg.almacen.remoto} ({rama})"

    # --- Consulta -----------------------------------------------------------

    def equipos_sin_respaldo(self, equipos: list[Equipo]) -> list[Equipo]:
        """Equipos del inventario que nunca se han respaldado con exito."""
        return [e for e in equipos if not (self.repo / e.ruta_relativa).is_file()]

    def huerfanos(self, equipos: list[Equipo]) -> list[dict]:
        """Respaldos que quedan en el repositorio de equipos ya dados de baja.

        Dar de baja un equipo no borra sus respaldos, y esta bien que sea asi:
        el dia que alguien pregunta como estaba configurado ese router antes de
        retirarlo, la respuesta esta aqui. Pero un cliente que se va tiene
        derecho a pedir que no quede nada suyo, y un equipo de pruebas que
        nunca existio de verdad solo estorba. Esta lista es la unica forma de
        ver que hay guardado de gente que ya no esta.

        Se compara contra ruta_relativa y no contra el nombre: dos empresas
        pueden tener un equipo homonimo, y mirar solo el nombre daria por vivo
        el archivo del cliente que se fue porque otro cliente tiene un router
        que se llama igual.

        No lanza nunca: esto pinta una pantalla, y un repositorio recien creado
        o a medio inicializar no puede dejar Ajustes sin abrir.
        """
        if not self.repo.is_dir():
            return []

        vivos = {
            (e.ruta_relativa or "").replace("\\", "/") for e in equipos
        }

        sueltos = []
        for archivo in sorted(self.repo.rglob("*.rsc")):
            ruta = archivo.relative_to(self.repo).as_posix()
            # Lo de dentro de .git son las tripas de git, no respaldos. No
            # deberia haber ningun .rsc ahi, pero un repositorio se puede
            # ensuciar de mil formas y ofrecer "borrar" un objeto interno seria
            # ofrecer romper el repositorio.
            if ruta.startswith(".git/") or ruta in vivos:
                continue
            if not _ruta_repo_valida(ruta):
                continue

            try:
                cuenta = _git(self.repo, "rev-list", "--count", "HEAD", "--",
                              ruta, permitir_fallo=True).strip()
                ultimo = _git(self.repo, "log", "-1", "--format=%aI", "--",
                              ruta, permitir_fallo=True).strip()
            except OSError as exc:
                log.warning("huerfanos: no se pudo consultar %s: %s", ruta, exc)
                cuenta, ultimo = "", ""

            carpeta = self.dir_binarios / PurePosixPath(_sin_extension(ruta))
            binarios = len(list(carpeta.glob("*.backup"))) if carpeta.is_dir() else 0

            sueltos.append({
                "ruta": ruta,
                "nombre": PurePosixPath(ruta).stem,
                "empresa_carpeta": str(PurePosixPath(ruta).parent)
                                   if str(PurePosixPath(ruta).parent) != "." else "",
                "versiones": int(cuenta) if cuenta.isdigit() else 0,
                "binarios": binarios,
                "ultimo": ultimo,
            })
        return sueltos

    def _borrar_binarios(self, ruta: str) -> None:
        """Tira la carpeta de binarios del equipo. Best-effort, nunca lanza.

        La carpeta se deriva de la ruta del .rsc igual que en guardar_binario:
        mientras las dos salgan del mismo sitio, no puede quedarse aqui un
        arbol que alli no exista. Y si se quedaran, serian .backup de un equipo
        que ya no esta en ningun sitio: certificados y claves SSH de un cliente
        que pidio que no quedara nada suyo.
        """
        carpeta = self.dir_binarios / PurePosixPath(_sin_extension(ruta))
        if not carpeta.is_dir():
            return
        try:
            shutil.rmtree(carpeta)
        except OSError as exc:
            log.warning("no se pudieron borrar los binarios de %s: %s", ruta, exc)

    def retirar(self, ruta: str) -> bool:
        """Saca el respaldo de un equipo del repositorio, dejando su historia.

        Es un `git rm` y un commit: hoy el archivo ya no esta, pero todas sus
        versiones anteriores siguen en git y se recuperan con un
        `git checkout <commit>^ -- <ruta>`. Es la opcion por defecto justo por
        eso: un borrado que se puede deshacer se puede pulsar sin miedo.

        Devuelve False y deja un log en vez de lanzar, igual que renombrar:
        quien llama es el panel, y esto no puede tumbar una peticion.
        """
        ruta = (ruta or "").strip().replace("\\", "/")
        if not _ruta_repo_valida(ruta):
            log.error("retirar: ruta no valida: %r", ruta)
            return False
        if not (self.repo / ruta).is_file():
            log.error("retirar: no existe %s en el repositorio", ruta)
            return False

        # Mismo formato que guardar y renombrar: ruta real sin extension, con
        # la empresa delante, para que un `git log --oneline` siga diciendo de
        # que cliente es cada linea.
        mensaje = f"Retirado: {_sin_extension(ruta)}"
        try:
            _git(self.repo, "rm", "-q", "--", ruta)
            _git(self.repo, "commit", "-q", "-m", mensaje, "--", ruta)
        except ErrorAlmacen as exc:
            log.error("retirar: no se pudo quitar %s: %s", ruta, exc)
            return False

        log.info("Retirado del repositorio: %s", ruta)
        self._borrar_binarios(ruta)
        return True

    def purgar(self, ruta: str) -> bool:
        """Borra un respaldo de TODA la historia del repositorio. Irreversible.

        Lo otro (retirar) deja las versiones anteriores dentro de git, que es
        lo que casi siempre se quiere. Esto es para cuando lo que se pide es
        que no quede nada: un cliente que se va, o un equipo cuyo export se
        guardo con secretos que no debieron guardarse. Despues de esto no hay
        commit del que sacarlo.

        Reescribir la historia cambia el hash de todos los commits, asi que un
        repositorio ya subido a un remoto queda divergido y el siguiente push
        se rechaza. Es el precio de que el dato desaparezca de verdad.

        Devuelve False y deja un log en vez de lanzar.
        """
        ruta = (ruta or "").strip().replace("\\", "/")
        # CUIDADO: la ruta se interpola dentro de una cadena que git le pasa a
        # un shell (--index-filter es un comando, no una lista de argumentos).
        # Una comilla o un ';' ahi dentro serian una orden mas ejecutandose
        # como el usuario del servicio, que en este despliegue es root. Por eso
        # se valida ANTES -_ruta_repo_valida solo deja rutas relativas de una
        # forma conocida- y ademas se pone entre comillas simples. Las dos
        # cosas, no una: la validacion es la que de verdad cierra la puerta, y
        # las comillas son el cinturon por si algun dia se afloja.
        if not _ruta_repo_valida(ruta) or "'" in ruta:
            log.error("purgar: ruta no valida: %r", ruta)
            return False

        filtro = f"git rm --cached --ignore-unmatch -- '{ruta}'"
        try:
            _git(
                self.repo, "filter-branch", "--force",
                "--index-filter", filtro, "--prune-empty", "--", "--all",
                # filter-branch escupe un aviso de varias lineas recomendando
                # filter-repo. Aqui no se puede usar: seria una dependencia mas
                # que instalar en el servidor para un boton que se pulsa una vez
                # al ano. Silenciarlo es lo que deja legible el log del panel.
                entorno={"FILTER_BRANCH_SQUELCH_WARNING": "1"},
            )
        except ErrorAlmacen as exc:
            log.error("purgar: no se pudo reescribir la historia de %s: %s",
                      ruta, exc)
            return False

        # filter-branch guarda las referencias viejas en refs/original: mientras
        # esten ahi, los commits de antes siguen siendo alcanzables y el archivo
        # NO se ha borrado de nada. La receta de git usa xargs; aqui se hace en
        # Python porque esto se prueba en Windows, donde no hay xargs.
        try:
            viejas = _git(self.repo, "for-each-ref", "--format=%(refname)",
                          "refs/original", permitir_fallo=True)
            for referencia in viejas.split():
                _git(self.repo, "update-ref", "-d", referencia, permitir_fallo=True)
            # Y el reflog es la otra copia: sin expirarlo, el objeto sigue vivo
            # y `git gc` no lo toca.
            _git(self.repo, "reflog", "expire", "--expire=now", "--all",
                 permitir_fallo=True)
            _git(self.repo, "gc", "--prune=now", "--aggressive",
                 permitir_fallo=True)
        except ErrorAlmacen as exc:
            # La historia ya esta reescrita: el archivo no vuelve. Que quede un
            # objeto suelto sin recoger es feo, no es el fallo.
            log.warning("purgar: quedaron restos por recoger en %s: %s", ruta, exc)

        log.info("Purgado de toda la historia: %s", ruta)
        self._borrar_binarios(ruta)
        return True
