"""Prueba del sondeo de nombres: preguntarle a la flota como se llama.

No conecta a ningun router. Sustituye device.identidad por una funcion que
responde lo que diga un diccionario, y a partir de ahi todo lo demas es de
verdad: el CSV del inventario, el repositorio de git y el archivo de hechos.

Lo que hay que dejar demostrado, en orden de gravedad:

  1. Renombrar NO puede perder las credenciales del equipo. Este es el fallo
     que motivo el archivo: el renombrado automatico que ya existia llamaba a
     validar_equipo sin pasarle usuario, clave ni intervalo, asi que un router
     con clave propia se quedaba sin ella el dia que se renombraba solo. El
     siguiente ciclo fallaba autenticando, un ciclo despues y en otro sitio,
     que es la forma mas cara de enterarse.
  2. Renombrar NO puede perder el historial. El archivo se mueve con git mv y
     'git log --follow' tiene que seguir viendo lo de antes.
  3. Dos equipos NO pueden acabar con el mismo nombre. Comparten archivo en el
     repo y se pisan los respaldos.

Ejecutar:  python -m tests.test_identidades
"""

import subprocess
import tempfile
import threading
import time
from pathlib import Path

from mkbackup import identidades
from mkbackup.config import Config, ConfigAlmacen
from mkbackup.hechos import Hechos
from mkbackup.inventory import Equipo, cargar, guardar
from mkbackup.store import Almacen

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def git(repo: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    ).stdout.strip()


class RouterFalso:
    """Sustituye a device.identidad. Devuelve lo que diga el mapa, por IP.

    Se instala sobre el modulo device de verdad, que es como lo busca
    identidades.py (import perezoso dentro de la funcion). Asi se prueba el
    camino real y no una version de mentira del modulo.
    """

    def __init__(self, respuestas: dict):
        self.respuestas = respuestas
        self.preguntados = []
        self.candado = threading.Lock()

    def __call__(self, equipo, cfg) -> str:
        with self.candado:
            self.preguntados.append(equipo.nombre)
        respuesta = self.respuestas.get(equipo.ip, "")
        if respuesta == "__lento__":
            time.sleep(0.4)
            return ""
        return respuesta


def montar(base: Path, equipos, respuestas):
    cfg = Config(
        almacen=ConfigAlmacen(
            git=str(base / "configs.git"),
            binarios=str(base / "binarios"),
            hechos=str(base / "equipos.json"),
        ),
        ssh=Config().ssh,
    )
    cfg.ssh.password = "x"
    cfg.inventario = str(base / "inventory.csv")
    cfg.concurrencia = 4

    guardar(cfg.inventario, equipos)
    almacen = Almacen(cfg)
    almacen.inicializar()

    # Un respaldo por equipo, para que haya historial que mover. Sin esto el
    # renombrado no tendria archivo que mover y la comprobacion del historico
    # pasaria por no haber nada que perder.
    from mkbackup.device import Resultado

    for equipo in equipos:
        almacen.guardar(Resultado(
            equipo=equipo, export=f"/system identity\nset name={equipo.nombre}\n",
            version="7.14",
        ))

    from mkbackup import device

    falso = RouterFalso(respuestas)
    device.identidad = falso
    return cfg, almacen, falso


def esperar(sondeo, segundos=20.0) -> bool:
    limite = time.monotonic() + segundos
    while sondeo.corriendo and time.monotonic() < limite:
        time.sleep(0.02)
    return not sondeo.corriendo


