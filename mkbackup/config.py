"""Carga y validacion de la configuracion."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .sesion import formato_valido


class ErrorConfig(Exception):
    """La configuracion es invalida o esta incompleta."""


@dataclass
class ConfigSSH:
    usuario: str = "Respaldo"
    password: str = ""
    clave_privada: str = ""
    puerto_defecto: int = 22
    timeout: int = 60
    # RouterOS acepta algoritmos modernos desde v6.45; dejarlo en None usa
    # los de paramiko por defecto.
    host_key_desconocida: str = "aceptar"  # aceptar | rechazar


@dataclass
class ConfigExport:
    # /export show-sensitive incluye passwords PPPoE, PSK wireless y secrets
    # de VPN. Requiere que el usuario tenga la policy "sensitive" en el equipo.
    mostrar_secretos: bool = True
    # Terse produce una linea por comando: diffs mucho mas limpios en git.
    terse: bool = True


@dataclass
class ConfigBinario:
    activo: bool = True
    # Cifra el .backup en el propio equipo antes de descargarlo.
    password: str = ""
    # Cuantos .backup conservar por equipo.
    retencion: int = 5
    # Solo generar el binario cuando el export en texto detecto un cambio real.
    # Evita acumular binarios identicos: el .backup cambia en cada ejecucion
    # aunque la configuracion no, porque incluye estado interno.
    solo_si_cambio: bool = True


@dataclass
class ConfigAlmacen:
    # Todo bajo /root: es lo que se decidio para el despliegue en Debian. La
    # razon de peso a favor es que /root es 0700, y el inventario puede llevar
    # las contrasenas de los routers en claro. El precio es que los servicios
    # corren como root (ver los comentarios de las unidades de systemd).
    git: str = "/root/mkbackup/configs.git"
    binarios: str = "/root/mkbackup/binarios"
    # Avance de la ejecucion, para que la web pueda mostrarlo. Lo escribe el
    # respaldo y lo lee la web; nunca al reves.
    estado: str = "/root/mkbackup/estado.json"
    # Lo que se cambia desde el panel (por ahora, cada cuanto se respalda).
    # Aparte de config.yaml a proposito: ese archivo lo edita una persona a
    # mano y esta lleno de comentarios que un volcado automatico se comeria.
    ajustes: str = "/root/mkbackup/ajustes.json"
    # Cuentas del panel: nombre, rol y HASH de la clave (nunca la clave). Va
    # aparte por lo mismo que los ajustes: las cuentas y sus claves se cambian
    # desde la web, asi que tienen que vivir donde el servicio puede escribir.
    usuarios: str = "/root/mkbackup/usuarios.json"
    # Quien entro, quien lo intento y quien toco que. Aparte del log del
    # proceso a proposito: ese se rota y se mezcla con el ruido de los
    # respaldos, y esto hay que poder leerlo despues de un incidente.
    auditoria: str = "/root/mkbackup/auditoria.log"
    # Lo ultimo que se supo de cada equipo: modelo, version de RouterOS y
    # cuando se respaldo bien por ultima vez. Va aparte del estado de la
    # ejecucion porque eso solo habla del ciclo en curso, y aqui hace falta
    # recordar lo de un equipo aunque hoy no le haya tocado.
    hechos: str = "/root/mkbackup/equipos.json"
    # Remoto opcional para replicar (se recomienda cifrado, ver README).
    remoto: str = ""
    autor: str = "mkbackup"
    email: str = "mkbackup@localhost"


@dataclass
class ConfigWeb:
    """Panel web: estado de los respaldos, inventario e historial.

    direccion por defecto 127.0.0.1: el panel lista nombres, IPs y empresas de
    toda la flota, asi que abrirlo a 0.0.0.0 es una decision consciente y no
    algo que deba pasar por dejar la configuracion de ejemplo tal cual.
    """

    direccion: str = "127.0.0.1"
    puerto: int = 8080
    # Cada cuantos segundos se refresca el panel en el navegador.
    refresco: int = 3

    # Login. La clave se guarda hasheada (pbkdf2), nunca en claro: este archivo
    # se copia, se respalda y se mira por encima del hombro. El hash se genera
    # con `mkbackup --hash-clave`. Sin clave_hash el panel no arranca.
    usuario: str = "admin"
    clave_hash: str = ""
    # Cuanto dura la sesion antes de volver a pedir la clave.
    sesion_horas: int = 8
    # Freno a la fuerza bruta: tras N fallos, esa IP espera.
    intentos_max: int = 5
    bloqueo_segundos: int = 300

    # Ver que cambio y cuando, leyendo el historial de git.
    ver_diferencias: bool = True
    # Con mostrar_secretos: true el repo lleva passwords PPPoE, PSK y secrets
    # de VPN. Esto tapa esos valores en el diff: se sigue viendo QUE linea
    # cambio, sin repartir las credenciales a quien tenga acceso al panel.
    # Ponerlo en false es una decision consciente, no un descuido.
    ocultar_secretos: bool = True
    # Cuantas versiones se listan por equipo.
    historial_maximo: int = 50
    # Eventos por pagina en el registro de auditoria.
    eventos_por_pagina: int = 30

    # Imagen de fondo de la pantalla de entrada. Ruta a un archivo del disco;
    # vacio = sin fondo. La sirve el propio panel en /fondo, porque su politica
    # de contenido (CSP) le prohibe cargar nada de otro sitio, y ademas asi no
    # hace falta montar un servidor de archivos al lado.
    fondo_login: str = ""

    # Dar de alta y editar equipos desde el panel. En false, el inventario
    # solo se toca por archivo y el panel vuelve a ser de solo lectura.
    editar_inventario: bool = True


@dataclass
class ConfigPlanificador:
    """Cada cuanto se buscan cambios en la flota.

    Antes esto lo decidia un timer de systemd. Ahora vive aqui porque el
    intervalo se cambia desde el panel, y pedirle a la web que reescriba
    unidades de systemd y recargue el demonio obligaria a darle root: mucho
    poder para un panel de consulta.

    El programador relee este archivo en cada ciclo, asi que un cambio de
    intervalo entra solo, sin reiniciar el servicio.
    """

    intervalo_minutos: int = 240
    # Respaldar nada mas arrancar, sin esperar al primer intervalo. Util tras
    # un reinicio: si no, un servidor que se reinicia cada noche podria no
    # respaldar nunca.
    al_arrancar: bool = True
    # Si un ciclo dura mas que el intervalo, NO se lanza otro encima: git no
    # admite dos escrituras a la vez. Se salta el turno y se avisa.


@dataclass
class ConfigTelegram:
    token: str = ""
    chat_id: str = ""
    modo: str = "resumen"  # resumen | detalle | ninguno


# Lo unico que el panel puede cambiar sin root, y a donde va cada cosa.
#
# Es una lista blanca explicita y corta a proposito: todo lo demas (rutas del
# repositorio, remoto de replicacion, Telegram, parametros del propio panel)
# solo se toca editando el YAML con root. Aunque alguien se hiciera con una
# cuenta de administrador del panel, no puede apuntar los respaldos a otro
# sitio ni sacarlos de la maquina.
#
# Las credenciales SSH generales SI estan: son lo que se usa con cualquier
# equipo que no traiga las suyas, cambian cuando se rota la clave de la flota,
# y pedir un root y un editor para eso llevaba a que no se rotara nunca. El
# precio, dicho claro: quien entre al panel como administrador puede cambiar
# con que credenciales se entra a los routers.
AJUSTES_EDITABLES = {
    "intervalo_minutos": ("planificador", "intervalo_minutos"),
    "al_arrancar": ("planificador", "al_arrancar"),
    "ssh_usuario": ("ssh", "usuario"),
    "ssh_password": ("ssh", "password"),
    "fondo_login": ("web", "fondo_login"),
}


def zona_horaria(nombre: str):
    """tzinfo del nombre IANA dado, con reserva para Ecuador.

    Las marcas de tiempo se guardan SIEMPRE en UTC (ver estado.py): eso no se
    toca, porque una hora local en un archivo es una bomba de relojeria el dia
    que el servidor cambia de zona. La conversion se hace solo al mostrarlas.

    La reserva existe por Windows: Python no trae la base de datos IANA en esa
    plataforma (hace falta el paquete tzdata) y `ZoneInfo` lanza. En Debian si
    esta, pero no se puede dar por hecho. Para Ecuador continental el desfase
    fijo de -5 es exacto todo el ano, porque no tiene horario de verano; para
    cualquier otra zona seria mentir, asi que ahi se avisa y se deja UTC.
    """
    from datetime import timedelta, timezone

    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(nombre)
    except Exception:  # noqa: BLE001  (ZoneInfoNotFoundError y ImportError)
        if nombre == "America/Guayaquil":
            return timezone(timedelta(hours=-5))
        logging.getLogger("mkbackup.config").warning(
            "No se pudo cargar la zona horaria '%s' (falta el paquete tzdata); "
            "las fechas se mostraran en UTC.", nombre
        )
        return timezone.utc


@dataclass
class Config:
    # Junto al resto de los datos y no en /etc: el panel da de alta equipos y
    # el renombrado automatico lo reescribe, asi que tiene que estar donde los
    # servicios pueden escribir. Y puede llevar credenciales de los routers.
    inventario: str = "/root/mkbackup/inventory.csv"
    # Solo afecta a como se MUESTRAN las fechas en el panel; lo que se guarda
    # en disco sigue siendo UTC.
    zona_horaria: str = "America/Guayaquil"
    concurrencia: int = 15
    reintentos: int = 3
    # Espera entre reintentos, en segundos (se multiplica por el intento).
    espera_reintento: int = 10

    ssh: ConfigSSH = field(default_factory=ConfigSSH)
    export: ConfigExport = field(default_factory=ConfigExport)
    binario: ConfigBinario = field(default_factory=ConfigBinario)
    almacen: ConfigAlmacen = field(default_factory=ConfigAlmacen)
    telegram: ConfigTelegram = field(default_factory=ConfigTelegram)
    web: ConfigWeb = field(default_factory=ConfigWeb)
    planificador: ConfigPlanificador = field(default_factory=ConfigPlanificador)

    # De donde se cargo, para poder recargarla y para los mensajes de error.
    ruta: Path | None = None

    @classmethod
    def desde_archivo(cls, ruta: str | Path) -> "Config":
        ruta = Path(ruta)
        if not ruta.is_file():
            raise ErrorConfig(f"no existe el archivo de configuracion: {ruta}")

        try:
            datos = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ErrorConfig(f"YAML invalido en {ruta}: {exc}") from exc

        if not isinstance(datos, dict):
            raise ErrorConfig(f"{ruta} no contiene un mapa de configuracion")

        cfg = cls(
            inventario=datos.get("inventario", cls.inventario),
            zona_horaria=datos.get("zona_horaria", cls.zona_horaria),
            concurrencia=int(datos.get("concurrencia", cls.concurrencia)),
            reintentos=int(datos.get("reintentos", cls.reintentos)),
            espera_reintento=int(datos.get("espera_reintento", cls.espera_reintento)),
            ssh=ConfigSSH(**datos.get("ssh", {})),
            export=ConfigExport(**datos.get("export", {})),
            binario=ConfigBinario(**datos.get("backup_binario", {})),
            almacen=ConfigAlmacen(**datos.get("almacen", {})),
            telegram=ConfigTelegram(**datos.get("telegram", {})),
            web=ConfigWeb(**datos.get("web", {})),
            planificador=ConfigPlanificador(**datos.get("planificador", {})),
        )
        cfg.ruta = ruta
        cfg.aplicar_ajustes()
        cfg.validar()
        return cfg

    # --- Ajustes que cambia el panel ----------------------------------------

    def aplicar_ajustes(self) -> None:
        """Pisa la configuracion con lo que se haya cambiado desde el panel.

        Tolerante a proposito: un archivo de ajustes corrupto o a medio
        escribir no puede impedir que arranque el respaldo. Se ignora y se
        sigue con lo que diga el YAML, que es un valor razonable.
        """
        archivo = Path(self.almacen.ajustes)
        try:
            datos = json.loads(archivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(datos, dict):
            return

        for clave, (seccion, campo) in AJUSTES_EDITABLES.items():
            if clave not in datos:
                continue
            destino = getattr(self, seccion)
            actual = getattr(destino, campo)
            valor = datos[clave]
            # El tipo manda: el panel escribe este archivo, pero tambien puede
            # editarlo alguien a mano y meter un texto donde va un numero.
            if isinstance(actual, bool):
                if isinstance(valor, bool):
                    setattr(destino, campo, valor)
            elif isinstance(actual, int):
                try:
                    setattr(destino, campo, int(valor))
                except (TypeError, ValueError):
                    continue
            elif isinstance(actual, str) and isinstance(valor, str):
                setattr(destino, campo, valor)

    def guardar_ajustes(self, cambios: dict) -> None:
        """Escribe los ajustes del panel. Solo lo que este en la lista blanca.

        Escritura atomica: el programador lee este archivo en cada ciclo y no
        debe encontrarselo a medias.
        """
        archivo = Path(self.almacen.ajustes)
        try:
            actuales = json.loads(archivo.read_text(encoding="utf-8"))
            if not isinstance(actuales, dict):
                actuales = {}
        except (OSError, ValueError):
            actuales = {}

        actuales.update(
            {k: v for k, v in cambios.items() if k in AJUSTES_EDITABLES}
        )

        archivo.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=archivo.parent,
            prefix=archivo.name + ".", suffix=".tmp", delete=False,
        ) as fh:
            json.dump(actuales, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
            temporal = fh.name
        # Desde que aqui puede vivir la clave SSH de la flota, este archivo es
        # tan sensible como el inventario. Se cierra ANTES del replace para que
        # el definitivo no exista ni un instante con permisos abiertos. En
        # Windows chmod apenas hace nada y puede fallar; alli manda la ACL de
        # la carpeta y no es donde corre el servicio.
        try:
            os.chmod(temporal, 0o600)
        except OSError:
            pass
        os.replace(temporal, archivo)

        self.aplicar_ajustes()

    def validar(self) -> None:
        if self.concurrencia < 1:
            raise ErrorConfig("concurrencia debe ser al menos 1")
        if self.reintentos < 1:
            raise ErrorConfig("reintentos debe ser al menos 1")

        if not self.ssh.password and not self.ssh.clave_privada:
            raise ErrorConfig(
                "hay que definir ssh.password o ssh.clave_privada; "
                "sin credenciales no se puede entrar a los equipos"
            )

        if self.ssh.clave_privada:
            clave = Path(self.ssh.clave_privada)
            if not clave.is_file():
                raise ErrorConfig(f"no existe la clave privada: {clave}")

        if self.telegram.modo not in ("resumen", "detalle", "ninguno"):
            raise ErrorConfig(
                f"telegram.modo invalido: {self.telegram.modo} "
                "(valores: resumen, detalle, ninguno)"
            )

        if self.binario.retencion < 1:
            raise ErrorConfig("backup_binario.retencion debe ser al menos 1")

        if not 1 <= self.web.puerto <= 65535:
            raise ErrorConfig(f"web.puerto fuera de rango: {self.web.puerto}")
        if self.web.refresco < 1:
            raise ErrorConfig("web.refresco debe ser al menos 1 segundo")

        # Un hash vacio se acepta aqui y lo rechaza el panel al arrancar: quien
        # solo respalda no tiene por que configurar el login para que le corra
        # el timer. Pero un hash MAL ESCRITO se caza ya, y no en el momento en
        # que alguien necesita entrar al panel.
        if self.web.clave_hash and not formato_valido(self.web.clave_hash):
            raise ErrorConfig(
                "web.clave_hash no tiene el formato esperado; generalo con "
                "`mkbackup --hash-clave`"
            )
        if not self.web.usuario.strip():
            raise ErrorConfig("web.usuario no puede estar vacio")
        if self.web.sesion_horas < 1:
            raise ErrorConfig("web.sesion_horas debe ser al menos 1")
        if self.web.intentos_max < 1:
            raise ErrorConfig("web.intentos_max debe ser al menos 1")
        if self.web.bloqueo_segundos < 0:
            raise ErrorConfig("web.bloqueo_segundos no puede ser negativo")
        if self.web.historial_maximo < 1:
            raise ErrorConfig("web.historial_maximo debe ser al menos 1")
        if self.web.eventos_por_pagina < 1:
            raise ErrorConfig("web.eventos_por_pagina debe ser al menos 1")

        if self.planificador.intervalo_minutos < 1:
            raise ErrorConfig(
                "planificador.intervalo_minutos debe ser al menos 1 "
                f"(hay {self.planificador.intervalo_minutos})"
            )
