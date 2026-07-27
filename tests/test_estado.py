"""Prueba del estado que alimenta el panel web.

Lo que se comprueba aqui es lo que hace creible al panel:

  - una ejecucion viva se ve como viva, y una que se corto NO se queda
    "en curso" para siempre
  - los contadores cuadran aunque las transiciones lleguen de 30 hilos
  - quien lee mientras se escribe nunca ve un JSON a medias

No conecta a ningun equipo ni levanta el servidor.

Ejecutar:  python -m tests.test_estado
"""

import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mkbackup import estado as est
from mkbackup.inventory import Equipo

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def equipos(n: int) -> list[Equipo]:
    return [
        Equipo(nombre=f"BTS-{i:03d}", ip=f"10.0.0.{i}", grupo="bts")
        for i in range(1, n + 1)
    ]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        # 1. Sin archivo: el panel debe decir "nunca", no reventar.
        ruta = base / "no_existe.json"
        comprobar("sin archivo no hay estado", est.leer(ruta) is None)
        comprobar("sin archivo la situacion es 'nunca'",
                  est.situacion(est.leer(ruta)) == est.NUNCA)
        comprobar("resumen sin archivo no lanza",
                  est.resumen(ruta)["situacion"] == est.NUNCA)

        # 2. Archivo corrupto (o a medio escribir): tampoco debe reventar.
        roto = base / "roto.json"
        roto.write_text("{esto no es json", encoding="utf-8")
        comprobar("un JSON corrupto se ignora en vez de romper el panel",
                  est.leer(roto) is None)

        # 3. Ejecucion en marcha.
        ruta = base / "estado.json"
        e = est.Estado(ruta)
        e.iniciar(equipos(3), concurrencia=2, binario_activo=False,
                  avisos=["L4: puerto raro"])

        datos = est.leer(ruta)
        comprobar("al iniciar se escribe el archivo", datos is not None)
        comprobar("la situacion es 'corriendo'", est.situacion(datos) == est.CORRIENDO)
        comprobar("estan los 3 equipos", len(datos["equipos"]) == 3)
        comprobar("todos empiezan pendientes",
                  all(x["estado"] == est.PENDIENTE for x in datos["equipos"]))
        comprobar("se conservan los avisos del inventario",
                  datos["avisos_inventario"] == ["L4: puerto raro"])
        comprobar("hechos = 0 al empezar", est.resumen(ruta)["hechos"] == 0)

        # 4. Transiciones.
        e.equipo_inicia("BTS-001")
        estados = {x["nombre"]: x["estado"] for x in est.leer(ruta)["equipos"]}
        comprobar("el equipo en marcha se marca en curso",
                  estados["BTS-001"] == est.EN_CURSO)
        comprobar("los demas siguen pendientes", estados["BTS-002"] == est.PENDIENTE)

        e.equipo_cambio("BTS-001", "abc1234", binario=True)
        e.equipo_sin_cambios("BTS-002")
        e.equipo_falla("BTS-003", "timeout", "sin respuesta en 60s")

        resumen = est.resumen(ruta)
        comprobar("cuenta los correctos", resumen["ok"] == 2)
        comprobar("cuenta los cambios", resumen["cambios"] == 1)
        comprobar("cuenta los fallidos", resumen["fallidos"] == 1)
        comprobar("hechos = 3 con todos resueltos", resumen["hechos"] == 3)
        detalles = {x["nombre"]: x["detalle"] for x in resumen["equipos"]}
        comprobar("el cambio guarda commit y binario",
                  detalles["BTS-001"] == "abc1234 + binario")
        comprobar("el fallo guarda tipo y motivo",
                  detalles["BTS-003"].startswith("timeout:"))
        comprobar("sigue 'corriendo' hasta que se cierre",
                  resumen["situacion"] == est.CORRIENDO)

        # 5. Fin: queda el resultado visible, no se borra.
        e.terminar(duracion=12.4, codigo=1)
        final = est.resumen(ruta)
        comprobar("al terminar la situacion es 'terminada'",
                  final["situacion"] == est.TERMINADA)
        comprobar("guarda la duracion", final["duracion"] == 12.4)
        comprobar("guarda el codigo de salida", final["codigo_salida"] == 1)
        comprobar("el detalle de los equipos sobrevive al fin",
                  len(final["equipos"]) == 3)

        # 6. Una ejecucion cortada NO puede quedarse "en curso" para siempre.
        #    Se simula un latido viejo, que es justo lo que deja un kill -9.
        viejo = base / "colgado.json"
        hace_rato = (
            datetime.now(timezone.utc) - timedelta(seconds=est.LATIDO_MAXIMO + 30)
        ).isoformat()
        viejo.write_text(json.dumps({
            "inicio": hace_rato, "latido": hace_rato, "terminada": False,
            "total": 10, "ok": 4, "cambios": 1, "fallidos": 0, "equipos": [],
        }), encoding="utf-8")
        comprobar("un latido viejo se reporta como INTERRUMPIDA",
                  est.situacion(est.leer(viejo)) == est.INTERRUMPIDA)

        fresco = base / "vivo.json"
        fresco.write_text(json.dumps({
            "inicio": hace_rato,
            "latido": datetime.now(timezone.utc).isoformat(),
            "terminada": False, "total": 10, "equipos": [],
        }), encoding="utf-8")
        comprobar("un latido fresco sigue siendo CORRIENDO",
                  est.situacion(est.leer(fresco)) == est.CORRIENDO)

        # 7. Una excepcion no debe dejar el panel diciendo "en curso".
        ruta2 = base / "revienta.json"
        try:
            with est.Estado(ruta2) as e2:
                e2.iniciar(equipos(2))
                raise RuntimeError("algo exploto a mitad")
        except RuntimeError:
            pass
        comprobar("si revienta a mitad, el estado queda cerrado igualmente",
                  est.situacion(est.leer(ruta2)) == est.TERMINADA)
        comprobar("y con codigo de salida distinto de 0",
                  est.leer(ruta2)["codigo_salida"] == 1)

        # 8. Historial: la ejecucion anterior no se pierde al empezar otra.
        e3 = est.Estado(ruta)
        e3.iniciar(equipos(2))
        e3.terminar(duracion=3.0, codigo=0)
        historial = est.leer(ruta)["historial"]
        comprobar("la ejecucion anterior pasa al historial", len(historial) == 1)
        comprobar("el historial conserva sus cifras",
                  historial[0]["cambios"] == 1 and historial[0]["fallidos"] == 1)

        # 8b. Y no crece sin fin. Este archivo lo lee el panel cada pocos
        #     segundos, asi que cada fila de mas se paga en cada refresco; y en
        #     pantalla, una tabla larga hace que no se lea ninguna fila.
        ruta_larga = base / "muchas.json"
        for i in range(HISTORIAL := est.HISTORIAL_MAXIMO + 8):
            e = est.Estado(ruta_larga)
            e.iniciar(equipos(1))
            e.terminar(duracion=float(i), codigo=0)
        guardadas = est.leer(ruta_larga)["historial"]
        comprobar(f"tras {HISTORIAL} ejecuciones se guardan solo "
                  f"{est.HISTORIAL_MAXIMO} ({len(guardadas)})",
                  len(guardadas) == est.HISTORIAL_MAXIMO)
        comprobar("son las mas RECIENTES, no las primeras",
                  guardadas[0]["duracion"] > guardadas[-1]["duracion"])
        comprobar("y el tope son 10, que es lo que cabe de un vistazo",
                  est.HISTORIAL_MAXIMO == 10)

        # 9. Concurrencia: las transiciones llegan desde los hilos del pool.
        ruta3 = base / "paralelo.json"
        e4 = est.Estado(ruta3)
        flota = equipos(30)
        e4.iniciar(flota, concurrencia=15)

        def trabajar(equipo, indice):
            e4.equipo_inicia(equipo.nombre)
            if indice % 3 == 0:
                e4.equipo_falla(equipo.nombre, "timeout", "sin respuesta")
            elif indice % 3 == 1:
                e4.equipo_cambio(equipo.nombre, f"c{indice:05d}")
            else:
                e4.equipo_sin_cambios(equipo.nombre)

        hilos = [
            threading.Thread(target=trabajar, args=(eq, i))
            for i, eq in enumerate(flota)
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        paralelo = est.resumen(ruta3)
        comprobar("con 30 hilos los contadores cuadran",
                  paralelo["ok"] + paralelo["fallidos"] == 30)
        comprobar("ningun equipo se queda sin resolver", paralelo["hechos"] == 30)
        comprobar("no se pierde ni se duplica ningun equipo",
                  len({x["nombre"] for x in paralelo["equipos"]}) == 30)
        e4.terminar(duracion=1.0, codigo=1)

        # 10. Escritura atomica: quien lee mientras se escribe nunca debe
        #     encontrarse un JSON partido por la mitad.
        ruta4 = base / "atomico.json"
        e5 = est.Estado(ruta4)
        flota = equipos(40)
        e5.iniciar(flota)
        parar = threading.Event()
        lecturas = {"total": 0, "rotas": 0, "bloqueadas": 0, "vacias": 0, "ciegas": 0}

        def escribir():
            for vuelta in range(60):
                for eq in flota:
                    e5.equipo_cambio(eq.nombre, f"v{vuelta:04d}")
                if parar.is_set():
                    return
            parar.set()

        def leer_sin_parar():
            while not parar.is_set():
                try:
                    crudo = ruta4.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except OSError:
                    # Windows: os.replace bloquea el archivo un instante. Es
                    # lo que absorben los reintentos de estado.leer().
                    lecturas["bloqueadas"] += 1
                    continue
                lecturas["total"] += 1
                if not crudo:
                    lecturas["vacias"] += 1
                    continue
                try:
                    json.loads(crudo)
                except ValueError:
                    lecturas["rotas"] += 1
                # Lo que de verdad importa: el lector que usa el panel siempre
                # obtiene un estado, nunca None, mientras la ejecucion corre.
                if est.leer(ruta4) is None:
                    lecturas["ciegas"] += 1

        escritor = threading.Thread(target=escribir)
        lector = threading.Thread(target=leer_sin_parar)
        escritor.start()
        lector.start()
        escritor.join()
        parar.set()
        lector.join()

        comprobar("el lector alcanzo a leer mientras se escribia",
                  lecturas["total"] > 50)
        comprobar(
            f"ninguna lectura salio partida ni vacia ({lecturas['total']} lecturas, "
            f"{lecturas['rotas']} partidas, {lecturas['vacias']} vacias)",
            lecturas["rotas"] == 0 and lecturas["vacias"] == 0,
        )
        comprobar(
            f"el panel nunca se queda sin estado ({lecturas['ciegas']} ciegas de "
            f"{lecturas['total']}, {lecturas['bloqueadas']} bloqueos absorbidos)",
            lecturas["ciegas"] == 0,
        )
        e5.terminar(duracion=1.0, codigo=0)

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
