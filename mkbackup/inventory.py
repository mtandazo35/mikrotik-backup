"""Lectura y escritura del inventario de equipos.

Formato CSV con cabecera:
    nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave

CUIDADO: este archivo puede contener CONTRASENAS EN CLARO. Las columnas
'usuario' y 'clave' son opcionales y sirven para los equipos que no usan las
credenciales generales de config.yaml, y se guardan tal cual, sin cifrar. Por
eso el inventario vive en /root y guardar() lo deja en 0600: nadie mas que el
usuario del servicio tiene por que leerlo. Y por eso mismo cualquier COPIA se
lleva las claves de toda la red: un scp a un portatil, un adjunto en un correo,
un volcado de respaldo del servidor o un git add sin mirar exponen todos los
routers a la vez. Si hay que mover el archivo, trata la copia como trataria a
la contrasena del router mas critico, y borrala cuando termines.

Las columnas 'empresa', 'intervalo_minutos', 'usuario' y 'clave' son opcionales
al leer: los inventarios escritos antes de que existieran siguen cargando, y
esas filas quedan con la empresa por defecto, con el intervalo global y con las
credenciales generales.
Al escribir siempre se emite la cabecera completa (ver CABECERA).

Se tolera lo que sale de Excel en Windows: CRLF, espacios sobrantes, columnas
opcionales vacias. Un inventario a medio llenar no debe tumbar la ejecucion:
las lineas malas se reportan y se saltan.

El modulo tambien ESCRIBE porque el panel web da de alta y edita equipos desde
un formulario. Esa parte no es tolerante: si el alta no se pudo guardar, la
persona que la hizo tiene que enterarse (ver guardar y validar_equipo).
"""

from __future__ import annotations

import csv
import ipaddress
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Se usa como nombre de archivo y de directorio dentro del repo git.
NOMBRE_VALIDO = re.compile(r"^[A-Za-z0-9._-]+$")

