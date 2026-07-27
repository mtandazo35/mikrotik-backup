"""Prueba del programador residente.

Lo que se comprueba aqui es lo unico que puede fallar en silencio:

  - la cuenta del proximo turno (un respaldo cada 8 horas cuando se pidieron
    4 no lo nota nadie hasta el dia que hace falta un respaldo)
  - a quien le toca en cada tic: un equipo que deja de entrar en los ciclos no
    da ningun error, simplemente se queda sin respaldos y nadie se entera
  - que el panel puede preguntar por el estado del programador aunque el
    archivo no exista o este corrupto, sin llevarse una excepcion
  - que el archivo que se escribe lleva las claves que el panel espera

NO se prueba el bucle infinito: esperar un intervalo real no cabe en una
prueba, y todo lo que decide (intervalo y vencimientos) esta extraido a
funciones puras a las que se les pasa la hora en vez de que miren el reloj.

Ejecutar:  python -m tests.test_planificador
"""

import dataclasses
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mkbackup import inventory, planificador as pl
from mkbackup.config import Config

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


@dataclass
class AlmacenFalso:
    estado: str = ""


@dataclass
class PlanificadorFalso:
    intervalo_minutos: int = 240
    al_arrancar: bool = True


@dataclass
class EquipoFalso:
    """Lo unico que mira el planificador de un equipo.

    Doble y no inventory.Equipo a proposito: asi la prueba de la decision no
    depende de la forma que tenga hoy el inventario. El enlace con el Equipo
    real se comprueba aparte, al final.
    """

    nombre: str
    intervalo_minutos: int = 0


