"""Estado de la ejecucion, para poder mirarlo desde fuera mientras corre.

mkbackup no es un demonio: el timer lo arranca, respalda y termina. Eso deja
sin respuesta la pregunta que hace cualquiera que no este leyendo el log:
esta corriendo ahora mismo, o ya termino?

Aqui se escribe un JSON con el avance equipo por equipo. La web lo LEE; nadie
mas lo escribe. Esa separacion es a proposito: el panel no puede estorbar al
respaldo, y si el panel esta caido el respaldo sigue igual.

Como se distingue "en curso" de "se murio a medias": mientras la ejecucion
vive, un hilo refresca un latido cada pocos segundos. Si el archivo dice que
no termino pero el latido esta viejo, es que el proceso se corto (kill, corte
de luz, OOM) y se reporta INTERRUMPIDA. Sin eso, una ejecucion muerta se
queda "en curso" para siempre y el panel miente justo cuando hay que mirarlo.

El archivo NO contiene configuracion de los equipos: solo nombres, IPs,
estado y hashes de commit. Es deliberado, porque el repo si lleva secretos
(ver mostrar_secretos en el README) y este archivo lo lee un servicio web.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Cada cuanto refresca el latido el hilo de fondo.
LATIDO_SEGUNDOS = 5
# A partir de cuanto silencio se considera muerta una ejecucion sin terminar.
# Holgado respecto al latido: un servidor cargado puede retrasar el hilo, y
# declarar "interrumpida" una ejecucion viva seria peor que tardar en notarlo.
LATIDO_MAXIMO = 40
# Ejecuciones anteriores que se conservan para ver la tendencia.
HISTORIAL_MAXIMO = 20
# Cuanto se insiste en leer el estado cuando el archivo esta bloqueado. Solo
# se gasta si hay colision de verdad: en el caso normal se acierta a la
# primera y no se espera nada. Dos segundos es mucho mas de lo que dura un
# os.replace y sigue siendo menos de lo que tarda el panel en refrescar.
PLAZO_LECTURA = 2.0
# Y cuanto se insiste cuando el archivo directamente no esta. Corto: casi
# siempre significa que no ha corrido nunca, y el panel pregunta cada pocos
# segundos.
PLAZO_AUSENTE = 0.2

# Estados por equipo.
PENDIENTE = "pendiente"
EN_CURSO = "en_curso"
SIN_CAMBIOS = "sin_cambios"
CAMBIO = "cambio"
FALLO = "fallo"

# Situaciones de la ejecucion completa.
NUNCA = "nunca"
CORRIENDO = "corriendo"
INTERRUMPIDA = "interrumpida"
TERMINADA = "terminada"


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _segundos_desde(marca: str) -> float:
    """Segundos transcurridos desde una marca ISO. Infinito si no se entiende."""
    if not marca:
        return float("inf")
    try:
        momento = datetime.fromisoformat(marca)
    except ValueError:
        return float("inf")
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - momento).total_seconds()


@dataclass
class EquipoEstado:
    nombre: str
    ip: str
    grupo: str
    # El panel agrupa la flota por cliente: sin la empresa aqui tendria que
    # cruzar este archivo con el inventario para pintar una columna.
    empresa: str = ""
    estado: str = PENDIENTE
    detalle: str = ""
    intento: int = 0
    fin: str = ""


class Estado:
    """Escritor del estado. Seguro para usar desde varios hilos.

    Los equipos se consultan en paralelo, asi que las transiciones llegan
    desde hilos distintos; toda mutacion pasa por el mismo lock y reescribe
    el archivo completo. Con 300 equipos el JSON ronda las decenas de KB y se
    escribe un par de veces por equipo: irrelevante al lado de un SSH.
    """

    def __init__(self, ruta: str | Path):
        self.ruta = Path(ruta)
        self._lock = threading.Lock()
        self._equipos: dict[str, EquipoEstado] = {}
        self._datos: dict = {
            "formato": 1,
            "inicio": "",
            "fin": "",
            "latido": "",
            "pid": os.getpid(),
            "total": 0,
            "ok": 0,
            "cambios": 0,
            "fallidos": 0,
            "terminada": False,
            "duracion": 0.0,
            "codigo_salida": None,
            "binario_activo": False,
            "concurrencia": 0,
            "avisos_inventario": [],
            "equipos": [],
            "historial": [],
        }
        self._hilo: threading.Thread | None = None
        self._parar = threading.Event()
        self._activo = False

    # --- Ciclo de vida ------------------------------------------------------

    def iniciar(
        self,
        equipos,
        concurrencia: int = 0,
        binario_activo: bool = False,
        avisos: list[str] | None = None,
    ) -> None:
        with self._lock:
            self._datos["historial"] = self._historial_previo()
            self._equipos = {
                e.nombre: EquipoEstado(
                    nombre=e.nombre,
                    ip=e.ip,
                    grupo=e.grupo,
                    # getattr y no e.empresa: aqui tambien llegan objetos de
                    # prueba que solo traen lo minimo.
                    empresa=getattr(e, "empresa", ""),
                )
                for e in equipos
            }
            self._datos.update(
                inicio=_ahora(),
                fin="",
                latido=_ahora(),
                pid=os.getpid(),
                total=len(equipos),
                ok=0,
                cambios=0,
                fallidos=0,
                terminada=False,
                duracion=0.0,
                codigo_salida=None,
                binario_activo=binario_activo,
                concurrencia=concurrencia,
                avisos_inventario=list(avisos or []),
            )
            self._activo = True
            self._escribir()

        self._parar.clear()
        # Daemon: si el proceso principal muere, este hilo no lo mantiene vivo.
        # Y si muere sin escribir el final, el latido viejo delata el corte.
        self._hilo = threading.Thread(target=self._latir, name="latido", daemon=True)
        self._hilo.start()

    def _historial_previo(self) -> list[dict]:
        """Resumen de las ejecuciones anteriores, leido del archivo que haya."""
        anterior = leer(self.ruta)
        if not anterior:
            return []

        historial = list(anterior.get("historial", []))
        if anterior.get("inicio"):
            historial.insert(
                0,
                {
                    "inicio": anterior.get("inicio", ""),
                    "fin": anterior.get("fin", ""),
                    "total": anterior.get("total", 0),
                    "ok": anterior.get("ok", 0),
                    "cambios": anterior.get("cambios", 0),
                    "fallidos": anterior.get("fallidos", 0),
                    "duracion": anterior.get("duracion", 0.0),
                    # Una ejecucion previa sin fin es una que se corto: queda
                    # asi registrada en vez de perderse al arrancar la nueva.
                    "terminada": bool(anterior.get("terminada")),
                },
            )
        return historial[:HISTORIAL_MAXIMO]

    def terminar(self, duracion: float, codigo: int) -> None:
        with self._lock:
            self._datos.update(
                fin=_ahora(),
                latido=_ahora(),
                terminada=True,
                duracion=round(duracion, 1),
                codigo_salida=codigo,
            )
            self._activo = False
            self._escribir()
        self._parar.set()

    def __enter__(self) -> "Estado":
        return self

    def __exit__(self, tipo, valor, traza) -> bool:
        # Que una excepcion no deje el archivo diciendo "en curso" eternamente.
        if self._activo:
            self.terminar(duracion=0.0, codigo=1 if tipo else 0)
        return False

    def _latir(self) -> None:
        while not self._parar.wait(LATIDO_SEGUNDOS):
            with self._lock:
                if not self._activo:
                    return
                self._datos["latido"] = _ahora()
                self._escribir()

    # --- Transiciones por equipo -------------------------------------------

    def equipo_inicia(self, nombre: str, intento: int = 1) -> None:
        self._actualizar(nombre, estado=EN_CURSO, intento=intento, detalle="")

    def equipo_reintenta(self, nombre: str, intento: int, motivo: str) -> None:
        self._actualizar(
            nombre, estado=EN_CURSO, intento=intento, detalle=f"reintentando: {motivo}"
        )

    def equipo_sin_cambios(self, nombre: str) -> None:
        self._actualizar(nombre, estado=SIN_CAMBIOS, detalle="", fin=_ahora())
        self._sumar(ok=1)

    def equipo_cambio(self, nombre: str, commit: str, binario: bool = False) -> None:
        detalle = commit + (" + binario" if binario else "")
        self._actualizar(nombre, estado=CAMBIO, detalle=detalle, fin=_ahora())
        self._sumar(ok=1, cambios=1)

    def equipo_falla(self, nombre: str, tipo: str, motivo: str) -> None:
        self._actualizar(nombre, estado=FALLO, detalle=f"{tipo}: {motivo}", fin=_ahora())
        self._sumar(fallidos=1)

    def _actualizar(self, nombre: str, **campos) -> None:
        with self._lock:
            equipo = self._equipos.get(nombre)
            if equipo is None:
                return
            for clave, valor in campos.items():
                setattr(equipo, clave, valor)
            self._datos["latido"] = _ahora()
            self._escribir()

    def _sumar(self, ok: int = 0, cambios: int = 0, fallidos: int = 0) -> None:
        with self._lock:
            self._datos["ok"] += ok
            self._datos["cambios"] += cambios
            self._datos["fallidos"] += fallidos
            self._escribir()

    # --- Escritura ----------------------------------------------------------

    def _escribir(self) -> None:
        """Vuelca el estado. Debe llamarse con el lock tomado.

        Escritura atomica (archivo temporal + replace): la web lee este archivo
        mientras el respaldo lo escribe, y un lector no debe encontrarse nunca
        un JSON a medias. Nunca lanza: un panel roto no puede tumbar respaldos.
        """
        self._datos["equipos"] = [asdict(e) for e in self._equipos.values()]
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
                json.dump(self._datos, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
                temporal = fh.name
            os.replace(temporal, self.ruta)
        except OSError:
            try:
                os.unlink(temporal)
            except (OSError, NameError, UnboundLocalError):
                pass


# --- Lectura (la usa la web) ------------------------------------------------


def leer(ruta: str | Path, intentos: int = 12) -> dict | None:
    """Devuelve el estado, o None si no hay o no se puede leer.

    Tolerante a proposito: si el archivo esta corrupto, el panel debe decir
    "no se sabe" y no reventar.

    Los reintentos son por Windows: alli os.replace bloquea el archivo un
    instante, y un lector que caiga justo en esa ventana recibe
    PermissionError. En Linux el replace es atomico y esto nunca se usa, pero
    sin ello el panel parpadea a "sin ejecuciones" mientras el respaldo corre,
    que es exactamente el momento en que alguien lo esta mirando.

    Sobre el numero de intentos: con 5 se colaba una lectura ciega de cada
    ~13.000 durante una ejecucion con 30 hilos escribiendo. Poco, pero el
    panel refresca cada pocos segundos y quien lo mire justo ahi ve un
    parpadeo sin motivo. La espera total en el peor caso sigue siendo de
    decimas de segundo, y solo se paga cuando el archivo esta bloqueado de
    verdad: en el caso normal se acierta a la primera y no se duerme nada.
    """
    ruta = Path(ruta)
    limite = time.monotonic() + PLAZO_LECTURA
    # Plazo corto y aparte para el archivo ausente: ver el comentario de
    # FileNotFoundError mas abajo.
    breve = time.monotonic() + PLAZO_AUSENTE
    espera = 0.005
    while True:
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # Casi siempre significa "todavia no ha corrido nunca", y ahi
            # reintentar dos segundos seria absurdo: el panel pregunta cada
            # pocos segundos y se quedaria colgado en cada vuelta.
            #
            # Pero en Windows hay un instante, durante os.replace, en el que el
            # archivo NO esta: un lector que caiga justo ahi recibe esto aunque
            # el archivo exista de sobra. Por eso se insiste un poco -unas
            # decimas- y solo entonces se da por bueno que no hay estado. Es lo
            # que quedaba de las "lecturas ciegas" que se colaban de vez en
            # cuando con el respaldo escribiendo a destajo.
            if time.monotonic() >= breve:
                return None
            time.sleep(0.005)
        except OSError:
            # Se reintenta contra un PLAZO y no contra un numero de intentos.
            # Con un numero fijo, la resistencia depende de cuanto se tarde en
            # cada vuelta: en una maquina cargada, donde el respaldo escribe
            # mas seguido, es justo cuando mas colisiones hay y cuando antes
            # se agotan los intentos. El plazo se adapta solo.
            if time.monotonic() >= limite:
                return None
            # Creciente y con tope: si el respaldo esta escribiendo sin parar,
            # esperar siempre lo mismo puede caer una y otra vez en la misma
            # ventana. El tope evita quedarse esperando mas de lo que tarda el
            # propio refresco del panel.
            time.sleep(espera)
            espera = min(espera * 1.7, 0.05)
        except ValueError:
            return None  # corrupto de verdad
        else:
            return datos if isinstance(datos, dict) else None


def situacion(datos: dict | None) -> str:
    """NUNCA | CORRIENDO | INTERRUMPIDA | TERMINADA."""
    if not datos or not datos.get("inicio"):
        return NUNCA
    if datos.get("terminada"):
        return TERMINADA
    if _segundos_desde(datos.get("latido", "")) > LATIDO_MAXIMO:
        return INTERRUMPIDA
    return CORRIENDO


def resumen(ruta: str | Path) -> dict:
    """Estado listo para servir: lo del archivo mas lo que depende de la hora."""
    datos = leer(ruta)
    situacion_actual = situacion(datos)

    if datos is None:
        datos = {}

    equipos = datos.get("equipos", [])
    hechos = sum(
        1 for e in equipos if e.get("estado") in (SIN_CAMBIOS, CAMBIO, FALLO)
    )

    if situacion_actual == CORRIENDO:
        transcurrido = _segundos_desde(datos.get("inicio", ""))
    else:
        transcurrido = float(datos.get("duracion", 0.0) or 0.0)

    return {
        **datos,
        "situacion": situacion_actual,
        "hechos": hechos,
        "transcurrido": round(transcurrido, 1) if transcurrido != float("inf") else 0.0,
        "latido_hace": round(_segundos_desde(datos.get("latido", "")), 1)
        if datos.get("latido")
        else None,
        "servidor": _ahora(),
    }