# Nombre DNS razonable: etiquetas alfanumericas separadas por puntos, con
# guiones por dentro. No pretende ser el RFC entero, solo cazar la basura.
DNS_VALIDO = re.compile(
    r"^(?=.{1,253}$)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)

# Empresa de las filas que no declaran ninguna. Vale a la vez como nombre
# legible y como slug, asi no hay que tratarla aparte.
EMPRESA_DEFECTO = "sin-empresa"

# Excel (y LibreOffice) interpretan como FORMULA cualquier celda que empiece
# por uno de estos. Este inventario esta pensado para ir y venir en Excel, asi
# que un nombre de empresa como =cmd|' /C calc'!A0 seria codigo esperando a que
# alguien abra el archivo. Se rechaza al escribirlo en vez de intentar
# neutralizarlo despues: un cliente real no se llama asi, y ensuciar el valor
# con una comilla lo estropearia al reimportarlo.
INICIO_FORMULA = ("=", "+", "-", "@")

GRUPO_DEFECTO = "mikrotiks"

# Orden de las columnas al escribir. La empresa va segunda porque es lo que
# primero busca quien abre el CSV a mano: agrupa la lista. El intervalo va al
# final porque casi siempre esta vacio y estorbaria entre los datos de red, y
# usuario/clave detras de el por lo mismo: son la excepcion, no la norma.
CABECERA = ["nombre", "empresa", "ip", "puerto", "grupo", "intervalo_minutos",
            "usuario", "clave"]

# Puertos que casi siempre son un error de dedo: son de Winbox / API, no SSH.
PUERTOS_SOSPECHOSOS = {8291: "Winbox", 8728: "API", 8729: "API-SSL"}

# Por debajo de esto se avisa: abrir una sesion SSH a un RouterOS y pedirle un
# export completo no es gratis, y repetirlo cada pocos minutos se nota en un
# equipo cargado (CPU y sesiones ssh abiertas). No se prohibe: es su red.
INTERVALO_AGRESIVO = 5


@dataclass(frozen=True)
class Equipo:
    nombre: str
    ip: str
    puerto: int = 22
    grupo: str = GRUPO_DEFECTO
    empresa: str = EMPRESA_DEFECTO
    # Cada cuanto se consulta ESTE equipo. 0 = heredar el intervalo global
    # (planificador.intervalo_minutos): hay routers que no cambian en meses y
    # no tienen por que preguntarse al ritmo de un core.
    #
    # Se usa 0 y no None a proposito: el CSV no sabe distinguir una celda vacia
    # de un None (todo llega como cadena), asi que None obligaria a inventar
    # una convencion de texto para escribirlo y a leerla de vuelta. Con un
    # entero, la comparacion ("if equipo.intervalo_minutos:") no tiene sorpresas
    # y el valor viaja al CSV y vuelve siendo el mismo.
    intervalo_minutos: int = 0

    # Credenciales SSH propias de ESTE equipo. Vacias = usar las generales de
    # config.yaml (ssh.usuario / ssh.password), que es el caso normal: solo se
    # llenan en los routers que no comparten el usuario de gestion (un cliente
    # que no deja crear usuarios, un equipo heredado con su propia clave).
    #
    # Se guardan EN CLARO en el inventario: es una decision consciente (ver el
    # docstring del modulo). Por eso guardar() deja el archivo en 0600.
    #
    # Cadena vacia y no None por lo mismo que el intervalo: el CSV solo sabe de
    # texto y un None obligaria a inventar una convencion para escribirlo.
    usuario: str = ""
    clave: str = ""

    @property
    def carpeta_empresa(self) -> str:
        """Slug de la empresa, tal como aparece en las rutas del repo."""
        return sanear_empresa(self.empresa)

    @property
    def ruta_relativa(self) -> str:
        """Ruta dentro del repo: <carpeta_empresa>/<nombre>.rsc

        El GRUPO ya no entra en la ruta. Es una decision del usuario: quiere
        abrir la carpeta de un cliente y ver ahi dentro TODOS sus respaldos, sin
        tener que entrar en una subcarpeta por grupo para encontrar un router
        del que no recuerda si lo clasifico como 'core' o como 'bts'.

        El campo 'grupo' se mantiene en el inventario y en el CSV: sigue
        sirviendo para clasificar y para filtrar en el panel. Lo unico que
        cambia es que ya no decide donde se guarda el archivo, asi que cambiarle
        el grupo a un equipo tampoco mueve su historial en git.
        """
        return f"{self.carpeta_empresa}/{self.nombre}.rsc"

    def __str__(self) -> str:
        return f"{self.nombre} ({self.ip}:{self.puerto})"


class ErrorInventario(Exception):
    """El inventario no se pudo leer o no se pudo guardar."""


def sanear_empresa(texto: str) -> str:
    """Convierte 'Acme S.A.' en un slug apto para carpeta: 'acme-s.a'.

    Existe porque el nombre de la empresa se guarda y se muestra tal cual lo
    escribe una persona ("Telecomunicaciones Andinas S.A.", con espacios,
    tildes y puntos), pero ese texto no puede viajar a una ruta del repo: los
    espacios complican los comandos de git, las tildes se codifican distinto
    segun el sistema de archivos (NFC en Linux, NFD en macOS) y el mismo
    respaldo acabaria en dos carpetas distintas segun quien clone el repo.

    Se pasa todo a minusculas a proposito: Windows y macOS no distinguen
    mayusculas en las rutas, asi que 'Acme SA' y 'acme sa' serian la misma
    carpeta en unos equipos y dos en otros. Mejor que sean siempre una, y que
    cargar() avise cuando dos nombres legibles distintos caen en el mismo slug.
    """
    # NFKD separa la letra de su tilde; descartar los combinantes deja solo la
    # letra base (la ene con virgulilla queda en n) sin mantener ninguna tabla.
    base = unicodedata.normalize("NFKD", (texto or "").strip())
    base = "".join(c for c in base if not unicodedata.combining(c))
    base = base.lower()

    # Cualquier separador (espacios, barras, dos puntos) pasa a guion; asi
    # "Acme / Sur" no se convierte en "acmesur" y pierde la frontera.
    base = re.sub(r"[\s/\\:,;|]+", "-", base)
    base = re.sub(r"[^a-z0-9._-]", "", base)
    base = re.sub(r"-{2,}", "-", base)
    # Se recortan tambien puntos: Windows borra los puntos finales de un
    # directorio, y ademas '.' o '..' como carpeta no significa lo que parece.
    base = base.strip("-.")

    return base or EMPRESA_DEFECTO


def cargar(ruta: str | Path, puerto_defecto: int = 22) -> tuple[list[Equipo], list[str]]:
    """Devuelve (equipos, avisos).

    Los avisos describen lineas descartadas o corregidas; conviene mostrarlos
    para que un typo no pase inadvertido en un inventario de 300 lineas.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorInventario(f"no existe el inventario: {ruta}")

    equipos: list[Equipo] = []
    avisos: list[str] = []
    # Filas de datos que decian ALGO: ni vacias ni comentarios. Sirve para poder
    # distinguir "el archivo esta vacio" de "el archivo tiene filas y ninguna
    # vale", que son dos situaciones muy distintas (ver el final de la funcion).
    filas_leidas = 0
    vistos: dict[str, str] = {}
    # slug de empresa -> primer nombre legible que lo produjo, para detectar
    # dos empresas distintas que terminarian compartiendo carpeta.
    slugs: dict[str, str] = {}
    choques_avisados: set[tuple[str, str]] = set()

    # newline="" es lo que pide csv; utf-8-sig se come el BOM que mete Excel.
    with ruta.open("r", encoding="utf-8-sig", newline="") as fh:
        lector = csv.DictReader(fh)
        if not lector.fieldnames:
            raise ErrorInventario(f"{ruta} esta vacio o no tiene cabecera")

        columnas = {(c or "").strip().lower() for c in lector.fieldnames}
        faltan = {"nombre", "ip"} - columnas
        if faltan:
            raise ErrorInventario(
                f"{ruta}: faltan columnas obligatorias: {', '.join(sorted(faltan))}. "
                "Cabecera esperada: " + ",".join(CABECERA) + " (solo nombre e ip "
                "son obligatorias)"
            )

        for n, fila in enumerate(lector, start=2):  # 2 = primera fila de datos
            # 'crudo' conserva el valor tal cual venia; 'limpia' es el recortado
            # que usa casi todo. Hacen falta los dos porque la contrasena es el
            # unico campo que NO se puede recortar (ver mas abajo).
            crudo = {(k or "").strip().lower(): (v or "") for k, v in fila.items()}
            limpia = {k: v.strip() for k, v in crudo.items()}

            nombre = limpia.get("nombre", "")
            ip = limpia.get("ip", "")

            if not nombre and not ip:
                continue  # fila vacia
            if nombre.startswith("#"):
                continue  # comentario

            filas_leidas += 1

            if not nombre or not ip:
                avisos.append(f"L{n}: descartada, falta nombre o ip")
                continue

            original = nombre
            nombre = nombre.replace(" ", "")
            if nombre != original:
                avisos.append(f"L{n}: '{original}' tenia espacios, queda como '{nombre}'")

            if not NOMBRE_VALIDO.match(nombre):
                avisos.append(
                    f"L{n}: descartada, nombre invalido '{nombre}' "
                    "(solo letras, numeros, punto, guion y guion bajo)"
                )
                continue

            if nombre in vistos:
                avisos.append(
                    f"L{n}: descartada, nombre duplicado '{nombre}' "
                    f"(ya usado por {vistos[nombre]}); se pisarian los respaldos"
                )
                continue

            puerto_txt = limpia.get("puerto", "") or str(puerto_defecto)
            try:
                puerto = int(puerto_txt)
            except ValueError:
                avisos.append(f"L{n}: descartada, puerto no numerico '{puerto_txt}'")
                continue
            if not 1 <= puerto <= 65535:
                avisos.append(f"L{n}: descartada, puerto fuera de rango '{puerto}'")
                continue

            # Error frecuente: apuntar a Winbox o a la API en vez de a SSH.
            if puerto in PUERTOS_SOSPECHOSOS:
                servicio = PUERTOS_SOSPECHOSOS[puerto]
                avisos.append(
                    f"L{n}: '{nombre}' apunta al puerto {puerto} ({servicio}); "
                    "el respaldo va por SSH y ahi no va a responder"
                )

            grupo = limpia.get("grupo", "") or GRUPO_DEFECTO
            if not NOMBRE_VALIDO.match(grupo):
                avisos.append(f"L{n}: grupo invalido '{grupo}', se usa '{GRUPO_DEFECTO}'")
                grupo = GRUPO_DEFECTO

            # La columna empresa es opcional: sin ella el inventario sigue
            # siendo valido y todo cae en la carpeta por defecto.
            empresa = limpia.get("empresa", "") or EMPRESA_DEFECTO
            slug = sanear_empresa(empresa)
            if slug == EMPRESA_DEFECTO and empresa != EMPRESA_DEFECTO:
                avisos.append(
                    f"L{n}: la empresa '{empresa}' no deja ningun caracter usable "
                    f"para la carpeta, se usa '{EMPRESA_DEFECTO}'"
                )
                empresa = EMPRESA_DEFECTO

            # Dos nombres legibles distintos con el mismo slug mezclarian los
            # respaldos de dos clientes en una sola carpeta del repo. No se
            # descarta la fila (los datos son validos), pero hay que corregirlo.
            anterior = slugs.setdefault(slug, empresa)
            if anterior != empresa and (anterior, empresa) not in choques_avisados:
                choques_avisados.add((anterior, empresa))
                avisos.append(
                    f"L{n}: las empresas '{anterior}' y '{empresa}' comparten la "
                    f"carpeta '{slug}'; sus respaldos quedarian mezclados, "
                    "unifica el nombre en el inventario"
                )

            # La columna intervalo_minutos tambien es opcional: si falta, si
            # esta vacia o si trae basura, el equipo se consulta al ritmo
            # global. Aqui se degrada en vez de descartar la fila: perder un
            # router del respaldo por un intervalo mal escrito seria mucho peor
            # que consultarlo con el intervalo de siempre.
            intervalo_txt = limpia.get("intervalo_minutos", "")
            intervalo = 0
            if intervalo_txt:
                try:
                    intervalo = int(intervalo_txt)
                except ValueError:
                    avisos.append(
                        f"L{n}: '{nombre}' tiene un intervalo no numerico "
                        f"'{intervalo_txt}'; se usa el intervalo global"
                    )
                    intervalo = 0
                else:
                    if intervalo < 0:
                        avisos.append(
                            f"L{n}: '{nombre}' tiene un intervalo negativo "
                            f"'{intervalo}'; se usa el intervalo global"
                        )
                        intervalo = 0
                    # El 0 escrito a mano no es agresivo: es "usa el global".
                    elif 0 < intervalo < INTERVALO_AGRESIVO:
                        avisos.append(
                            f"L{n}: '{nombre}' se consultaria cada {intervalo} "
                            f"min; por debajo de {INTERVALO_AGRESIVO} min cada "
                            "respaldo abre una sesion SSH y pide un export "
                            "completo, y en un equipo cargado se nota"
                        )

            # Credenciales propias del equipo. Vacias es lo NORMAL: significa
            # "usa las generales de config.yaml" y no es ningun error.
            #
            # El usuario si se recorta: un ' admin' con un espacio delante es
            # siempre un copiar y pegar mal, no hay usuario de RouterOS que
            # empiece por espacio.
            usuario = limpia.get("usuario", "")
            # La CLAVE no se recorta NUNCA, ni siquiera si son todo espacios: un
            # espacio final puede formar parte de la contrasena de verdad, y
            # "arreglarsela" a quien la escribio provoca un fallo de
            # autenticacion que no hay forma de diagnosticar mirando el CSV (la
            # celda se ve igual). Si sobra un espacio, que se note al conectar y
            # no que lo borremos a sus espaldas.
            clave = crudo.get("clave", "")

            # Rellenar solo una de las dos casi siempre es un descuido al llenar
            # el Excel, y el resultado es una mezcla silenciosa: usuario del
            # equipo con clave general, o al reves. La fila NO se descarta
            # (puede ser a proposito), pero hay que decirlo.
            if usuario and not clave:
                avisos.append(
                    f"L{n}: '{nombre}' trae usuario propio pero no clave; se "
                    "usara ese usuario con la clave general de config.yaml"
                )
            elif clave and not usuario:
                avisos.append(
                    f"L{n}: '{nombre}' trae clave propia pero no usuario; se "
                    "usara el usuario general de config.yaml con esa clave"
                )

            vistos[nombre] = ip
            equipos.append(
                Equipo(
                    nombre=nombre,
                    ip=ip,
                    puerto=puerto,
                    grupo=grupo,
                    empresa=empresa,
                    intervalo_minutos=intervalo,
                    usuario=usuario,
                    clave=clave,
                )
            )

    # Un inventario VACIO no es un error, y confundir las dos cosas se veia en
    # pantalla: al dar de baja el ultimo equipo, el panel sacaba un recuadro de
    # "avisos del inventario" diciendo que el archivo no contiene ningun equipo
    # valido, como si estuviera roto. Estaba vacio, que es exactamente lo que
    # deja quien acaba de instalar esto o quien acaba de borrar su ultimo
    # router. Un aviso que sale cuando no pasa nada ensena a no leer los avisos.
    #
    # Lo que SI es un problema es un archivo CON filas de las que no se salva
    # ninguna: ahi hay 40 lineas escritas y ni una sirve, y eso hay que decirlo
    # y parar. La diferencia esta en filas_leidas, que no cuenta ni las lineas
    # en blanco ni los comentarios.
    if not equipos and filas_leidas:
        raise ErrorInventario(f"{ruta} no contiene ningun equipo valido")

    return equipos, avisos


def guardar(ruta: str | Path, equipos: list[Equipo]) -> None:
    """Reescribe el inventario completo con la cabecera CABECERA.

    Escritura atomica (temporal en la misma carpeta + os.replace): el proceso
    de respaldo puede estar leyendo el inventario justo cuando el panel lo
    reescribe, y no debe encontrarse nunca un CSV a medias. El temporal va en
    la misma carpeta porque os.replace solo es atomico dentro del mismo
    sistema de archivos.

    A diferencia del estado, aqui un fallo de escritura SI se propaga: si el
    alta no se guardo, quien la hizo tiene que enterarse en vez de creer que
    el equipo ya esta en el inventario.

    El archivo queda en 0600 porque puede llevar contrasenas en claro en la
    columna 'clave' (ver el docstring del modulo).
    """
    ruta = Path(ruta)
    temporal = ""
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        # newline="" lo exige el modulo csv; sin ello Windows duplicaria el
        # retorno de carro y quedaria una linea en blanco entre filas.
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=ruta.parent,
            prefix=ruta.name + ".",
            suffix=".tmp",
            delete=False,
        ) as fh:
            escritor = csv.writer(fh)
            escritor.writerow(CABECERA)
            for equipo in equipos:
                escritor.writerow(
                    [
                        equipo.nombre,
                        equipo.empresa,
                        equipo.ip,
                        equipo.puerto,
                        equipo.grupo,
                        # Se escribe tal cual, 0 incluido: al releer vuelve a
                        # significar "usa el intervalo global" y el ciclo
                        # cargar -> guardar -> cargar no cambia nada.
                        equipo.intervalo_minutos,
                        equipo.usuario,
                        # Tal cual, sin recortar: si la contrasena termina en
                        # espacio, el csv la entrecomilla sola y vuelve igual.
                        equipo.clave,
                    ]
                )
            fh.flush()
            # Sin fsync, un corte de luz justo despues del replace puede dejar
            # el archivo nuevo con contenido vacio: el rename llega al disco
            # antes que los datos.
            os.fsync(fh.fileno())
            temporal = fh.name

        # El inventario puede llevar contrasenas en claro, asi que solo su
        # dueno debe poder leerlo. Se hace sobre el TEMPORAL y ANTES del
        # replace: si se hiciera despues, el archivo definitivo existiria un
        # instante con los permisos por defecto (0644 con el umask habitual) y
        # en ese hueco cualquiera del sistema podria copiarselo.
        try:
            os.chmod(temporal, 0o600)
        except OSError:
            # En Windows chmod es casi simbolico (solo toca el bit de solo
            # lectura) y puede fallar segun el sistema de archivos. No es
            # motivo para perder el alta: alli el archivo no vive en /root y
            # los permisos reales los pone la ACL de la carpeta. Donde esto
            # importa de verdad, que es el Debian del servicio, funciona.
            pass

        os.replace(temporal, ruta)
    except OSError as exc:
        if temporal:
            try:
                os.unlink(temporal)
            except OSError:
                pass
        raise ErrorInventario(f"no se pudo guardar el inventario {ruta}: {exc}") from exc


def _ip_o_dns_valido(texto: str) -> bool:
    """IPv4/IPv6 literal, o un nombre DNS con pinta de serlo."""
    try:
        ipaddress.ip_address(texto)
        return True
    except ValueError:
        pass

    # Si solo tiene digitos, puntos o dos puntos es que queria ser una IP y no
    # lo es ('10.0.0.999'). Dejarlo pasar como DNS solo aplazaria el error
    # hasta la hora del respaldo, de madrugada y sin nadie delante.
    if re.fullmatch(r"[0-9.]+", texto) or ":" in texto:
        return False

    return bool(DNS_VALIDO.match(texto))


def validar_equipo(
    nombre: str,
    empresa: str,
    ip: str,
    puerto: str | int | None,
    grupo: str,
    existentes: Iterable[str],
    original: str = "",
    intervalo: str | int | None = "",
    usuario: str = "",
    clave: str = "",
) -> tuple[Equipo | None, list[str]]:
    """Valida los datos de un alta o edicion del panel. Devuelve (equipo, errores).

    Si hay errores, el equipo es None: no se devuelve nada a medio validar.
    Los mensajes van dirigidos a la persona que llena el formulario.

    Aqui se rechaza lo que cargar() solo avisa (un grupo invalido, por
    ejemplo). Es a proposito: hay alguien delante que puede corregirlo en el
    momento, mientras que cargar() corre de madrugada y prefiere degradar
    antes que perder un equipo del respaldo.
    """
    errores: list[str] = []

    nombre = (nombre or "").strip()
    empresa = (empresa or "").strip()
    ip = (ip or "").strip()
    grupo = (grupo or "").strip() or GRUPO_DEFECTO
    original = (original or "").strip()
    # Mismo criterio que cargar(): el usuario se recorta (un espacio delante es
    # un pegado mal) y la clave NO se toca jamas, porque el espacio puede ser
    # parte de la contrasena y quitarselo daria un fallo de autenticacion
    # imposible de ver en el formulario.
    usuario = (usuario or "").strip()
    clave = clave or ""

    # --- nombre -------------------------------------------------------------
    if not nombre:
        errores.append("el nombre es obligatorio")
    elif " " in nombre:
        errores.append("el nombre no puede llevar espacios")
    elif not NOMBRE_VALIDO.match(nombre):
        errores.append(
            "nombre invalido: solo letras, numeros, punto, guion y guion bajo"
        )
    elif nombre in set(existentes or ()) and nombre != original:
        # 'original' es el nombre que tenia el equipo antes de editarlo: sin
        # esta salvedad, guardar una edicion sin tocar el nombre se rechazaria
        # a si misma por duplicada.
        errores.append(
            f"ya existe un equipo llamado '{nombre}'; se pisarian los respaldos"
        )

    # --- empresa ------------------------------------------------------------
    if not empresa:
        errores.append("la empresa es obligatoria")
    elif sanear_empresa(empresa) == EMPRESA_DEFECTO and empresa != EMPRESA_DEFECTO:
        errores.append(
            f"la empresa '{empresa}' no deja ningun caracter usable para la "
            "carpeta: usa letras o numeros"
        )
    elif empresa[0] in INICIO_FORMULA:
        errores.append(
            "el nombre de la empresa no puede empezar por "
            f"{' '.join(INICIO_FORMULA)}: el inventario se abre en Excel y ahi "
            "eso se ejecuta como formula"
        )

    # --- ip -----------------------------------------------------------------
    if not ip:
        errores.append("la ip o el nombre DNS es obligatorio")
    elif not _ip_o_dns_valido(ip):
        errores.append(f"'{ip}' no es una IP ni un nombre DNS valido")

    # --- puerto -------------------------------------------------------------
    # Llega como texto del formulario y puede venir vacio: eso es el 22.
    puerto_txt = str(puerto if puerto is not None else "").strip() or "22"
    numero = 22
    try:
        numero = int(puerto_txt)
    except ValueError:
        errores.append(f"el puerto '{puerto_txt}' no es un numero")
    else:
        if not 1 <= numero <= 65535:
            errores.append("el puerto debe estar entre 1 y 65535")
        # 8291/8728/8729 (Winbox/API) no son error: hay instalaciones que
        # redirigen SSH ahi. El aviso ya lo da cargar() al leer el inventario.

    # --- grupo --------------------------------------------------------------
    if not NOMBRE_VALIDO.match(grupo):
        errores.append(
            f"grupo invalido '{grupo}': solo letras, numeros, punto, guion y "
            "guion bajo"
        )

    # --- intervalo ----------------------------------------------------------
    # Llega como texto del formulario o de una celda de Excel, y venir vacio es
    # lo normal: significa "el intervalo general del sistema". Eso NO es error.
    # Lo que si se rechaza (a diferencia de cargar(), que degrada) es un valor
    # escrito mal: hay alguien delante que puede corregirlo ahora mismo, y
    # aceptarlo en silencio le haria creer que su router se consulta cada hora
    # cuando en realidad seguiria con el intervalo global.
    intervalo_txt = str(intervalo if intervalo is not None else "").strip()
    minutos = 0
    if intervalo_txt:
        try:
            minutos = int(intervalo_txt)
        except ValueError:
            errores.append(
                f"el intervalo '{intervalo_txt}' no es un numero de minutos "
                "(dejalo vacio para usar el intervalo general)"
            )
        else:
            if minutos < 0:
                errores.append(
                    "el intervalo no puede ser negativo; dejalo vacio o en 0 "
                    "para usar el intervalo general"
                )

    # --- credenciales -------------------------------------------------------
    # No se valida nada: usuario y clave son OPCIONALES y dejarlos vacios es lo
    # normal (se heredan los de config.yaml). Tampoco se exige llenarlos a la
    # vez: llenar solo uno es raro pero puede ser a proposito (usuario propio
    # con la clave de siempre), y cargar() ya avisa de esa mezcla al releer el
    # inventario. Cualquier regla mas dura aqui bloquearia un alta legitima.

    if errores:
        return None, errores

    return (
        Equipo(
            nombre=nombre,
            ip=ip,
            puerto=numero,
            grupo=grupo,
            empresa=empresa,
            intervalo_minutos=minutos,
            usuario=usuario,
            clave=clave,
        ),
        [],
    )