@dataclass
class ConfigFalsa:
    """Lo minimo que mira el planificador de una configuracion.

    Se usa un doble en vez de un Config real para no arrastrar a la prueba las
    credenciales SSH que exige validar().
    """

    almacen: AlmacenFalso = field(default_factory=AlmacenFalso)
    planificador: PlanificadorFalso = field(default_factory=PlanificadorFalso)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = ConfigFalsa(almacen=AlmacenFalso(estado=str(base / "estado.json")))

        # 1. Donde escribe: al lado del estado de la ejecucion.
        ruta = pl.ruta_programador(cfg)
        comprobar("el estado del programador va junto al de la ejecucion",
                  ruta.parent == base)
        comprobar("y se llama programador.json", ruta.name == "programador.json")

        # 2. Sin archivo (el programador nunca arranco): el panel pregunta
        #    igual y recibe el diccionario completo, no una excepcion.
        vacio = pl.estado_programador(cfg)
        comprobar("sin archivo devuelve un dict", isinstance(vacio, dict))
        comprobar("sin archivo estan todas las claves",
                  set(vacio) == set(pl.ESTADO_VACIO))
        comprobar("sin archivo no hay proximo ciclo", vacio["proxima"] is None)
        comprobar("sin archivo no hay ciclos hechos", vacio["ciclos"] == 0)
        comprobar("sin archivo no esta corriendo", vacio["corriendo"] is False)

        # 3. Archivo corrupto o a medio escribir: lo mismo, sin reventar.
        ruta.write_text("{esto no es json", encoding="utf-8")
        roto = pl.estado_programador(cfg)
        comprobar("un JSON corrupto se ignora en vez de romper el panel",
                  roto == pl.ESTADO_VACIO)

        ruta.write_text('"una cadena, no un mapa"', encoding="utf-8")
        comprobar("un JSON valido que no es un mapa tampoco pasa",
                  pl.estado_programador(cfg) == pl.ESTADO_VACIO)

        # Tipos raros metidos a mano: el panel hace cuentas con estos valores.
        ruta.write_text(json.dumps({
            "proxima": None, "ultima": None, "intervalo_minutos": "muchos",
            "corriendo": "si", "ciclos": None, "ultimo_codigo": "cero",
        }), encoding="utf-8")
        sucio = pl.estado_programador(cfg)
        comprobar("un intervalo no numerico no llega al panel",
                  isinstance(sucio["intervalo_minutos"], int))
        comprobar("un codigo no numerico se queda en None",
                  sucio["ultimo_codigo"] is None)
        comprobar("corriendo siempre es booleano",
                  isinstance(sucio["corriendo"], bool))

        # 4. Lo que se escribe es lo que el panel lee.
        pl._escribir_estado(ruta, {
            "proxima": "2026-01-01T00:00:00+00:00",
            "ultima": "2025-12-31T20:00:00+00:00",
            "intervalo_minutos": 240,
            "corriendo": False,
            "ciclos": 7,
            "ultimo_codigo": 0,
        })
        leido = pl.estado_programador(cfg)
        comprobar("el archivo escrito lleva las claves esperadas",
                  set(leido) == set(pl.ESTADO_VACIO))
        comprobar("se conserva el proximo ciclo",
                  leido["proxima"] == "2026-01-01T00:00:00+00:00")
        comprobar("se conserva el ultimo ciclo",
                  leido["ultima"] == "2025-12-31T20:00:00+00:00")
        comprobar("se conserva el intervalo", leido["intervalo_minutos"] == 240)
        comprobar("se conserva el numero de ciclos", leido["ciclos"] == 7)
        comprobar("se conserva el codigo del ultimo ciclo",
                  leido["ultimo_codigo"] == 0)
        comprobar("no quedan temporales tirados en la carpeta",
                  not list(base.glob("*.tmp")))

        comprobar("un estado viejo sin 'ultimas' no rompe: se rellena vacio",
                  leido["ultimas"] == {})

        # 4b. Las marcas por equipo viajan al panel, y la basura no.
        pl._escribir_estado(ruta, {
            "proxima": None, "ultima": None, "intervalo_minutos": 240,
            "corriendo": False, "ciclos": 1, "ultimo_codigo": 0,
            "ultimas": {"core-01": "2026-01-01T00:00:00+00:00"},
            "ultimo_lote": 3,
        })
        con_marcas = pl.estado_programador(cfg)
        comprobar("las marcas por equipo llegan al panel",
                  con_marcas["ultimas"] == {"core-01": "2026-01-01T00:00:00+00:00"})
        comprobar("y cuantos equipos entraron en el ultimo ciclo",
                  con_marcas["ultimo_lote"] == 3)

        # El panel recorre 'ultimas': si alguien mete ahi una lista a mano, la
        # plantilla no puede reventar a mitad de pagina.
        ruta.write_text(json.dumps({"ultimas": ["core-01"], "ultimo_lote": "tres"}),
                        encoding="utf-8")
        raro = pl.estado_programador(cfg)
        comprobar("unas 'ultimas' que no son un mapa se descartan",
                  raro["ultimas"] == {})
        comprobar("un lote no numerico no llega al panel",
                  isinstance(raro["ultimo_lote"], int))

        # El defecto es mutable: si se devolviera el mismo objeto, el primero
        # que lo tocara cambiaria lo que ve todo el proceso a partir de ahi.
        raro["ultimas"]["colado"] = "x"
        comprobar("el diccionario por defecto no se comparte entre llamadas",
                  pl.estado_programador(cfg)["ultimas"] == {}
                  and pl.ESTADO_VACIO["ultimas"] == {})

        # Escribir en una carpeta que no existe no puede tumbar el ciclo.
        pl._escribir_estado(base / "sub" / "nueva" / "programador.json", {"ciclos": 1})
        comprobar("se crea la carpeta del estado si falta",
                  (base / "sub" / "nueva" / "programador.json").is_file())

    # 5. La cuenta del proximo turno: el tiempo del ciclo se descuenta.
    hora = 60  # minutos

    comprobar("un ciclo corto descuenta su duracion (240 min - 6 min)",
              pl.espera_hasta_el_turno(6 * 60, 240) == (240 - 6) * 60)
    comprobar("un ciclo instantaneo espera el intervalo entero",
              pl.espera_hasta_el_turno(0.0, hora) == hora * 60)
    comprobar("los segundos sueltos tambien cuentan",
              pl.espera_hasta_el_turno(90.5, 10) == 10 * 60 - 90.5)

    # Ciclo que dura MAS que el intervalo: no se encadenan ciclos sin pausa.
    largo = pl.espera_hasta_el_turno(300 * 60, 240)
    comprobar("si el ciclo pasa del intervalo se espera la pausa minima",
              largo == pl.PAUSA_MINIMA)
    comprobar("y esa pausa nunca es cero (no se encadenan ciclos)", largo > 0)

    comprobar("un ciclo justo igual al intervalo tambien pausa",
              pl.espera_hasta_el_turno(240 * 60, 240) == pl.PAUSA_MINIMA)

    # Valores imposibles: el intervalo puede venir de un JSON escrito a mano.
    comprobar("un intervalo de 0 no deja el bucle girando en vacio",
              pl.espera_hasta_el_turno(0.0, 0) > 0)
    comprobar("un intervalo negativo tampoco",
              pl.espera_hasta_el_turno(0.0, -5) > 0)
    comprobar("una duracion negativa no alarga la espera",
              pl.espera_hasta_el_turno(-30.0, 10) == 10 * 60)

    # La espera nunca puede salir en negativo: seria un sleep instantaneo en
    # bucle, es decir, respaldar sin parar.
    comprobar(
        "ninguna combinacion produce una espera negativa",
        all(
            pl.espera_hasta_el_turno(d, i) > 0
            for d in (0, 1, 59, 60, 3599, 3600, 100000)
            for i in (1, 5, 60, 240, 1440)
        ),
    )

    # 6. A quien le toca. Todo esto es puro y recibe la hora, asi que se
    #    comprueban dias de calendario en milisegundos.
    ahora = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def hace(minutos: float) -> str:
        """Marca ISO de hace `minutos` (negativo = en el futuro)."""
        return (ahora - timedelta(minutes=minutos)).isoformat()

    global_240 = 240

    # 6a. El intervalo efectivo: el del equipo manda, el 0 hereda.
    comprobar("sin intervalo propio se hereda el global",
              pl.intervalo_efectivo(EquipoFalso("sucursal"), global_240) == 240)
    comprobar("un intervalo propio manda sobre el global",
              pl.intervalo_efectivo(EquipoFalso("core", 30), global_240) == 30)
    comprobar("y tambien cuando es mas largo que el global",
              pl.intervalo_efectivo(EquipoFalso("remoto", 1440), global_240) == 1440)
    comprobar("un intervalo propio negativo se trata como 0 (hereda)",
              pl.intervalo_efectivo(EquipoFalso("raro", -5), global_240) == 240)
    comprobar("un global de 0 no deja un intervalo de 0 minutos",
              pl.intervalo_efectivo(EquipoFalso("x"), 0) == 1)
    comprobar("un equipo sin la columna todavia hereda el global",
              pl.intervalo_efectivo(object(), global_240) == 240)

    # 6b. Le toca o no le toca.
    sucursal = EquipoFalso("sucursal")          # hereda el global (240)
    core = EquipoFalso("core", 30)              # mas corto que el global
    remoto = EquipoFalso("remoto", 1440)        # mas largo que el global

    comprobar("un equipo nunca consultado siempre entra",
              pl.toca_ahora(sucursal, None, ahora, global_240))
    comprobar("una marca vacia cuenta como nunca consultado",
              pl.toca_ahora(sucursal, "", ahora, global_240))
    comprobar("recien consultado no vuelve a entrar",
              not pl.toca_ahora(sucursal, hace(10), ahora, global_240))
    comprobar("a un minuto de cumplir su intervalo tampoco",
              not pl.toca_ahora(sucursal, hace(239), ahora, global_240))
    comprobar("justo al cumplir el intervalo SI entra",
              pl.toca_ahora(sucursal, hace(240), ahora, global_240))
    comprobar("pasado el intervalo entra",
              pl.toca_ahora(sucursal, hace(600), ahora, global_240))

    comprobar("el que tiene intervalo corto entra antes que el global",
              pl.toca_ahora(core, hace(31), ahora, global_240))
    comprobar("pero no antes de cumplir el suyo",
              not pl.toca_ahora(core, hace(29), ahora, global_240))
    comprobar("el que tiene intervalo largo no entra al cumplirse el global",
              not pl.toca_ahora(remoto, hace(241), ahora, global_240))
    comprobar("y entra cuando se cumple el suyo",
              pl.toca_ahora(remoto, hace(1441), ahora, global_240))

    # Marcas que no salieron de aqui: el archivo se edita a mano y el reloj del
    # servidor se corrige. Ninguno de los dos casos puede dejar a un equipo sin
    # respaldo para siempre, que es el unico fallo de verdad grave.
    comprobar("una marca corrupta no bloquea al equipo",
              pl.toca_ahora(sucursal, "ayer por la tarde", ahora, global_240))
    comprobar("una marca que no es texto tampoco",
              pl.toca_ahora(sucursal, {"raro": 1}, ahora, global_240))
    comprobar("una marca en el futuro no deja al equipo esperando ese futuro",
              pl.toca_ahora(sucursal, hace(-600), ahora, global_240))
    comprobar("una marca sin zona horaria se toma como UTC y se respeta",
              not pl.toca_ahora(
                  sucursal, hace(10).replace("+00:00", ""), ahora, global_240))
    comprobar("una hora sin zona ya vencida si entra",
              pl.toca_ahora(
                  sucursal, hace(300).replace("+00:00", ""), ahora, global_240))
    comprobar("una hora 'ahora' sin zona no revienta la resta",
              isinstance(pl.toca_ahora(sucursal, hace(10), ahora.replace(tzinfo=None),
                                       global_240), bool))

    # 6c. La seleccion completa de un ciclo.
    flota = [core, sucursal, remoto, EquipoFalso("nuevo")]
    ultimas = {
        "core": hace(31),        # le toca (30)
        "sucursal": hace(100),   # no le toca (240)
        "remoto": hace(500),     # no le toca (1440)
        # "nuevo" no aparece: nunca se le consulto
    }
    pendientes = pl.equipos_pendientes(flota, ultimas, ahora, global_240)
    comprobar("solo entran los vencidos y los nuevos",
              [e.nombre for e in pendientes] == ["core", "nuevo"])
    comprobar("se respeta el orden del inventario",
              pendientes == [core, EquipoFalso("nuevo")])
    comprobar("sin ninguna marca entra la flota entera",
              len(pl.equipos_pendientes(flota, {}, ahora, global_240)) == 4)
    comprobar("sin equipos no entra nadie (y no revienta)",
              pl.equipos_pendientes([], ultimas, ahora, global_240) == [])
    comprobar("un 'ultimas' None se trata como vacio",
              len(pl.equipos_pendientes(flota, None, ahora, global_240)) == 4)

    recientes = {e.nombre: hace(1) for e in flota}
    comprobar("con toda la flota recien consultada no se lanza ciclo",
              pl.equipos_pendientes(flota, recientes, ahora, global_240) == [])

    # 6d. Limpieza de los que ya no estan: el archivo no puede crecer para
    #     siempre con equipos dados de baja.
    guardadas = {
        "core": hace(5),
        "sucursal": hace(5),
        "remoto": hace(5),
        "nuevo": hace(5),
        "dado-de-baja": hace(5),
        "otro-que-se-fue": hace(9999),
    }
    limpias = pl.limpiar_ultimas(guardadas, flota)
    comprobar("se van las marcas de los equipos que ya no estan",
              set(limpias) == {"core", "sucursal", "remoto", "nuevo"})
    comprobar("y se conservan las de los que siguen",
              limpias["core"] == guardadas["core"])
    comprobar("limpiar no toca el diccionario original", len(guardadas) == 6)
    comprobar("un 'ultimas' vacio se limpia sin quejarse",
              pl.limpiar_ultimas({}, flota) == {})

    # 6e. Cuanto falta para el primero: es lo que fija el ritmo del bucle.
    comprobar("si alguno nunca se consulto, toca ya",
              pl.segundos_hasta_el_proximo(flota, ultimas, ahora, global_240) == 0.0)
    comprobar("manda el vencimiento mas cercano (core, 30 min)",
              pl.segundos_hasta_el_proximo(
                  flota, recientes, ahora, global_240) == (30 - 1) * 60)
    comprobar("sin equipos no hay vencimiento que esperar",
              pl.segundos_hasta_el_proximo(
                  [], {}, ahora, global_240) == float("inf"))
    comprobar("una marca de futuro tampoco aplaza el tic",
              pl.segundos_hasta_el_proximo(
                  [sucursal], {"sucursal": hace(-60)}, ahora, global_240) == 0.0)
    comprobar("nunca sale un plazo negativo",
              pl.segundos_hasta_el_proximo(
                  [remoto], {"remoto": hace(99999)}, ahora, global_240) == 0.0)

    # 6f. El tic del bucle: nunca por debajo del suelo, y sin inventario se cae
    #     al intervalo global de siempre.
    comprobar("el tic nunca baja del suelo aunque toque ya",
              pl._espera_tic(flota, recientes, 240, 0.0, ahora) >= pl.TIC_MINIMO)
    comprobar("con un equipo a 30 min el bucle no duerme las 4 horas globales",
              pl._espera_tic([core], {"core": hace(1)}, 240, 0.0, ahora)
              == (30 - 1) * 60)
    comprobar("sin inventario legible se cae al intervalo global",
              pl._espera_tic([], {}, 240, 0.0, ahora)
              == pl.espera_hasta_el_turno(0.0, 240))
    comprobar("un equipo con intervalo 1 no convierte el bucle en una rueda",
              pl._espera_tic([EquipoFalso("loco", 1)], {"loco": hace(5)},
                             240, 0.0, ahora) == pl.TIC_MINIMO)

    # 6f-bis. La instalacion recien hecha. El instalador crea inventory.csv con
    #     solo la cabecera, asi que el programador arranca SIEMPRE con el
    #     inventario vacio. Con la regla de arriba a secas se dormia el
    #     intervalo global entero: quien acababa de instalar daba de alta su
    #     flota desde el panel y no pasaba nada durante cuatro horas.
    comprobar("recien instalado y sin equipos, se vuelve a mirar en segundos",
              pl._espera_tic([], {}, 240, 0.0, ahora, True)
              == pl.ESPERA_SIN_ESTRENAR)
    comprobar("y eso es MUCHO menos que el intervalo global",
              pl.ESPERA_SIN_ESTRENAR < pl.espera_hasta_el_turno(0.0, 240))
    comprobar("pero en un sistema ya estrenado manda el intervalo de siempre",
              pl._espera_tic([], {}, 240, 0.0, ahora, False)
              == pl.espera_hasta_el_turno(0.0, 240))
    comprobar("y por defecto (sin decir nada) se comporta como siempre",
              pl._espera_tic([], {}, 240, 0.0, ahora)
              == pl.espera_hasta_el_turno(0.0, 240))
    comprobar("con equipos ya no aplica: manda el vencimiento mas proximo",
              pl._espera_tic([core], {"core": hace(1)}, 240, 0.0, ahora, True)
              == (30 - 1) * 60)

    # 6g. Enlace con el inventario real: el planificador tiene que leer la
    #     columna del Equipo de verdad, no solo la del doble de prueba.
    campos = {c.name for c in dataclasses.fields(inventory.Equipo)}
    if "intervalo_minutos" in campos:
        real = inventory.Equipo(nombre="core-01", ip="10.0.0.1", intervalo_minutos=30)
        comprobar("el intervalo del Equipo real manda sobre el global",
                  pl.intervalo_efectivo(real, 240) == 30)
        comprobar("y un Equipo real sin intervalo hereda el global",
                  pl.intervalo_efectivo(
                      inventory.Equipo(nombre="core-02", ip="10.0.0.2"), 240) == 240)
    else:
        comprobar("Equipo aun no trae intervalo_minutos: toda la flota hereda",
                  pl.intervalo_efectivo(
                      inventory.Equipo(nombre="core-02", ip="10.0.0.2"), 240) == 240)

    # --- 7. "Respaldar ahora" ----------------------------------------------
    # El panel NO lanza el respaldo: deja una peticion en disco y el
    # programador la recoge. Dos procesos escribiendo en el mismo repositorio
    # de git es lo unico que este proyecto no puede permitirse (index.lock), y
    # es justo lo que haria un boton que arrancara su propio ciclo.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.almacen.estado = str(Path(tmp) / "estado.json")

        comprobar("sin peticion, no hay nada que recoger",
                  pl.recoger_peticion(cfg) is None)

        comprobar("el panel puede dejar la peticion",
                  pl.pedir_ciclo(cfg, "manuel") is True)
        comprobar("y queda un archivo, que es lo que ve el programador",
                  pl.ruta_peticion(cfg).is_file())

        comprobar("el programador la recoge y sabe quien la pidio",
                  pl.recoger_peticion(cfg) == "manuel")
        # Recogerla la CONSUME. Sin esto, el programador encadenaria un ciclo
        # cada 5 segundos para siempre, que es peor que no tener el boton.
        comprobar("recogerla la consume: no se repite el ciclo",
                  pl.recoger_peticion(cfg) is None)
        comprobar("y el archivo desaparece",
                  not pl.ruta_peticion(cfg).exists())

        # Dos clicks seguidos son UN ciclo, no dos encolados.
        pl.pedir_ciclo(cfg, "uno")
        pl.pedir_ciclo(cfg, "dos")
        comprobar("dos clicks seguidos dejan una sola peticion",
                  pl.recoger_peticion(cfg) == "dos"
                  and pl.recoger_peticion(cfg) is None)

        # Un archivo que no se entiende NO puede dejar al programador
        # arrancando ciclos sin parar: se consume igual. Que exista ya
        # significa que alguien le dio al boton.
        pl.ruta_peticion(cfg).write_text("esto no es json", encoding="utf-8")
        comprobar("una peticion ilegible se atiende igual (el archivo es la senal)",
                  pl.recoger_peticion(cfg) == "")
        comprobar("y tambien se consume, no se queda dando vueltas",
                  pl.recoger_peticion(cfg) is None)

        # Vacio tambien: el nombre es lo accesorio.
        pl.ruta_peticion(cfg).write_text("{}", encoding="utf-8")
        comprobar("sin nombre dentro, sigue siendo una peticion valida",
                  pl.recoger_peticion(cfg) == "")

        comprobar("la peticion vive al lado del estado, no en otra carpeta",
                  pl.ruta_peticion(cfg).parent
                  == Path(cfg.almacen.estado).parent)

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
