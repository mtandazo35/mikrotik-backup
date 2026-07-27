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

import logging
import os
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


@dataclass
class Guardado:
    equipo: Equipo
    cambio: bool
    commit: str = ""
    binario_guardado: bool = False


def _git(repo: Path, *args: str, permitir_fallo: bool = False) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 and not permitir_fallo:
        raise ErrorAlmacen(
            f"git {' '.join(args)} fallo ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
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

    def replicar(self) -> str:
        """Empuja al remoto configurado. Devuelve texto descriptivo."""
        if not self.cfg.almacen.remoto:
            return "sin remoto configurado"

        actual = _git(self.repo, "remote", "get-url", "origin", permitir_fallo=True).strip()
        if not actual:
            _git(self.repo, "remote", "add", "origin", self.cfg.almacen.remoto)
        elif actual != self.cfg.almacen.remoto:
            _git(self.repo, "remote", "set-url", "origin", self.cfg.almacen.remoto)

        rama = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        _git(self.repo, "push", "-q", "origin", rama)
        return f"replicado a {self.cfg.almacen.remoto} ({rama})"

    # --- Consulta -----------------------------------------------------------

    def equipos_sin_respaldo(self, equipos: list[Equipo]) -> list[Equipo]:
        """Equipos del inventario que nunca se han respaldado con exito."""
        return [e for e in equipos if not (self.repo / e.ruta_relativa).is_file()]
