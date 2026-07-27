"""Prueba del almacen: git debe commitear SOLO cuando hay cambio real, y cada
empresa debe quedar aislada en su propia carpeta.

No conecta a ningun equipo: fabrica resultados y comprueba el comportamiento
del repositorio.

Ejecutar:  python -m tests.test_almacen
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


def commits(repo: Path) -> int:
    salida = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True,
    )
    return int(salida.stdout.strip() or 0)


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
        base_commits = commits(repo)

        # El arbol es empresa/nombre. El grupo ("bts") es una etiqueta para
        # filtrar la flota y NO aparece en la ruta: cambiar un equipo de grupo
        # no puede mover su archivo ni partir su historico en dos.
        equipo = Equipo(nombre="BTS-Norte-01", ip="10.0.0.1", grupo="bts",
                        empresa="acme")
        config_v1 = "/ip dns\nset servers=8.8.8.8\n"

        # 1. Primer respaldo: debe crear commit
        g1 = almacen.guardar(Resultado(equipo=equipo, export=config_v1, version="7.14",
                                       binario=b"BINARIO-1"))
        comprobar("primer respaldo marca cambio", g1.cambio)
        comprobar("primer respaldo genera commit", commits(repo) == base_commits + 1)
        comprobar("el archivo existe en el repo bajo empresa/nombre",
                  (repo / "acme" / "BTS-Norte-01.rsc").is_file())
        comprobar("el grupo NO crea un nivel en el repo",
                  not (repo / "acme" / "bts").exists())
        comprobar("guarda el binario", g1.binario_guardado)

        # 2. Mismo contenido: NO debe commitear
        g2 = almacen.guardar(Resultado(equipo=equipo, export=config_v1, version="7.14",
                                       binario=b"BINARIO-2"))
        comprobar("respaldo identico NO marca cambio", not g2.cambio)
        comprobar("respaldo identico NO genera commit",
                  commits(repo) == base_commits + 1)
        comprobar("respaldo identico NO guarda binario nuevo", not g2.binario_guardado)

        # 3. Cambio real: debe commitear
        config_v2 = "/ip dns\nset servers=1.1.1.1\n"
        g3 = almacen.guardar(Resultado(equipo=equipo, export=config_v2, version="7.14",
                                       binario=b"BINARIO-3"))
        comprobar("cambio real marca cambio", g3.cambio)
        comprobar("cambio real genera commit", commits(repo) == base_commits + 2)
        comprobar("cambio real tiene hash de commit", bool(g3.commit))

        # 4. El contenido guardado es el nuevo
        guardado = (repo / "acme" / "BTS-Norte-01.rsc").read_text(
            encoding="utf-8")
        comprobar("el archivo contiene la version nueva", "1.1.1.1" in guardado)

        # 5. El historial permite recuperar la version anterior
        antiguo = subprocess.run(
            ["git", "-C", str(repo), "show", "HEAD~1:acme/BTS-Norte-01.rsc"],
            capture_output=True, text=True,
        ).stdout
        comprobar("el historial conserva la version anterior", "8.8.8.8" in antiguo)

        # 6. Retencion de binarios.
        # El arbol de binarios copia al del repo: empresa/nombre, sin grupo. Si
        # no coincidieran, el renombrado no encontraria la carpeta y la
        # retencion rotaria un directorio que nadie usa.
        carpeta = Path(cfg.almacen.binarios) / "acme" / "BTS-Norte-01"
        for i in range(6):
            almacen.guardar_binario(equipo, f"BIN-{i}".encode())
        copias = list(carpeta.glob("*.backup"))
        comprobar(
            f"guardar 6 binarios deja solo los de retencion "
            f"(quedaron {len(copias)})",
            len(copias) == cfg.binario.retencion,
        )
        # El mas reciente es el de nombre mayor (el sello ordena cronologicamente).
        # Comprobarlo por mtime seria fragil: varios archivos escritos en el
        # mismo tic del reloj empatan y el orden queda indefinido.
        reciente = max(copias, key=lambda p: p.name)
        comprobar("conserva el binario mas reciente",
                  reciente.read_bytes() == b"BIN-5")
        comprobar("descarta los mas antiguos",
                  b"BIN-0" not in {p.read_bytes() for p in copias})
        comprobar("el grupo NO crea un nivel en el arbol de binarios",
                  not (Path(cfg.almacen.binarios) / "acme" / "bts").exists())
        comprobar("los binarios NO quedan en la ruta vieja grupo/nombre",
                  not (Path(cfg.almacen.binarios) / "bts" / "BTS-Norte-01").exists())

        # 6b. Rafaga: varios binarios del mismo equipo SIN pausa entre medias.
        #
        # Esto fallaba de verdad, no es una hipotesis: el sello llegaba solo al
        # milisegundo y el reloj de datetime.now() en Windows avanza a saltos de
        # ~15 ms, asi que escrituras seguidas leian la misma hora, generaban el
        # mismo nombre y se pisaban. En produccion eso es perder una copia sin
        # aviso; aqui hacia fallar la retencion 1 de cada ~4 ejecuciones.
        #
        # Se comprueba ANTES de rotar, con la retencion subida: rotando no se
        # ve nada, porque un archivo pisado desaparece de la cuenta igual que
        # uno descartado a proposito y las dos cosas dan el mismo resultado.
        rafaga = Equipo(nombre="Rafaga-01", ip="10.0.0.7", grupo="bts",
                        empresa="acme")
        carpeta_rafaga = Path(cfg.almacen.binarios) / "acme" / "Rafaga-01"
        total = 40
        cfg.binario.retencion = total + 10  # que no rote todavia
        for i in range(total):
            almacen.guardar_binario(rafaga, f"RAFAGA-{i:02d}".encode())

        escritos = sorted(carpeta_rafaga.glob("*.backup"), key=lambda p: p.name)
        comprobar(
            f"{total} binarios seguidos producen {total} archivos, ninguno se "
            f"pisa (hay {len(escritos)})",
            len(escritos) == total,
        )
        comprobar("ningun contenido se perdio por colision de nombre",
                  {p.read_bytes() for p in escritos} ==
                  {f"RAFAGA-{i:02d}".encode() for i in range(total)})
        # Propiedad de la que depende _rotar para elegir la copia mas vieja:
        # ordenar los nombres alfabeticamente tiene que ser ordenarlos por
        # fecha. Un sufijo aleatorio para desempatar la romperia.
        comprobar("ordenar por nombre equivale a ordenar por antiguedad",
                  [p.read_bytes() for p in escritos] ==
                  [f"RAFAGA-{i:02d}".encode() for i in range(total)])

        # Y ahora si, la rotacion cuenta bien sobre esa rafaga.
        cfg.binario.retencion = 3
        almacen.guardar_binario(rafaga, b"RAFAGA-ULTIMO")
        quedan = list(carpeta_rafaga.glob("*.backup"))
        comprobar("tras rotar quedan exactamente las copias de retencion",
                  len(quedan) == 3)
        comprobar("y las que quedan son las tres mas recientes",
                  {p.read_bytes() for p in quedan} ==
                  {b"RAFAGA-38", b"RAFAGA-39", b"RAFAGA-ULTIMO"})

        # 6c. Rafaga CON rotacion actuando entre escritura y escritura. Es un
        # caso distinto del anterior y mas dificil: al rotar se liberan nombres
        # ya usados, y si el sello se dedujera de los archivos que hay en disco
        # una escritura nueva podria reocupar uno de esos huecos y colocarse
        # ANTES que copias mas viejas. Entonces la rotacion siguiente tirarian
        # la copia recien hecha creyendola antigua.
        ciclo = Equipo(nombre="Ciclo-01", ip="10.0.0.8", grupo="bts",
                       empresa="acme")
        carpeta_ciclo = Path(cfg.almacen.binarios) / "acme" / "Ciclo-01"
        for i in range(15):
            almacen.guardar_binario(ciclo, f"CICLO-{i:02d}".encode())
        vivos = {p.read_bytes() for p in carpeta_ciclo.glob("*.backup")}
        comprobar(
            f"rotando en cada escritura sobreviven las 3 ultimas (hay {vivos})",
            vivos == {b"CICLO-12", b"CICLO-13", b"CICLO-14"},
        )

        # 7. Los binarios NO estan en git
        seguimiento = subprocess.run(
            ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True
        ).stdout
        comprobar("los .backup NO entran al repo git",
                  ".backup" not in seguimiento)

        # 8. Equipos sin respaldo
        otro = Equipo(nombre="BTS-Nuevo", ip="10.0.0.9", grupo="bts",
                      empresa="acme")
        pendientes = almacen.equipos_sin_respaldo([equipo, otro])
        comprobar("detecta equipos nunca respaldados",
                  [e.nombre for e in pendientes] == ["BTS-Nuevo"])

        # 9. Aislamiento entre empresas: es el motivo de que la empresa
        # encabece la ruta. Dos clientes del ISP repiten nombres de equipo con
        # toda naturalidad ("Router-Principal" lo tiene todo el mundo). Sin la
        # empresa delante estos dos habrian escrito el MISMO archivo y cada
        # respaldo habria parecido un cambio de configuracion enorme.
        uno = Equipo(nombre="Router-Principal", ip="10.1.0.1", grupo="core",
                     empresa="cliente-uno")
        dos = Equipo(nombre="Router-Principal", ip="10.2.0.1", grupo="core",
                     empresa="cliente-dos")
        config_uno = "/system identity\nset name=CLIENTE-UNO\n"
        config_dos = "/system identity\nset name=CLIENTE-DOS\n"

        g_uno = almacen.guardar(Resultado(equipo=uno, export=config_uno,
                                          binario=b"BIN-UNO"))
        g_dos = almacen.guardar(Resultado(equipo=dos, export=config_dos,
                                          binario=b"BIN-DOS"))
        comprobar("equipo homonimo de otra empresa se ve como alta, no como cambio",
                  g_uno.cambio and g_dos.cambio)

        archivo_uno = repo / "cliente-uno" / "Router-Principal.rsc"
        archivo_dos = repo / "cliente-dos" / "Router-Principal.rsc"
        comprobar("cada empresa tiene su propio archivo en el repo",
                  archivo_uno.is_file() and archivo_dos.is_file())
        comprobar("el equipo de cliente-uno conserva SU configuracion",
                  "CLIENTE-UNO" in archivo_uno.read_text(encoding="utf-8"))
        comprobar("el equipo de cliente-dos conserva SU configuracion",
                  "CLIENTE-DOS" in archivo_dos.read_text(encoding="utf-8"))

        bin_uno = Path(cfg.almacen.binarios) / "cliente-uno" / "Router-Principal"
        bin_dos = Path(cfg.almacen.binarios) / "cliente-dos" / "Router-Principal"
        comprobar("los binarios de cada empresa van a carpetas distintas",
                  bin_uno.is_dir() and bin_dos.is_dir())
        comprobar("el binario de cliente-uno no lo pisa el de cliente-dos",
                  {p.read_bytes() for p in bin_uno.glob("*.backup")} == {b"BIN-UNO"})

        # El historial de un cliente se saca solo, que es para lo que se
        # metio la empresa en la ruta.
        historial = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline", "--", "cliente-uno/"],
            capture_output=True, text=True,
        ).stdout
        comprobar("git log por empresa devuelve solo lo de esa empresa",
                  len(historial.strip().splitlines()) == 1)
        comprobar("el mensaje de commit identifica a la empresa",
                  "cliente-uno" in historial)

        # El mensaje de commit reproduce la ruta real: si siguiera nombrando el
        # grupo, un `git log --oneline` describiria archivos que no existen.
        comprobar("el mensaje de commit no menciona el grupo",
                  "/core/" not in historial)

        # 10. Equipo sin empresa declarada: cae en la carpeta por defecto,
        # nunca en la raiz del repo (ahi se mezclaria con las empresas).
        huerfano = Equipo(nombre="AP-Suelto", ip="10.9.9.9", grupo="aps")
        almacen.guardar(Resultado(equipo=huerfano, export="/interface print\n"))
        comprobar("equipo sin empresa va a la carpeta por defecto",
                  (repo / "sin-empresa" / "AP-Suelto.rsc").is_file())

        # 11. Renombrado: el equipo se dio de alta con la IP como nombre
        # provisional y el primer respaldo revelo su identidad. Tiene que
        # moverse el .rsc conservando el historico Y la carpeta de binarios,
        # que se deriva de la misma ruta.
        provisional = Equipo(nombre="10.5.0.1", ip="10.5.0.1", grupo="core",
                             empresa="acme")
        definitivo = Equipo(nombre="Router-Sur", ip="10.5.0.1", grupo="core",
                            empresa="acme")
        almacen.guardar(Resultado(equipo=provisional, export="/ip dns\nset servers=9.9.9.9\n",
                                  binario=b"BIN-PROVISIONAL"))
        movido = almacen.renombrar(provisional.ruta_relativa, definitivo.ruta_relativa)
        comprobar("el renombrado se commitea", movido)
        comprobar("el .rsc esta en la ruta nueva",
                  (repo / "acme" / "Router-Sur.rsc").is_file()
                  and not (repo / "acme" / "10.5.0.1.rsc").exists())

        # git log --follow es lo que hace que el renombrado no corte el
        # historico del equipo; sin el, los meses anteriores dejarian de verse.
        seguido = subprocess.run(
            ["git", "-C", str(repo), "log", "--follow", "--oneline", "--",
             "acme/Router-Sur.rsc"],
            capture_output=True, text=True,
        ).stdout
        comprobar("el historico sobrevive al renombrado",
                  len(seguido.strip().splitlines()) >= 2)

        bin_nuevo = Path(cfg.almacen.binarios) / "acme" / "Router-Sur"
        comprobar("los binarios acompanan al renombrado",
                  bin_nuevo.is_dir()
                  and not (Path(cfg.almacen.binarios) / "acme" / "10.5.0.1").exists())
        # El prefijo tambien se renombra: _rotar ordena por nombre y dos
        # prefijos conviviendo en la carpeta romperian ese orden.
        comprobar("los binarios movidos llevan el prefijo nuevo",
                  [p.read_bytes() for p in bin_nuevo.glob("Router-Sur_*.backup")]
                  == [b"BIN-PROVISIONAL"])

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallidas:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
