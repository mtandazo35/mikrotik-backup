"""Lectura del historial de git: que cambio en cada equipo y cuando.

El repo de configuraciones ya guarda la respuesta a la pregunta que se hace
cualquiera despues de un corte ("que le tocaron a este router anoche?"), pero
esa respuesta hoy solo se saca por consola. Este modulo la deja disponible
para el panel: lista de versiones por equipo, ultimos cambios de la flota,
diff de una version y contenido completo de una version.

Es SOLO LECTURA. No hace init, ni add, ni commit: eso es de store.py, que
corre en el proceso del respaldo. Aqui se ejecuta git desde un servidor web,
posiblemente mientras el respaldo esta commiteando, y las operaciones de
lectura no tocan el index (no hay index.lock que disputar).

Por que se enmascara
--------------------
Con `mostrar_secretos: true` (el valor por defecto) el /export de RouterOS
lleva las credenciales reales de la red: passwords PPPoE, PSK de wireless,
secrets de VPN, community SNMP. El panel se sirve por HTTP y basta un login
para llegar a el; publicar el diff en crudo seria repartir las llaves de toda
la red a cualquiera que consiga una sesion, o a cualquiera que mire la
pantalla por encima del hombro.

La solucion no es ocultar la linea entera, porque entonces el panel deja de
servir para lo unico que se le pide: ver QUE cambio. Se tapa el VALOR y se
conserva el nombre del atributo, asi se sigue leyendo "cambio la PSK de esta
interfaz" sin revelar cual es. Ver enmascarar() para el criterio exacto.

Todo lo que llega de la web (ruta y commit) se valida ANTES de pasarlo a git:
son parametros HTTP escritos por el usuario. Ver _validar_ruta y _validar_commit.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Marca que sustituye a cualquier valor secreto. Las comillas angulares no
# aparecen nunca en un /export de RouterOS, asi que se distingue a simple
# vista de un valor real y no se puede confundir con configuracion.
MARCA = "«oculto»"

# Separadores del formato de `git log`. Se usan bytes de control en vez de
# algo como "|" porque el mensaje de commit lleva la ruta del equipo y puede
# llevar cualquier cosa que alguien escriba a mano en el repo; un separador
# imprimible acabaria apareciendo dentro de un mensaje y partiendo el parseo.
# git no admite NUL ni SOH dentro de un mensaje de commit, asi que estos dos
# no pueden colisionar.
SEP_REGISTRO = "\x00"
SEP_CAMPO = "\x01"

# %h hash corto, %aI fecha de autor en ISO 8601 con zona, %s asunto.
# Se usa %s (asunto, siempre una sola linea) y no %B: el cuerpo multilinea
# obligaria a un parseo mucho mas fragil y el panel muestra una linea.
#
# Los separadores se escriben como %x00/%x01 y NO como el byte literal: en
# Windows, CreateProcess rechaza un argumento con un NUL dentro (ValueError:
# embedded null character). Asi el argumento que sale de aqui es ASCII puro y
# es git quien emite los bytes de control en su salida.
FORMATO = "--format=%x00%h%x01%aI%x01%s"

# Extension de los archivos de configuracion. Se usa para filtrar en cambios():
# el repo tambien versiona su .gitattributes, y ese commit no es "un cambio en
# un router": colarlo en la vista de novedades daria una fila que no lleva
# a ningun equipo.
EXTENSION = ".rsc"

# Un hash, corto o largo. Deliberadamente NO se aceptan revisiones simbolicas
# (HEAD, master@{1}, rev^, rev~2): la web solo navega hashes que ella misma
# saco de versiones(), y aceptar la sintaxis completa de git abriria la puerta
# a expresiones que ni el panel ni quien lo audita saben adonde apuntan.
RE_COMMIT = re.compile(r"^[0-9a-f]{4,40}$")

# Charset de una ruta dentro del repo. Coincide con lo que el proyecto puede
# generar: inventory.NOMBRE_VALIDO y sanear_empresa() solo producen letras,
# numeros, punto, guion y guion bajo, y store.py las une con "/". Al cerrar el
# charset aqui quedan fuera de una vez los comodines de pathspec (*, ?, [ ]),
# los dos puntos de una ruta de Windows y cualquier separador de comandos.
RE_RUTA = re.compile(r"^[A-Za-z0-9._/-]+$")


class ErrorHistorial(Exception):
    """No se pudo leer el historial, o lo que se pidio no es valido."""


@dataclass
class Version:
    commit: str
    fecha: str
    mensaje: str
    lineas_mas: int
    lineas_menos: int
    # Como se llamaba el archivo EN ESE COMMIT. No es lo mismo que como se
    # llama hoy: un equipo renombrado tiene versiones guardadas bajo su nombre
    # anterior, y pedirle a git el contenido de un commit viejo con la ruta de
    # ahora no devuelve nada. Vacia = la de siempre.
    ruta: str = ""


@dataclass
class Linea:
    tipo: str  # "mas" | "menos" | "contexto" | "trozo"
    texto: str
    oculta: bool = False


# --- Enmascarado ------------------------------------------------------------

# Atributos de RouterOS cuyo valor es una credencial. La lista es explicita a
# proposito: tapar "todo lo que suene a clave" con una heuristica llenaria el
# diff de marcas y haria el panel inutil, y dejar de tapar uno solo reparte esa
# credencial. Si aparece un atributo nuevo, se agrega aqui.
ATRIBUTOS_SECRETOS = (
    "password",
    "secret",
    "psk",
    "pre-shared-key",
    "wpa-pre-shared-key",
    "wpa2-pre-shared-key",
    "passphrase",
    "key",
    "private-key",
    "auth-password",
    "enc-password",
    "authentication-password",
    "encryption-password",
    "community",
    "token",
    "shared-secret",
    "eap-password",
    "caller-id-password",
)

# Valores que llevan el nombre de un secreto pero no son una credencial: son
# interruptores. `require-password=yes` no dice cual es la clave, dice que hay
# que pedirla; taparlo solo escondaria informacion util sin proteger nada.
# Tambien entra la cadena vacia: `password=""` no tiene nada que ocultar, y
# marcarla como oculta haria creer que ahi hay una clave puesta.
VALORES_NO_SECRETOS = frozenset(
    {"", "yes", "no", "true", "false", "none", "auto", "default", "disabled", "enabled"}
)

_ATRIBUTOS_ORDENADOS = sorted(ATRIBUTOS_SECRETOS, key=len, reverse=True)

# El corazon del criterio contra falsos positivos, en tres piezas:
#
# 1. (?<![\w-]) : el nombre del atributo tiene que EMPEZAR de verdad ahi. Esto
#    es lo que salva a `public-key=...` y a `ssh-host-key=...` de que el "key"
#    del final los dispare, y lo que impide que `require-password=yes` se
#    confunda con el atributo `password`. Los propios atributos compuestos de
#    la lista (auth-password, pre-shared-key...) siguen entrando porque la
#    alternancia va ordenada de mas largo a mas corto y Python resuelve la
#    alternancia por el primer patron que casa: se prueba `auth-password`
#    antes que `password`.
#
# 2. = pegado, sin espacios : el nombre tiene que ser el lado izquierdo de una
#    asignacion. Esto es lo que salva a `password-strength=medium` (despues de
#    "password" viene un guion, no un igual) y a cualquier mencion del termino
#    dentro de un comentario. RouterOS nunca escribe espacios alrededor del
#    igual en un /export, asi que exigir que este pegado no pierde nada.
#
# 3. el valor : entre comillas (admitiendo comillas escapadas, que es como
#    RouterOS emite una clave con espacios) o sin comillas hasta el primer
#    espacio o el fin de linea.
#
# Nota para quien llame a enmascarar() a mano: hay que pasarle la linea SIN el
# marcador +/- del diff. Con el marcador delante, el guion de `-password=x`
# cae dentro del lookbehind del punto 1 y la linea saldria en claro.
# diferencia() ya quita el marcador antes de enmascarar.
_RE_SECRETO = re.compile(
    r"(?<![\w-])(" + "|".join(_ATRIBUTOS_ORDENADOS) + r")"
    r"(=)"
    r"("
    r'"(?:[^"\\]|\\.)*"'  # valor entre comillas, con escapes
    r"|[^\s]+"  # valor sin comillas, hasta el espacio
    r")",
    re.IGNORECASE,
)


def enmascarar(linea: str) -> tuple[str, bool]:
    """Tapa el valor de los atributos secretos. Devuelve (linea, se_oculto_algo).

    Se conserva el nombre del atributo y se sustituye solo su valor, para que
    quien mire el panel siga viendo QUE cambio sin ver la credencial.

    Consecuencia buscada, y no un defecto: si lo unico que cambio en una linea
    fue el secreto, la version enmascarada del "-" y la del "+" salen
    IDENTICAS. Eso es exactamente lo que se quiere: se ve que la linea cambio
    (sigue habiendo un par -/+ en el diff) sin revelar ni el valor viejo ni el
    nuevo, que es justo el momento en que una clave se filtra: cuando se rota.
    """
    if not linea:
        return linea, False

    oculto = False

    def _sustituir(m: "re.Match[str]") -> str:
        nonlocal oculto
        valor = m.group(3)
        # Se compara el valor sin las comillas: `password="yes"` y
        # `password=yes` son el mismo interruptor.
        desnudo = valor[1:-1] if len(valor) >= 2 and valor.startswith('"') else valor
        if desnudo.lower() in VALORES_NO_SECRETOS:
            return m.group(0)
        oculto = True
        return f"{m.group(1)}={MARCA}"

    return _RE_SECRETO.sub(_sustituir, linea), oculto


def _enmascarar_texto(texto: str) -> str:
    """enmascarar() aplicado linea a linea, conservando los saltos."""
    return "\n".join(enmascarar(l)[0] for l in texto.split("\n"))


# --- Validacion de lo que llega de la web -----------------------------------


def _validar_commit(commit: str) -> str:
    commit = (commit or "").strip()
    if not RE_COMMIT.match(commit):
        raise ErrorHistorial(
            f"commit invalido '{commit}': se espera un hash hexadecimal "
            "de 4 a 40 caracteres"
        )
    return commit


def _validar_ruta(ruta_relativa: str) -> str:
    """Comprueba que la ruta es relativa, literal y no se sale del repo.

    La validacion es lexica (no se resuelve contra el disco) a proposito: se
    consultan versiones de archivos que pueden haber sido BORRADOS y ya no
    existen en el arbol de trabajo, asi que un resolve() rechazaria justo los
    casos que mas interesa poder mirar.
    """
    ruta = (ruta_relativa or "").strip().replace("\\", "/")
    if not ruta:
        raise ErrorHistorial("ruta vacia")

    # Un argumento que empieza por guion lo lee git como opcion, no como ruta.
    # Se comprueba aparte del charset para poder dar el motivo exacto, aunque
    # ademas todas las rutas van siempre despues de "--".
    if ruta.startswith("-"):
        raise ErrorHistorial(f"ruta invalida '{ruta_relativa}': empieza por guion")

    if ruta.startswith("/") or re.match(r"^[A-Za-z]:", ruta):
        raise ErrorHistorial(
            f"ruta invalida '{ruta_relativa}': tiene que ser relativa al repo"
        )

    if not RE_RUTA.match(ruta):
        raise ErrorHistorial(
            f"ruta invalida '{ruta_relativa}': solo letras, numeros, punto, "
            "guion, guion bajo y barra"
        )

    partes = ruta.split("/")
    if any(p in ("", ".", "..") for p in partes):
        raise ErrorHistorial(
            f"ruta invalida '{ruta_relativa}': no puede salirse del repo"
        )

    return ruta


# --- git --------------------------------------------------------------------


def _git(repo, *args: str, permitir_fallo: bool = False) -> str:
    """Ejecuta git y devuelve su salida. Copiado de store.py, con dos cambios.

    Lanza ErrorHistorial en vez de ErrorAlmacen, y captura el OSError de que
    git no este instalado: esto lo llama un servidor web, y ahi la diferencia
    entre un error con mensaje y un traceback es la diferencia entre una
    pagina que explica el problema y un 500.

    Los argumentos van SIEMPRE como lista, nunca shell=True.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        if permitir_fallo:
            return ""
        raise ErrorHistorial(f"no se pudo ejecutar git: {exc}") from exc

    if proc.returncode != 0 and not permitir_fallo:
        raise ErrorHistorial(
            f"git {' '.join(args)} fallo ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout


def _numero(texto: str) -> int:
    """Una celda de --numstat. Los binarios traen '-' en vez de un numero."""
    try:
        return int(texto)
    except ValueError:
        return 0


# git, cuando detecta un renombre, no escribe la ruta a secas en --numstat:
# escribe de donde a donde se movio, comprimiendo la parte comun. Las dos formas
# que emite son:
#
#   carpeta/{viejo.rsc => nuevo.rsc}      (con prefijo o sufijo comun)
#   carpeta/viejo.rsc => otra/nuevo.rsc   (sin nada en comun)
#
# Aqui interesa SIEMPRE el lado derecho: es como se llama el archivo DESPUES de
# ese commit, que es la ruta con la que hay que pedirle a git su contenido en
# ese punto. Quedarse con el izquierdo devolveria el archivo de antes del
# renombrado, o sea la version equivocada, y sin dar ningun error.
_RENOMBRE_LLAVES = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


def _ruta_del_commit(camino: str) -> str:
    """La ruta tal como queda tras ese commit, deshaciendo la notacion de git."""
    camino = camino.strip()
    if "{" in camino and " => " in camino:
        return _RENOMBRE_LLAVES.sub(lambda m: m.group(2), camino)
    if " => " in camino:
        return camino.split(" => ", 1)[1].strip()
    return camino


def _registros(salida: str):
    """Trocea la salida de `git log FORMATO --numstat` en (Version, archivos).

    'archivos' es la lista de (ruta, lineas_mas, lineas_menos) del commit; los
    contadores de la Version quedan a cero y los pone quien llama, que es el
    unico que sabe si suma un archivo o todos.
    """
    for bruto in salida.split(SEP_REGISTRO):
        bruto = bruto.strip("\n")
        if not bruto:
            continue

        lineas = bruto.split("\n")
        cabecera = lineas[0].split(SEP_CAMPO)
        if len(cabecera) < 3:
            continue  # registro truncado: mejor saltarlo que reventar

        version = Version(
            commit=cabecera[0],
            fecha=cabecera[1],
            # Por si algun dia el formato lleva mas campos: el mensaje es todo
            # lo que quede a la derecha, no solo el tercer trozo.
            mensaje=SEP_CAMPO.join(cabecera[2:]),
            lineas_mas=0,
            lineas_menos=0,
        )

        archivos: list[tuple[str, int, int]] = []
        for linea in lineas[1:]:
            if not linea.strip():
                continue
            partes = linea.split("\t")
            if len(partes) < 3:
                continue
            archivos.append((partes[2], _numero(partes[0]), _numero(partes[1])))

        yield version, archivos


# --- API --------------------------------------------------------------------


def versiones(repo, ruta_relativa: str, maximo: int = 50) -> list[Version]:
    """Versiones de UN archivo, de la mas reciente a la mas antigua.

    Devuelve lista vacia si el repo no existe todavia, si no tiene commits o
    si ese archivo nunca se respaldo: para el panel esas tres situaciones son
    la misma ("aun no hay historial que ensenar") y ninguna es un error.
    """
    ruta = _validar_ruta(ruta_relativa)
    maximo = max(1, int(maximo))

    salida = _git(
        repo,
        "log",
        "--no-color",
        # --follow y no --no-renames. Este es EL detalle del renombrado: sin
        # seguir el archivo a traves de sus cambios de nombre, el panel ensena
        # una sola version (la del propio renombrado) de un equipo que llevaba
        # meses respaldandose, y quien lo mira concluye, con toda la razon, que
        # renombrar borra el historial. En git no se pierde nada; lo que se
        # perdia era la forma de verlo, que para quien lo usa es lo mismo.
        #
        # Se le quito por un motivo REAL: con deteccion de renombres, numstat
        # emite la ruta como "carpeta/{viejo.rsc => nuevo.rsc}", que no sirve
        # para volver a pedirle el archivo a git. La solucion no era renunciar
        # al historial, sino entender esa notacion (ver _ruta_del_commit).
        "--follow",
        f"--max-count={maximo}",
        "--numstat",
        FORMATO,
        "--",
        ruta,
        permitir_fallo=True,
    )

    resultado: list[Version] = []
    for version, archivos in _registros(salida):
        # El log ya viene filtrado por la ruta, pero un commit de merge o un
        # cambio de modo pueden no traer numstat: la version se emite igual,
        # con los contadores a cero, porque existir existe.
        for camino, mas, menos in archivos:
            version.lineas_mas += mas
            version.lineas_menos += menos
            if not version.ruta:
                version.ruta = _ruta_del_commit(camino)
        resultado.append(version)

    return resultado


def cambios(repo, maximo: int = 50) -> list[tuple[str, Version]]:
    """Ultimos cambios de TODA la flota, del mas reciente al mas antiguo.

    Un par (ruta_relativa, Version) por archivo tocado. Hoy store.py commitea
    un archivo por commit (`git commit -- <ruta>`), asi que sale un par por
    commit; no se da por hecho, porque un commit hecho a mano en el repo o una
    futura agrupacion de altas romperia esa suposicion en silencio y la vista
    perderia cambios sin avisar.
    """
    maximo = max(1, int(maximo))

    salida = _git(
        repo,
        "log",
        "--no-color",
        "--no-renames",
        f"--max-count={maximo}",
        "--numstat",
        FORMATO,
        permitir_fallo=True,
    )

    resultado: list[tuple[str, Version]] = []
    for version, archivos in _registros(salida):
        for ruta, mas, menos in archivos:
            # Solo configuraciones de equipos: el repo tambien versiona su
            # .gitattributes, y esa fila no lleva a ningun router.
            if not ruta.endswith(EXTENSION):
                continue
            resultado.append(
                (
                    ruta,
                    Version(
                        commit=version.commit,
                        fecha=version.fecha,
                        mensaje=version.mensaje,
                        lineas_mas=mas,
                        lineas_menos=menos,
                    ),
                )
            )

    # El tope se aplica tambien a los pares: `maximo` es lo que la vista puede
    # pintar, y un commit que tocara 300 archivos no debe desbordarla.
    return resultado[:maximo]


def diferencia(
    repo, ruta_relativa: str, commit: str, ocultar_secretos: bool = True
) -> list[Linea]:
    """Diff de un commit para un archivo, ya troceado para pintarlo.

    El texto de cada Linea va SIN el marcador +/- del diff: el tipo ya dice si
    es alta, baja o contexto, y dejar el marcador obligaria a la plantilla a
    recortarlo. Las cabeceras de git (diff --git, index, ---, +++, modos,
    renombres) se descartan enteras: no le dicen nada a quien mira el panel.

    Devuelve lista vacia si ese commit no toco ese archivo.
    """
    ruta = _validar_ruta(ruta_relativa)
    revision = _validar_commit(commit)

    salida = _git(
        repo,
        "show",
        "--no-color",
        "--no-renames",
        "--format=",  # sin cabecera de commit: eso ya lo da versiones()
        revision,
        "--",
        ruta,
        permitir_fallo=True,
    )

    lineas: list[Linea] = []
    for cruda in salida.split("\n"):
        if cruda.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        if cruda.startswith(
            ("old mode", "new mode", "new file mode", "deleted file mode",
             "similarity index", "dissimilarity index", "rename from",
             "rename to", "copy from", "copy to", "Binary files", "\\ No newline")
        ):
            continue

        if cruda.startswith("@@"):
            tipo, texto = "trozo", cruda
        elif cruda.startswith("+"):
            tipo, texto = "mas", cruda[1:]
        elif cruda.startswith("-"):
            tipo, texto = "menos", cruda[1:]
        elif cruda.startswith(" "):
            tipo, texto = "contexto", cruda[1:]
        else:
            # Linea vacia: la que git deja al final de la salida, o la que
            # separa la cabecera del commit del diff. No aporta nada.
            continue

        if ocultar_secretos and tipo != "trozo":
            # Se enmascara DESPUES de quitar el marcador +/-: con el guion
            # delante, el lookbehind del patron daria la linea por buena y el
            # secreto saldria en claro. Ver el comentario de _RE_SECRETO.
            texto, oculta = enmascarar(texto)
        else:
            oculta = False

        lineas.append(Linea(tipo=tipo, texto=texto, oculta=oculta))

    return lineas


def contenido(
    repo, ruta_relativa: str, commit: str, ocultar_secretos: bool = True
) -> str:
    """El archivo completo tal como quedo en esa version.

    Aqui SI se lanza ErrorHistorial si el commit o la ruta no existen: a
    diferencia de un listado vacio, "ensename esta version" es una peticion
    concreta y responder con un cuadro en blanco haria pensar que el equipo
    tenia la configuracion vacia.
    """
    ruta = _validar_ruta(ruta_relativa)
    revision = _validar_commit(commit)

    # `git show <rev>:<ruta>` no admite "--", pero no hace falta: el argumento
    # empieza por el hash, que ya se valido como hexadecimal, asi que no puede
    # confundirse con una opcion. La ruta va validada por la misma funcion que
    # el resto y no puede salir del repo.
    texto = _git(repo, "show", f"{revision}:{ruta}", permitir_fallo=True)

    if not texto:
        # Puede ser un archivo realmente vacio, pero lo normal con diferencia
        # es que el commit o la ruta no existan; comprobarlo aparte costaria
        # otro proceso de git para distinguir un caso que no se da.
        raise ErrorHistorial(
            f"no hay contenido para '{ruta_relativa}' en el commit {revision}"
        )

    if ocultar_secretos:
        texto = _enmascarar_texto(texto)

    return texto


def existe_repo(repo) -> bool:
    """Si hay un repo git utilizable. La web lo usa para no ofrecer el enlace."""
    try:
        return (Path(repo) / ".git").is_dir()
    except (OSError, TypeError):
        return False
