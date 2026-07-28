"""Borrar los datos de un equipo dado de baja: las dos formas, sobre git de verdad.

Dar de baja no borra los respaldos, y esta bien que sea asi. Pero un cliente
que se va puede pedir que no quede nada suyo, y ahi "borrado" tiene que
significar borrado: si el archivo sigue en un commit anterior, el dato sigue
estando y quien lo prometio mintio.

Por eso esto no se puede probar mirando el arbol de trabajo. Se fabrica un
repositorio git real en un directorio temporal y se le pregunta a git, que es
el unico que sabe lo que guarda:

  - retirar: el archivo desaparece de hoy pero `git log --follow` sigue viendo
    sus versiones anteriores. Es la opcion que se puede deshacer.
  - purgar: no queda en NINGUNA version (`git log --all` vacio) ni entre los
    objetos alcanzables (`git rev-list --objects --all`).
  - y purgar tiene que llevarse SOLO lo suyo: reescribir la historia toca
    todos los commits del repositorio, asi que hay que comprobar que el equipo
    de al lado conserva las suyas.

Ejecutar:  python -m tests.test_datos
"""

import subprocess
import tempfile
from pathlib import Path

from mkbackup.config import Config, ConfigAlmacen, ConfigBinario
from mkbackup.device import Resultado
from mkbackup.inventory import Equipo
from mkbackup.store import Almacen

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def binarios_de(cfg, ruta_sin_extension: str) -> int:
    carpeta = Path(cfg.almacen.binarios) / ruta_sin_extension
    return len(list(carpeta.glob("*.backup"))) if carpeta.is_dir() else 0


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = Config(
            almacen=ConfigAlmacen(
                git=str(base / "configs.git"), binarios=str(base / "binarios")
            ),
            binario=ConfigBinario(activo=True, retencion=3),
            ssh=Config().ssh,
        )
        cfg.ssh.password = "x"  # solo para pasar la validacion

        almacen = Almacen(cfg)
        almacen.inicializar()
        repo = Path(cfg.almacen.git)

        # Cuatro equipos: uno sigue de alta y los otros tres se daran de baja.
        # Cada uno con DOS versiones, que es lo que hace visible la diferencia
        # entre quitar el archivo y borrar su historia.
        activo = Equipo(nombre="Router-Activo", ip="10.0.0.1", empresa="acme")
        retirado = Equipo(nombre="Router-Retirado", ip="10.0.0.2", empresa="acme")
        purgado = Equipo(nombre="Router-Purgado", ip="10.0.0.3", empresa="acme")
        vecino = Equipo(nombre="Router-Vecino", ip="10.0.0.4", empresa="otra")

        for equipo in (activo, retirado, purgado, vecino):
            almacen.guardar(Resultado(
                equipo=equipo, export=f"/ip dns\nset servers=8.8.8.8\n# {equipo.nombre}\n",
                binario=b"BIN-1",
            ))
            almacen.guardar(Resultado(
                equipo=equipo, export=f"/ip dns\nset servers=1.1.1.1\n# {equipo.nombre}\n",
                binario=b"BIN-2",
            ))

        # --- huerfanos: lo que quedo de quien ya no esta en el inventario ---
        print("--- Que hay guardado de equipos que ya no estan de alta ---")
        sueltos = almacen.huerfanos([activo])
        nombres = sorted(h["nombre"] for h in sueltos)
        comprobar(
            f"encuentra los que NO estan en el inventario (vio {nombres})",
            nombres == ["Router-Purgado", "Router-Retirado", "Router-Vecino"],
        )
        comprobar("y no ofrece borrar el que SI esta de alta",
                  "Router-Activo" not in nombres)

        ficha = next(h for h in sueltos if h["nombre"] == "Router-Retirado")
        comprobar("la ruta es la del repositorio, empresa incluida",
                  ficha["ruta"] == "acme/Router-Retirado.rsc")
        comprobar("dice de que carpeta de empresa es",
                  ficha["empresa_carpeta"] == "acme")
        comprobar(f"cuenta sus versiones en git (dijo {ficha['versiones']})",
                  ficha["versiones"] == 2)
        comprobar(f"cuenta sus binarios en disco (dijo {ficha['binarios']})",
                  ficha["binarios"] == 2)
        comprobar("y sabe cuando se toco por ultima vez",
                  bool(ficha["ultimo"]) and ficha["ultimo"][:2] == "20")

        # Con el inventario entero no sobra nada: es la comprobacion de que la
        # lista se calcula contra ruta_relativa y no contra cualquier cosa.
        comprobar("con toda la flota de alta la lista queda vacia",
                  almacen.huerfanos([activo, retirado, purgado, vecino]) == [])

        # --- retirar: se quita de hoy, la historia se queda ------------------
        print("\n--- Quitarlo del repositorio (se puede recuperar) ---")
        ruta_retirado = "acme/Router-Retirado.rsc"
        comprobar("retirar dice que lo hizo", almacen.retirar(ruta_retirado))
        comprobar("el archivo ya no esta en el arbol de trabajo",
                  not (repo / ruta_retirado).exists())
        comprobar("ni lo sigue git",
                  ruta_retirado not in git(repo, "ls-files"))

        seguido = git(repo, "log", "--follow", "--format=%H", "--", ruta_retirado)
        comprobar(
            f"pero sus versiones anteriores siguen ahi "
            f"({len(seguido.split())} commits)",
            len(seguido.split()) >= 2,
        )
        antiguo = git(repo, "show", f"HEAD~1:{ruta_retirado}")
        comprobar("y se puede sacar el contenido de una de ellas",
                  "1.1.1.1" in antiguo)
        comprobar("los binarios si se van del disco",
                  binarios_de(cfg, "acme/Router-Retirado") == 0)

        # --- purgar: no queda en ninguna version -----------------------------
        print("\n--- Borrarlo tambien del historial (irreversible) ---")
        ruta_purgado = "acme/Router-Purgado.rsc"
        comprobar("purgar dice que lo hizo", almacen.purgar(ruta_purgado))
        comprobar("el archivo ya no esta en el arbol de trabajo",
                  not (repo / ruta_purgado).exists())

        todas = git(repo, "log", "--all", "--format=%H", "--", ruta_purgado)
        comprobar(f"NINGUNA version lo menciona (quedaron {len(todas.split())})",
                  todas.strip() == "")

        objetos = git(repo, "rev-list", "--objects", "--all")
        comprobar("y su nombre no aparece entre los objetos del repositorio",
                  "Router-Purgado" not in objetos)
        comprobar("no quedan referencias de respaldo de filter-branch",
                  git(repo, "for-each-ref", "--format=%(refname)",
                      "refs/original").strip() == "")
        comprobar("los binarios tambien se van",
                  binarios_de(cfg, "acme/Router-Purgado") == 0)

        # --- y sin llevarse por delante a los demas --------------------------
        print("\n--- Reescribir la historia no puede tocar al equipo de al lado ---")
        ruta_vecino = "otra/Router-Vecino.rsc"
        vecinas = git(repo, "log", "--format=%H", "--", ruta_vecino)
        comprobar(
            f"el otro equipo conserva sus versiones (tiene {len(vecinas.split())})",
            len(vecinas.split()) == 2,
        )
        comprobar("y su archivo sigue en el arbol de trabajo",
                  (repo / ruta_vecino).is_file())
        comprobar("con el contenido de siempre",
                  "1.1.1.1" in (repo / ruta_vecino).read_text(encoding="utf-8"))
        comprobar("el equipo que sigue de alta tampoco se movio",
                  (repo / "acme" / "Router-Activo.rsc").is_file())
        comprobar("y su historia entera sigue accesible",
                  "8.8.8.8" in git(repo, "show",
                                   "HEAD~1:acme/Router-Activo.rsc")
                  or "8.8.8.8" in git(repo, "log", "-p", "--",
                                      "acme/Router-Activo.rsc"))
        comprobar("los binarios del vecino no se tocaron",
                  binarios_de(cfg, "otra/Router-Vecino") == 2)

        # Y despues de purgar, la lista ya no lo ofrece.
        restantes = sorted(h["nombre"] for h in almacen.huerfanos([activo]))
        comprobar(f"lo borrado desaparece de la lista (queda {restantes})",
                  restantes == ["Router-Vecino"])

        # --- Rutas que no se aceptan -----------------------------------------
        print("\n--- Una ruta que viene de fuera no puede salirse del repo ---")
        # purgar interpola la ruta en una cadena que ejecuta un shell: si esto
        # dejara pasar algo, seria una orden mas corriendo como el usuario del
        # servicio, que en este despliegue es root.
        for mala in ("../fuera.rsc", "/etc/passwd", "-ayuda", "",
                     "acme/x'; touch colado.rsc; echo '.rsc"):
            comprobar(f"retirar rechaza {mala!r}", not almacen.retirar(mala))
            comprobar(f"purgar rechaza {mala!r}", not almacen.purgar(mala))
        comprobar("y no se ejecuto nada por el camino",
                  not (repo / "colado.rsc").exists())
        comprobar("el vecino sigue intacto despues de todo eso",
                  (repo / ruta_vecino).is_file())

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallidas:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
