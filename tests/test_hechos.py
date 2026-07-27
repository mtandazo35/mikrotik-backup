"""Prueba de lo que se recuerda de cada equipo (modelo, version, ultimo respaldo).

Lo que se comprueba aqui es lo que hace util a este archivo:

  - un dato VACIO nunca pisa uno bueno: un equipo apagado no puede olvidar que
    es un RB4011, que es justo cuando hace falta saberlo
  - ultimo_ok solo avanza cuando el respaldo salio bien, mientras ultimo_intento
    avanza siempre: sin esa pareja el panel no puede decir "se intento hace 5
    minutos, pero el ultimo bueno fue anteayer"
  - un archivo que no existe o esta corrupto no lanza nada: que esto se rompa no
    puede impedir un respaldo ni tumbar el panel
  - las anotaciones llegan desde los hilos del pool y no se pierde ninguna

No conecta a ningun equipo ni levanta el servidor.

Ejecutar:  python -m tests.test_hechos
"""

import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from mkbackup import hechos as hh

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        # 1. Sin archivo: no se sabe nada, pero no se lanza nada.
        h = hh.Hechos(base / "no_existe.json")
        comprobar("sin archivo, leer() devuelve un dict vacio", h.leer() == {})
        comprobar("sin archivo, de() devuelve {}", h.de("BTS-001") == {})
        comprobar("sin archivo, limpiar() no lanza", h.limpiar(["BTS-001"]) == 0)

        # 2. Archivo corrupto (o a medio escribir): se empieza de cero, sin
        #    excepciones. Que no se pueda leer esto no puede parar un respaldo.
        roto = base / "roto.json"
        roto.write_text("{esto no es json", encoding="utf-8")
        h_roto = hh.Hechos(roto)
        comprobar("un JSON corrupto se ignora en vez de romper nada",
                  h_roto.leer() == {})
        h_roto.anotar("BTS-001", modelo="RB4011iGS+", ok=True)
        comprobar("sobre un archivo corrupto se puede volver a anotar",
                  h_roto.de("BTS-001").get("modelo") == "RB4011iGS+")

        # Un JSON valido pero que no tiene esta forma tampoco puede reventar.
        raro = base / "raro.json"
        raro.write_text('["una", "lista"]', encoding="utf-8")
        comprobar("un JSON con otra forma se ignora", hh.Hechos(raro).leer() == {})

        # 3. Anotar y releer con OTRA instancia: el panel es otro proceso, no
        #    puede depender de la memoria del que respalda.
        ruta = base / "equipos.json"
        h = hh.Hechos(ruta)
        h.anotar(
            "BTS-Norte-01", modelo="RB4011iGS+", version="7.14.2",
            resultado="cambio", commit="abc1234", ok=True,
        )
        otro = hh.Hechos(ruta)
        datos = otro.de("BTS-Norte-01")
        comprobar("otra instancia ve lo anotado", bool(datos))
        comprobar("guarda el modelo", datos["modelo"] == "RB4011iGS+")
        comprobar("guarda la version", datos["version"] == "7.14.2")
        comprobar("guarda el resultado", datos["resultado"] == "cambio")
        comprobar("guarda el commit", datos["ultimo_commit"] == "abc1234")
        comprobar("un equipo desconocido sigue devolviendo {}",
                  otro.de("BTS-Que-No-Existe") == {})

        contenido = json.loads(ruta.read_text(encoding="utf-8"))
        comprobar("el archivo declara el formato", contenido.get("formato") == 1)
        comprobar("los equipos van bajo la clave 'equipos'",
                  "BTS-Norte-01" in contenido.get("equipos", {}))

        # 4. EL CASO CENTRAL: un dato vacio no pisa uno bueno.
        #    El ciclo siguiente falla y no se pudo leer ni modelo ni version.
        h.anotar("BTS-Norte-01", modelo="", version="", resultado="fallo", ok=False)
        tras_fallo = h.de("BTS-Norte-01")
        comprobar("tras un fallo se conserva el modelo que ya se sabia",
                  tras_fallo["modelo"] == "RB4011iGS+")
        comprobar("tras un fallo se conserva la version que ya se sabia",
                  tras_fallo["version"] == "7.14.2")
        comprobar("tras un fallo se conserva el ultimo commit conocido",
                  tras_fallo["ultimo_commit"] == "abc1234")
        comprobar("el resultado si se actualiza a 'fallo'",
                  tras_fallo["resultado"] == "fallo")

        # 5. ultimo_intento avanza siempre; ultimo_ok solo cuando ok=True.
        ok_tras_fallo = tras_fallo["ultimo_ok"]
        comprobar("el fallo NO movio el ultimo respaldo bueno",
                  ok_tras_fallo == datos["ultimo_ok"])
        comprobar("pero SI movio el ultimo intento",
                  tras_fallo["ultimo_intento"] > datos["ultimo_intento"])

        h.anotar("BTS-Norte-01", modelo="RB4011iGS+", version="7.15",
                 resultado="sin_cambios", ok=True)
        bueno = h.de("BTS-Norte-01")
        comprobar("un ciclo bueno si mueve el ultimo respaldo bueno",
                  bueno["ultimo_ok"] > ok_tras_fallo)
        comprobar("un dato nuevo si pisa al viejo (la version subio)",
                  bueno["version"] == "7.15")
        comprobar("un ciclo sin cambios conserva el commit del ultimo cambio",
                  bueno["ultimo_commit"] == "abc1234")

        # Un equipo que nunca contesto: solo se sabe que se intento.
        h.anotar("BTS-Muerto", resultado="fallo", ok=False)
        muerto = h.de("BTS-Muerto")
        comprobar("de un equipo que nunca contesto queda el intento",
                  bool(muerto.get("ultimo_intento")))
        comprobar("y no se inventa un ultimo respaldo bueno",
                  "ultimo_ok" not in muerto)

        # 6. Texto raro del equipo: viene de RouterOS, puede traer cualquier
        #    cosa. No debe partir el JSON ni la tabla del panel.
        h.anotar("BTS-Raro", modelo="RB\n\x00760iGS " + "X" * 200, ok=True)
        raro_modelo = h.de("BTS-Raro")["modelo"]
        comprobar("el modelo se queda sin caracteres de control",
                  "\n" not in raro_modelo and "\x00" not in raro_modelo)
        comprobar("el modelo se recorta a algo razonable",
                  0 < len(raro_modelo) <= hh.LARGO_MAXIMO)
        h.anotar("", modelo="RB4011iGS+", ok=True)
        comprobar("un nombre vacio no crea una fila fantasma",
                  "" not in hh.Hechos(ruta).leer())

        # 7. limpiar: se olvidan los que ya no estan en el inventario.
        vivos = ["BTS-Norte-01", "BTS-Raro"]
        borrados = h.limpiar(vivos)
        comprobar("limpiar devuelve cuantos olvido", borrados == 1)
        quedan = hh.Hechos(ruta).leer()
        comprobar("el dado de baja desaparece", "BTS-Muerto" not in quedan)
        comprobar("los que siguen en el inventario se conservan",
                  set(quedan) == set(vivos))
        comprobar("limpiar sin nada que borrar no cambia nada",
                  h.limpiar(vivos) == 0)
        # Una lista vacia casi siempre significa "no se pudo leer el
        # inventario": borrarlo todo seria irrecuperable.
        comprobar("limpiar con la lista vacia no borra la flota entera",
                  h.limpiar([]) == 0 and len(hh.Hechos(ruta).leer()) == 2)

        # 8. Concurrencia: las anotaciones llegan desde los hilos del pool.
        paralelo = base / "paralelo.json"
        hp = hh.Hechos(paralelo)
        nombres = [f"BTS-{i:03d}" for i in range(40)]

        def trabajar(nombre: str, indice: int) -> None:
            if indice % 4 == 0:
                hp.anotar(nombre, resultado="fallo", ok=False)
            else:
                hp.anotar(nombre, modelo="RB4011iGS+", version="7.14.2",
                          resultado="sin_cambios", ok=True)

        hilos = [
            threading.Thread(target=trabajar, args=(n, i))
            for i, n in enumerate(nombres)
        ]
        for t in hilos:
            t.start()
        for t in hilos:
            t.join()

        final = hh.Hechos(paralelo).leer()
        comprobar("con 40 hilos no se pierde ningun equipo",
                  set(final) == set(nombres))
        comprobar("ninguna anotacion queda sin fecha de intento",
                  all(d.get("ultimo_intento") for d in final.values()))
        comprobar("los que fallaron no tienen ultimo respaldo bueno",
                  all("ultimo_ok" not in final[n]
                      for i, n in enumerate(nombres) if i % 4 == 0))

        # Y el archivo que queda tras la tormenta sigue siendo JSON entero.
        try:
            json.loads(paralelo.read_text(encoding="utf-8"))
            entero = True
        except ValueError:
            entero = False
        comprobar("el archivo no queda partido tras escribir desde 40 hilos", entero)

        # 9. Permisos 0600 donde el sistema lo permita. En Windows chmod es casi
        #    simbolico, asi que alli solo se exige que no haya reventado.
        modo = stat.S_IMODE(os.stat(paralelo).st_mode)
        if os.name == "nt":
            comprobar("en Windows basta con que el archivo exista y se lea",
                      paralelo.is_file())
        else:
            comprobar(f"el archivo queda en 0600 (esta en {modo:04o})", modo == 0o600)

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