def main() -> None:
    print("--- Quien es provisional ---")
    comprobar("un equipo llamado como su IPv4 lo es",
              identidades.es_provisional(Equipo(nombre="10.0.0.1", ip="10.0.0.1")))
    comprobar("una IPv6 tambien, con los dos puntos cambiados por guiones",
              identidades.es_provisional(
                  Equipo(nombre="2001-db8--1", ip="2001:db8::1")))
    comprobar("y uno con nombre puesto NO lo es",
              not identidades.es_provisional(Equipo(nombre="Core-Quito", ip="10.0.0.1")))

    flota = [
        Equipo(nombre="10.0.0.1", ip="10.0.0.1", empresa="Acme"),
        Equipo(nombre="Core-Quito", ip="10.0.0.2", empresa="Acme"),
    ]
    comprobar("por defecto solo se pregunta a los que no tienen nombre",
              [e.nombre for e in identidades.a_quien_preguntar(
                  flota, identidades.SOLO_SIN_NOMBRE)] == ["10.0.0.1"])
    comprobar("y con 'todos' se pregunta a todos",
              len(identidades.a_quien_preguntar(flota, identidades.TODOS)) == 2)

    print("\n--- Renombrar conserva lo que no se pidio cambiar ---")
    # ESTE es el fallo por el que existe el archivo.
    equipo = Equipo(
        nombre="10.0.0.9", ip="10.0.0.9", puerto=2222, grupo="bts",
        empresa="Cliente Sur", intervalo_minutos=720,
        usuario="admin-cliente", clave="clave-solo-de-este",
    )
    nuevo, errores = identidades.renombrado(equipo, "Router-Sur-01", [equipo])
    comprobar(f"el renombrado se acepta ({errores})", nuevo is not None)
    if nuevo is not None:
        comprobar("cambia el nombre", nuevo.nombre == "Router-Sur-01")
        comprobar("y NO se lleva por delante el usuario propio",
                  nuevo.usuario == "admin-cliente")
        comprobar("ni la clave propia", nuevo.clave == "clave-solo-de-este")
        comprobar("ni el intervalo propio", nuevo.intervalo_minutos == 720)
        comprobar("ni el puerto", nuevo.puerto == 2222)
        comprobar("ni el grupo ni la empresa",
                  nuevo.grupo == "bts" and nuevo.empresa == "Cliente Sur")

    otro = Equipo(nombre="Router-Sur-01", ip="10.0.0.10", empresa="Cliente Sur")
    choque, errores = identidades.renombrado(equipo, "Router-Sur-01", [equipo, otro])
    comprobar("un nombre que ya ocupa otro equipo se rechaza", choque is None)
    comprobar(f"y se dice por que ({'; '.join(errores)})", bool(errores))

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        flota = [
            # Contesta con un nombre nuevo: se renombra.
            Equipo(nombre="10.0.0.1", ip="10.0.0.1", empresa="Acme",
                   usuario="propio", clave="secreta", intervalo_minutos=1440),
            # Contesta lo mismo que ya tiene: no se toca.
            Equipo(nombre="Ya-Se-Llama-Asi", ip="10.0.0.2", empresa="Acme"),
            # No contesta: se queda como esta.
            Equipo(nombre="10.0.0.3", ip="10.0.0.3", empresa="Acme"),
            # Dice llamarse como otro que ya existe: se rechaza.
            Equipo(nombre="10.0.0.4", ip="10.0.0.4", empresa="Acme"),
        ]
        respuestas = {
            "10.0.0.1": "Core-Guayaquil",
            "10.0.0.2": "Ya-Se-Llama-Asi",
            "10.0.0.3": "",
            "10.0.0.4": "Ya-Se-Llama-Asi",
        }
        cfg, almacen, falso = montar(base, flota, respuestas)
        repo = Path(cfg.almacen.git)
        hechos = Hechos(cfg.almacen.hechos)
        hechos.anotar("10.0.0.1", modelo="RB4011", version="7.14")

        antes = git(repo, "rev-list", "--count", "HEAD")

        print("\n--- El sondeo completo ---")
        identidades.olvidar()
        sondeo = identidades.lanzar(
            cfg, threading.Lock(), hechos, flota, "tester", identidades.TODOS
        )
        comprobar("arranca", sondeo is not None)
        comprobar("terminó", esperar(sondeo))

        d = sondeo.instantanea()
        comprobar(f"se pregunto a los cuatro (dio {d['preguntados']})",
                  d["preguntados"] == 4)
        comprobar(f"uno renombrado (dio {d['renombrados']})", d["renombrados"] == 1)
        comprobar(f"uno ya estaba bien (dio {d['iguales']})", d["iguales"] == 1)
        comprobar(f"uno sin respuesta (dio {d['mudos']})", d["mudos"] == 1)
        comprobar(f"uno rechazado (dio {d['rechazados']})", d["rechazados"] == 1)
        comprobar("y se dice cual y por que",
                  d["detalle_rechazados"] and d["detalle_rechazados"][0][0] == "10.0.0.4")
        comprobar("no quedo corriendo", not d["corriendo"])
        comprobar("sin errores", not d["error"])

        print("\n--- Lo que quedo en disco ---")
        guardados = {e.nombre: e for e in cargar(cfg.inventario)[0]}
        comprobar("el equipo aparece con su nombre nuevo",
                  "Core-Guayaquil" in guardados)
        comprobar("y el provisional ya no esta", "10.0.0.1" not in guardados)
        renombrado = guardados.get("Core-Guayaquil")
        if renombrado is not None:
            # La razon de ser de todo esto.
            comprobar("conserva su usuario propio", renombrado.usuario == "propio")
            comprobar("conserva su clave propia", renombrado.clave == "secreta")
            comprobar("conserva su intervalo", renombrado.intervalo_minutos == 1440)
        comprobar("el que no contesto sigue como estaba", "10.0.0.3" in guardados)
        comprobar("el que choco tambien", "10.0.0.4" in guardados)
        comprobar("y siguen siendo cuatro", len(guardados) == 4)

        print("\n--- El historial se mueve con el equipo ---")
        nueva = repo / "acme" / "Core-Guayaquil.rsc"
        comprobar("el archivo esta en su ruta nueva", nueva.is_file())
        comprobar("y ya no en la vieja", not (repo / "acme" / "10.0.0.1.rsc").exists())
        seguido = git(repo, "log", "--follow", "--format=%H", "--",
                      "acme/Core-Guayaquil.rsc")
        comprobar(f"--follow ve el respaldo de antes del renombrado "
                  f"({len(seguido.splitlines())} commits)",
                  len(seguido.splitlines()) >= 2)
        comprobar("el renombrado dejo su propio commit",
                  int(git(repo, "rev-list", "--count", "HEAD")) > int(antes))

        comprobar("lo observado del equipo viaja con el nombre nuevo",
                  hechos.leer().get("Core-Guayaquil", {}).get("modelo") == "RB4011")
        comprobar("y no se queda una copia con el nombre viejo",
                  "10.0.0.1" not in hechos.leer())

        print("\n--- No se solapan dos sondeos ---")
        identidades.olvidar()
        lentos = [Equipo(nombre=f"10.1.0.{i}", ip=f"10.1.0.{i}", empresa="Acme")
                  for i in range(1, 5)]
        guardar(cfg.inventario, lentos)
        falso.respuestas = {e.ip: "__lento__" for e in lentos}
        primero = identidades.lanzar(
            cfg, threading.Lock(), hechos, lentos, "tester", identidades.TODOS
        )
        segundo = identidades.lanzar(
            cfg, threading.Lock(), hechos, lentos, "otro", identidades.TODOS
        )
        comprobar("el primero arranca", primero is not None)
        comprobar("el segundo NO: uno cada vez", segundo is None)
        comprobar("en_curso lo ve mientras corre", identidades.en_curso() is not None)
        esperar(primero)
        comprobar("y cuando termina ya no hay ninguno en curso",
                  identidades.en_curso() is None)
        comprobar("pero el ultimo se sigue pudiendo consultar",
                  identidades.ultimo() is not None)

        print("\n--- Un equipo que aun no tiene respaldo SI se renombra ---")
        # Es el caso para el que existe el boton: alta con el router apagado,
        # queda con la IP de nombre y sin archivo en el repo. Almacen.renombrar
        # devuelve False cuando no hay origen, igual que cuando el mv falla de
        # verdad, asi que sin distinguir los dos casos este equipo se rechazaba
        # en todos los sondeos, para siempre, con "no se pudo mover su
        # historico" - hablando de un historico que no existe.
        identidades.olvidar()
        virgen = Equipo(nombre="10.3.0.1", ip="10.3.0.1", empresa="Acme",
                        usuario="propio", clave="secreta")
        guardar(cfg.inventario, [virgen])
        comprobar("de partida no tiene archivo en el repo",
                  not (repo / "acme" / "10.3.0.1.rsc").exists())
        falso.respuestas = {"10.3.0.1": "Recien-Instalado"}
        sondeo = identidades.lanzar(
            cfg, threading.Lock(), hechos, [virgen], "tester", identidades.TODOS
        )
        esperar(sondeo)
        d = sondeo.instantanea()
        comprobar(f"se renombra igual (dio {d['renombrados']} renombrados, "
                  f"{d['rechazados']} rechazados)", d["renombrados"] == 1)
        guardados = {e.nombre: e for e in cargar(cfg.inventario)[0]}
        comprobar("y queda en el inventario con su nombre",
                  "Recien-Instalado" in guardados)
        comprobar("sin perder su clave propia",
                  guardados.get("Recien-Instalado", virgen).clave == "secreta")

        print("\n--- Si el equipo cambio mientras se le preguntaba, no se toca ---")
        # El nombre provisional de un equipo es SU IP. Entre preguntar y aplicar
        # pasan minutos: si dan de baja ese equipo y dan de alta otro, de otro
        # cliente, que entre tambien sin nombre, la fila se llama igual. Buscar
        # solo por nombre le pondria a un router el nombre que dijo OTRO, y
        # ademas en una empresa que quien lanzo el sondeo no puede tocar.
        identidades.olvidar()
        preguntado = Equipo(nombre="10.4.0.1", ip="10.4.0.1", empresa="Acme")
        # Lo que hay en el inventario cuando llega el turno es otro aparato.
        suplantador = Equipo(nombre="10.4.0.1", ip="10.4.0.1", empresa="Otro Cliente")
        guardar(cfg.inventario, [suplantador])
        falso.respuestas = {"10.4.0.1": "Nombre-Del-Otro"}
        sondeo = identidades.lanzar(
            cfg, threading.Lock(), hechos, [preguntado], "tester", identidades.TODOS
        )
        esperar(sondeo)
        d = sondeo.instantanea()
        comprobar("no se renombra", d["renombrados"] == 0 and d["rechazados"] == 1)
        comprobar("y se dice que cambio, no otra cosa",
                  "cambio" in d["detalle_rechazados"][0][1])
        comprobar("el equipo del otro cliente sigue como estaba",
                  [e.nombre for e in cargar(cfg.inventario)[0]] == ["10.4.0.1"])

        print("\n--- El motivo de un choque no se le cuenta a cualquiera ---")
        # Decir "ya existe un equipo llamado X" confirma que ese equipo existe.
        # A quien no alcanza a la flota entera, eso es informacion del cliente
        # de al lado: misma razon por la que el panel contesta 404 y no 403.
        identidades.olvidar()
        dos = [Equipo(nombre="10.5.0.1", ip="10.5.0.1", empresa="Acme"),
               Equipo(nombre="Ocupado", ip="10.5.0.2", empresa="Acme")]
        guardar(cfg.inventario, dos)
        falso.respuestas = {"10.5.0.1": "Ocupado", "10.5.0.2": "Ocupado"}
        sondeo = identidades.lanzar(
            cfg, threading.Lock(), hechos, dos[:1], "juan", identidades.TODOS,
            detallado=False,
        )
        esperar(sondeo)
        motivo = sondeo.instantanea()["detalle_rechazados"][0][1]
        comprobar(f"a quien solo ve su empresa se le dice lo justo ({motivo!r})",
                  "Ocupado" not in motivo)

        identidades.olvidar()
        sondeo = identidades.lanzar(
            cfg, threading.Lock(), hechos, dos[:1], "admin", identidades.TODOS,
            detallado=True,
        )
        esperar(sondeo)
        motivo = sondeo.instantanea()["detalle_rechazados"][0][1]
        comprobar("y a quien ve la flota entera se le dice cual es",
                  "Ocupado" in motivo)

        print("\n--- El detalle se corta, las cuentas no ---")
        # Con una flota grande, la lista de los que no contestaron seria una
        # pared de texto; la CIFRA tiene que seguir siendo la de verdad, porque
        # es la que se mira para saber si el sondeo sirvio de algo.
        sondeo = identidades.Sondeo(quien="x", alcance="todos", total=200)
        for i in range(200):
            sondeo.mudo(f"equipo-{i}")
        d = sondeo.instantanea()
        comprobar(f"la cuenta va entera (dio {d['mudos']})", d["mudos"] == 200)
        comprobar(f"y el detalle se corta en {identidades.MAXIMO_DETALLE}",
                  len(d["detalle_mudos"]) == identidades.MAXIMO_DETALLE)

        print("\n--- Un equipo borrado a media consulta no rompe nada ---")
        identidades.olvidar()
        vivos = [Equipo(nombre="10.2.0.1", ip="10.2.0.1", empresa="Acme")]
        guardar(cfg.inventario, [])  # el inventario ya no lo tiene
        falso.respuestas = {"10.2.0.1": "Nombre-Nuevo"}
        sondeo = identidades.lanzar(
            cfg, threading.Lock(), hechos, vivos, "tester", identidades.TODOS
        )
        comprobar("termina igual", esperar(sondeo))
        d = sondeo.instantanea()
        comprobar("y lo cuenta como rechazado en vez de caerse",
                  d["rechazados"] == 1 and not d["error"])

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print("  -", f)
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
