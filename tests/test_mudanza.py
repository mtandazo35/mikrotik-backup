"""Mudarse de servidor: empaquetar todo y restaurarlo en la maquina nueva.

Esto se prueba sobre datos DE VERDAD -un repositorio git real, binarios,
inventario, cuentas- porque lo que hay que demostrar no es que el codigo
recorra las piezas, sino que el servidor de destino queda igual que el de
origen. Un paquete que se crea sin quejarse y restaura mal es peor que uno que
falla: se descubre el dia de la mudanza, con el servidor viejo ya apagado.

Los dos riesgos que se persiguen aqui
-------------------------------------
1. Que la copia sea FIEL. No basta con que los archivos aparezcan: se comparan
   sha256 byte a byte, y del repositorio se comprueba ademas que sigue siendo
   un repositorio git con toda su historia. El historico es la razon de ser de
   este programa; restaurarlo como una carpeta de archivos sueltos seria
   perder justo lo que se queria salvar.

2. Que restaurar NO pueda destruir. Son dos peligros distintos:
   - Pisar por accidente un servidor que ya tenia datos. Por eso hace falta
     pedir sobrescribir a proposito, y aun asi lo viejo se aparta en vez de
     borrarse.
   - Que un paquete manipulado escriba donde no debe. Un .tar.gz llega por la
     red o en un pendrive y se restaura como root: un miembro llamado
     '../fuera.txt', una ruta absoluta o un enlace simbolico son la forma
     clasica de convertir "extraer una copia" en "escribir en /etc". Aqui se
     fabrican esos tars a mano y se comprueba que se rechazan Y que no
     quedo nada escrito fuera.

Tambien se comprueba lo aburrido pero necesario: que el paquete nace con
permisos 0600 (lleva las claves SSH de la flota en claro), que un paquete
corrupto se caza por el hash antes de tocar el disco, y que una pieza que
todavia no existe en un servidor recien instalado no impide hacer la copia.

Ejecutar:  python -m tests.test_mudanza
"""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from mkbackup import mudanza
from mkbackup.config import Config, ConfigAlmacen, ConfigBinario
from mkbackup.device import Resultado
from mkbackup.inventory import Equipo
from mkbackup.mudanza import ErrorMudanza
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


def commits(repo: Path) -> int:
    return len([l for l in git(repo, "log", "--oneline").splitlines() if l.strip()])


def sha256(ruta: Path) -> str:
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()


def se_queja(funcion) -> str:
    """Ejecuta y devuelve el mensaje de ErrorMudanza, o '' si no se quejo.

    Devolver el texto y no solo un booleano es a proposito: un mensaje que no
    dice QUE paso obliga a quien restaura a adivinar, y adivinar con el
    servidor viejo ya apagado es exactamente la situacion que hay que evitar.
    """
    try:
        funcion()
    except ErrorMudanza as exc:
        return str(exc)
    return ""


# --- Fabricar servidores de mentira ------------------------------------------

def servidor(base: Path) -> Config:
    """Un Config completo apuntando todo dentro de `base`. No crea nada."""
    base.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        inventario=str(base / "inventory.csv"),
        almacen=ConfigAlmacen(
            git=str(base / "configs.git"),
            binarios=str(base / "binarios"),
            estado=str(base / "estado.json"),
            ajustes=str(base / "ajustes.json"),
            usuarios=str(base / "usuarios.json"),
            auditoria=str(base / "auditoria.log"),
            hechos=str(base / "equipos.json"),
        ),
        binario=ConfigBinario(activo=True, retencion=3),
    )
    cfg.ssh.password = "clave-de-la-flota"
    cfg.ruta = base / "config.yaml"
    return cfg


def poblar(cfg: Config) -> None:
    """Le mete a un servidor todo lo que tendria despues de un tiempo en uso."""
    base = Path(cfg.almacen.estado).parent

    almacen = Almacen(cfg)
    almacen.inicializar()
    # Dos equipos con dos versiones cada uno: asi el repositorio tiene una
    # historia que se pueda contar despues de restaurar, y no un solo commit
    # que cuadraria aunque la copia hubiera perdido todo lo anterior.
    for equipo in (Equipo(nombre="Router-Uno", ip="10.0.0.1", empresa="acme"),
                   Equipo(nombre="Router-Dos", ip="10.0.0.2", empresa="otra")):
        almacen.guardar(Resultado(
            equipo=equipo, binario=b"BINARIO-VIEJO",
            export=f"/ip dns\nset servers=8.8.8.8\n# {equipo.nombre}\n",
        ))
        almacen.guardar(Resultado(
            equipo=equipo, binario=b"BINARIO-NUEVO",
            export=f"/ip dns\nset servers=1.1.1.1\n# {equipo.nombre}\n",
        ))

    Path(cfg.inventario).write_text(
        "nombre,ip,empresa,usuario,password\n"
        "Router-Uno,10.0.0.1,acme,Respaldo,clave-en-claro\n"
        "Router-Dos,10.0.0.2,otra,Respaldo,otra-clave\n",
        encoding="utf-8",
    )
    Path(cfg.almacen.usuarios).write_text(
        json.dumps({"usuarios": [
            {"usuario": "admin", "rol": "admin", "clave_hash": "pbkdf2$de$mentira"},
            {"usuario": "soporte", "rol": "lector", "clave_hash": "otro$hash"},
        ]}, indent=1),
        encoding="utf-8",
    )
    Path(cfg.almacen.hechos).write_text('{"Router-Uno": {"modelo": "hEX"}}',
                                        encoding="utf-8")
    Path(cfg.almacen.estado).write_text('{"fase": "listo"}', encoding="utf-8")
    Path(cfg.almacen.auditoria).write_text(
        "2026-07-28T10:00:00Z admin entro\n", encoding="utf-8")
    Path(cfg.almacen.ajustes).write_text(
        json.dumps({"intervalo_minutos": 60,
                    "fondo_login": (base / "fondo.png").as_posix()}),
        encoding="utf-8",
    )

    # La imagen de fondo: no hace falta que sea una imagen valida, hace falta
    # que sea un archivo binario con un nombre cuya extension importe.
    (base / "fondo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"contenido falso" * 20)
    cfg.web.fondo_login = str(base / "fondo.png")

    # El config.yaml tiene que ser legible de verdad: al restaurar se relee
    # para avisar si apunta a otras rutas que las de esta maquina.
    cfg.ruta.write_text(
        "inventario: \"%s\"\n"
        "almacen:\n"
        "  git: \"%s\"\n"
        "  binarios: \"%s\"\n"
        "  estado: \"%s\"\n"
        "  ajustes: \"%s\"\n"
        "  usuarios: \"%s\"\n"
        "  auditoria: \"%s\"\n"
        "  hechos: \"%s\"\n"
        "ssh:\n"
        "  usuario: Respaldo\n"
        "  password: clave-de-la-flota\n"
        % (Path(cfg.inventario).as_posix(),
           Path(cfg.almacen.git).as_posix(),
           Path(cfg.almacen.binarios).as_posix(),
           Path(cfg.almacen.estado).as_posix(),
           Path(cfg.almacen.ajustes).as_posix(),
           Path(cfg.almacen.usuarios).as_posix(),
           Path(cfg.almacen.auditoria).as_posix(),
           Path(cfg.almacen.hechos).as_posix()),
        encoding="utf-8",
    )


MANIFIESTO_MINIMO = {
    "formato": mudanza.FORMATO,
    "programa": "mkbackup",
    "version": "0.0-prueba",
    "commit": "",
    "cuando": "2026-07-28T00:00:00+00:00",
    "servidor": "el-de-antes",
    "piezas": [{"clave": "usuarios", "nombre": "usuarios.json",
                "carpeta": False, "bytes": 2}],
}


def tar_a_mano(destino: Path, miembros, manifiesto=None) -> Path:
    """Un .tar.gz fabricado a mano, para meter dentro lo que un tar normal no deja."""
    with tarfile.open(destino, "w:gz") as tar:
        if manifiesto is not None:
            crudo = json.dumps(manifiesto).encode("utf-8")
            info = tarfile.TarInfo(mudanza.NOMBRE_MANIFIESTO)
            info.size = len(crudo)
            tar.addfile(info, io.BytesIO(crudo))
        for info, datos in miembros:
            tar.addfile(info, io.BytesIO(datos) if datos is not None else None)
    return destino


def rehacer(origen: Path, destino: Path, cambios: dict) -> Path:
    """Copia un paquete miembro a miembro cambiando el contenido de algunos.

    El manifiesto viejo se copia tal cual: es justo lo que hace un paquete
    corrupto o manipulado, decir una cosa y llevar otra.
    """
    with tarfile.open(origen, "r:gz") as viejo, tarfile.open(destino, "w:gz") as nuevo:
        for miembro in viejo:
            if not miembro.isfile():
                nuevo.addfile(miembro)
                continue
            fuente = viejo.extractfile(miembro)
            datos = fuente.read() if fuente else b""
            if miembro.name in cambios:
                datos = cambios[miembro.name]
                miembro.size = len(datos)
            nuevo.addfile(miembro, io.BytesIO(datos))
    return destino


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        paquetes = raiz / "paquetes"
        paquetes.mkdir()

        origen = servidor(raiz / "origen")
        poblar(origen)
        repo_origen = Path(origen.almacen.git)
        commits_origen = commits(repo_origen)
        archivo_repo = "acme/Router-Uno.rsc"

        # --- Ida: empaquetar --------------------------------------------------
        print("--- Empaquetar el servidor entero ---")
        paquete = paquetes / "mudanza.tar.gz"
        manifiesto = mudanza.empaquetar(origen, paquete)

        comprobar("el paquete existe y no esta vacio",
                  paquete.is_file() and paquete.stat().st_size > 0)
        comprobar("empaquetar devuelve un manifiesto de mkbackup",
                  manifiesto.get("programa") == "mkbackup")
        comprobar(f"con el formato de este programa ({mudanza.FORMATO})",
                  manifiesto.get("formato") == mudanza.FORMATO)

        leido = mudanza.leer_manifiesto(paquete)
        comprobar("leer_manifiesto saca el mismo manifiesto sin extraer nada",
                  leido == manifiesto)

        dentro = {p["nombre"] for p in leido["piezas"]}
        comprobar(f"lleva las piezas esenciales (vio {sorted(dentro)})",
                  {"config.yaml", "inventory.csv", "usuarios.json",
                   "configs.git"} <= dentro)
        comprobar("y tambien los binarios y la imagen de fondo",
                  {"binarios", "fondo.png"} <= dentro)

        # El paquete lleva las claves SSH de la flota EN CLARO. Si nace legible
        # para todo el mundo, da igual lo bien que se guarde despues: ya estuvo
        # ahi. En Windows los permisos POSIX no significan nada, asi que alli no
        # se mira; el servicio corre en Debian.
        if os.name == "nt":
            print("[SALTA] permisos 0600 del paquete (Windows no tiene modo POSIX)")
        else:
            comprobar(
                "el paquete nace con permisos 0600 "
                f"(tiene {oct(paquete.stat().st_mode & 0o777)})",
                paquete.stat().st_mode & 0o777 == 0o600,
            )

        # --- Vuelta: restaurar en una maquina vacia ---------------------------
        print("\n--- Restaurar en un servidor recien instalado ---")
        nuevo = servidor(raiz / "nuevo")
        resultado = mudanza.restaurar(nuevo, paquete)

        comprobar("restaurar dice que puso piezas", len(resultado["puestas"]) > 0)
        comprobar("y no aparto nada, porque no habia nada que apartar",
                  resultado["apartadas"] == [])

        for pieza, ruta_origen, ruta_nueva in (
            ("el inventario", Path(origen.inventario), Path(nuevo.inventario)),
            ("las cuentas del panel", Path(origen.almacen.usuarios),
             Path(nuevo.almacen.usuarios)),
            ("un archivo del historico", repo_origen / archivo_repo,
             Path(nuevo.almacen.git) / archivo_repo),
        ):
            comprobar(f"{pieza} llego al servidor nuevo", ruta_nueva.is_file())
            comprobar(
                f"{pieza} es byte a byte lo mismo",
                ruta_nueva.is_file() and sha256(ruta_nueva) == sha256(ruta_origen),
            )

        comprobar("los binarios tambien viajaron",
                  len(list((Path(nuevo.almacen.binarios)).rglob("*.backup"))) ==
                  len(list((Path(origen.almacen.binarios)).rglob("*.backup"))))

        # Lo que de verdad hay que salvar no son los archivos sino la HISTORIA:
        # un repositorio restaurado sin su .git seria una carpeta de textos, y
        # entonces la mudanza habria perdido justo lo que este programa guarda.
        repo_nuevo = Path(nuevo.almacen.git)
        comprobar("el repositorio restaurado sigue siendo un repositorio git",
                  (repo_nuevo / ".git").is_dir()
                  and "true" in git(repo_nuevo, "rev-parse", "--is-inside-work-tree"))
        comprobar(
            f"y conserva sus {commits_origen} commits "
            f"(cuenta {commits(repo_nuevo)})",
            commits(repo_nuevo) == commits_origen,
        )
        comprobar("las versiones anteriores se pueden sacar de git",
                  "8.8.8.8" in git(repo_nuevo, "show", f"HEAD~1:{archivo_repo}")
                  or "8.8.8.8" in git(repo_nuevo, "log", "-p", "--", archivo_repo))

        # --- Restaurar encima de un servidor con datos ------------------------
        print("\n--- Restaurar encima de datos que ya estaban ---")
        ocupado = servidor(raiz / "ocupado")
        poblar(ocupado)
        marca_vieja = "SOY EL INVENTARIO QUE YA ESTABA\n"
        Path(ocupado.inventario).write_text(marca_vieja, encoding="utf-8")

        aviso = se_queja(lambda: mudanza.restaurar(ocupado, paquete))
        comprobar("sin pedir sobrescribir se niega", bool(aviso))
        comprobar("y dice por que y como seguir",
                  "sobrescribir" in aviso and "ya tiene datos" in aviso)
        comprobar("no toco el inventario que ya estaba",
                  Path(ocupado.inventario).read_text(encoding="utf-8") == marca_vieja)
        comprobar("ni el repositorio que ya estaba",
                  commits(Path(ocupado.almacen.git)) == commits_origen)

        resultado = mudanza.restaurar(ocupado, paquete, sobrescribir=True)
        comprobar("pidiendolo a proposito si restaura",
                  sha256(Path(ocupado.inventario)) == sha256(Path(origen.inventario)))
        comprobar(f"y aparta lo viejo ({len(resultado['apartadas'])} piezas)",
                  len(resultado["apartadas"]) > 0)
        comprobar("todo lo apartado lleva el sufijo que dice el mensaje",
                  all(".antes-de-restaurar" in Path(a).name
                      for a in resultado["apartadas"]))
        comprobar("y lo apartado existe de verdad en el disco",
                  all(Path(a).exists() for a in resultado["apartadas"]))

        viejo_inventario = [a for a in resultado["apartadas"]
                            if Path(a).name.startswith("inventory.csv")]
        comprobar("el inventario de antes se aparto", len(viejo_inventario) == 1)
        comprobar(
            "con su contenido original intacto: equivocarse de paquete se deshace",
            bool(viejo_inventario)
            and Path(viejo_inventario[0]).read_text(encoding="utf-8") == marca_vieja,
        )

        # --- Un paquete que no es el que dice ser -----------------------------
        print("\n--- Paquetes corruptos o manipulados ---")
        victima = servidor(raiz / "victima")
        poblar(victima)
        marca_victima = "ESTE SERVIDOR NO SE PUEDE TOCAR\n"
        Path(victima.inventario).write_text(marca_victima, encoding="utf-8")

        # Un byte distinto y el mismo tamano: si solo se comparara el tamano
        # -o nada- esto pasaria por bueno y el destino se quedaria con un
        # inventario silenciosamente equivocado.
        crudo = Path(origen.inventario).read_bytes()
        tocado = crudo[:5] + bytes([crudo[5] ^ 0x01]) + crudo[6:]
        corrupto = rehacer(paquete, paquetes / "corrupto.tar.gz",
                           {"inventory.csv": tocado})

        aviso = se_queja(lambda: mudanza.restaurar(victima, corrupto,
                                                   sobrescribir=True))
        comprobar("un paquete con un byte cambiado se rechaza", bool(aviso))
        comprobar(f"y dice que pieza no cuadra con el manifiesto ({aviso[:60]!r})",
                  "inventory.csv" in aviso and "manifiesto" in aviso)
        comprobar("el servidor de destino queda intacto",
                  Path(victima.inventario).read_text(encoding="utf-8") == marca_victima)
        comprobar("hasta el repositorio, que ni se empezo a pisar",
                  commits(Path(victima.almacen.git)) == commits_origen)

        # --- Tars que intentan escribir fuera ---------------------------------
        print("\n--- Un tar no puede elegir donde se escribe ---")
        for mala in ("../fuera.txt", "/etc/passwd", "../../fuera.txt"):
            info = tarfile.TarInfo(mala)
            datos = b"colado\n"
            info.size = len(datos)
            malo = tar_a_mano(paquetes / "malo.tar.gz", [(info, datos)],
                              MANIFIESTO_MINIMO)

            blanco = servidor(raiz / "blanco")
            base_blanco = Path(blanco.almacen.estado).parent
            aviso = se_queja(lambda: mudanza.restaurar(blanco, malo))
            comprobar(f"rechaza el miembro {mala!r}", bool(aviso))
            comprobar(f"y lo dice claro ({aviso[:50]!r})",
                      "ruta" in aviso.lower() or "no se acepta" in aviso)
            # Lo importante no es que se queje, es que no llegara a escribir:
            # el mensaje se puede leer despues, el archivo colado no se ve.
            comprobar(f"no escribio nada fuera por {mala!r}",
                      not (base_blanco / "fuera.txt").exists()
                      and not (raiz / "fuera.txt").exists()
                      and not (base_blanco.parent / "fuera.txt").exists())

        enlace = tarfile.TarInfo("usuarios.json")
        enlace.type = tarfile.SYMTYPE
        enlace.linkname = "/etc/shadow"
        enlace.size = 0
        malo = tar_a_mano(paquetes / "enlace.tar.gz", [(enlace, None)],
                          MANIFIESTO_MINIMO)
        blanco = servidor(raiz / "blanco-enlace")
        aviso = se_queja(lambda: mudanza.restaurar(blanco, malo))
        comprobar("un enlace simbolico dentro del paquete se rechaza", bool(aviso))
        comprobar("diciendo que no es un archivo ni una carpeta",
                  "archivo" in aviso and "carpeta" in aviso)
        comprobar("y no dejo el archivo puesto",
                  not Path(blanco.almacen.usuarios).exists())

        # --- Un .tar.gz cualquiera --------------------------------------------
        print("\n--- Un .tar.gz que no es de mkbackup ---")
        info = tarfile.TarInfo("cosas.txt")
        datos = b"un tar de otra cosa\n"
        info.size = len(datos)
        ajeno = tar_a_mano(paquetes / "ajeno.tar.gz", [(info, datos)], None)

        aviso = se_queja(lambda: mudanza.leer_manifiesto(ajeno))
        comprobar("leer_manifiesto se niega con un tar sin manifiesto", bool(aviso))
        comprobar(f"y explica que le falta ({aviso[:70]!r})",
                  mudanza.NOMBRE_MANIFIESTO in aviso and "mkbackup" in aviso)

        blanco = servidor(raiz / "blanco-ajeno")
        aviso = se_queja(lambda: mudanza.restaurar(blanco, ajeno))
        comprobar("y restaurar tampoco lo acepta", bool(aviso))
        comprobar("sin dejar nada escrito",
                  not Path(blanco.inventario).exists())

        aviso = se_queja(lambda: mudanza.leer_manifiesto(paquetes / "no-existe.tar.gz"))
        comprobar("un archivo que no existe se dice tal cual",
                  "no existe" in aviso)

        # --- Detalles ---------------------------------------------------------
        print("\n--- Lo que se cuenta antes de descargar ---")
        cuenta = mudanza.resumen(origen)
        por_nombre = {p["nombre"]: p for p in cuenta["piezas"]}

        comprobar("el resumen mide algo y no cero",
                  cuenta["bytes"] > 0 and cuenta["archivos"] > 0)
        comprobar("ve el inventario que si esta", por_nombre["inventory.csv"]["hay"])
        comprobar("y el historico, contando sus archivos",
                  por_nombre["configs.git"]["hay"]
                  and por_nombre["configs.git"]["archivos"] > 0)
        comprobar("con todo en su sitio no falta nada esencial",
                  cuenta["faltan"] == [])

        # replica.json solo aparece cuando ya hubo una subida al remoto: en un
        # servidor recien instalado no esta, y eso no puede impedir la copia.
        comprobar("una pieza que no existe se ve como ausente",
                  not por_nombre["replica.json"]["hay"])
        comprobar("y no se cuenta como algo que falte",
                  "replica.json" not in cuenta["faltan"])
        comprobar("por eso no va dentro del paquete",
                  "replica.json" not in dentro)

        # Un paquete sin las cuentas del panel deja el servidor nuevo sin nadie
        # con quien entrar. Hay que verlo ANTES de descargar, no despues.
        Path(origen.almacen.usuarios).unlink()
        cuenta = mudanza.resumen(origen)
        comprobar(f"si falta algo esencial se avisa (dijo {cuenta['faltan']})",
                  "usuarios.json" in cuenta["faltan"])
        comprobar("y solo eso: lo demas sigue estando",
                  cuenta["faltan"] == ["usuarios.json"])
        comprobar("aun asi se puede empaquetar lo que si hay",
                  "usuarios.json" not in {
                      p["nombre"] for p in
                      mudanza.empaquetar(origen, paquetes / "incompleto.tar.gz")["piezas"]
                  })

        print("\n--- El nombre que se propone al descargar ---")
        nombre = mudanza.nombre_sugerido()
        comprobar(f"acaba en .tar.gz ({nombre})", nombre.endswith(".tar.gz"))
        comprobar("empieza por el nombre del programa", nombre.startswith("mkbackup-"))
        # El nombre viaja a una cabecera HTTP y a la carpeta de descargas de
        # quien lo pide: un espacio, una comilla o una barra ahi son problema
        # de otro, y ese otro es siempre alguien sin contexto para arreglarlo.
        comprobar("y no lleva ningun caracter raro",
                  all(c.isalnum() or c in "-_." for c in nombre)
                  and "/" not in nombre and "\\" not in nombre)
        comprobar("nunca queda vacio ni sin servidor",
                  len(nombre) > len("mkbackup-.tar.gz"))

        print("\n--- Una carpeta vacia no rompe el paquete ---")
        # Un servidor que todavia no ha guardado ningun .backup tiene la
        # carpeta de binarios creada y vacia. Sin una entrada suya en el tar,
        # el manifiesto la declaraba y el paquete no la traia, asi que al
        # restaurar se leia -con razon- como un paquete truncado: la copia
        # entera se rechazaba por una carpeta que no tenia nada dentro. Es un
        # fallo que solo aparece en instalaciones recien hechas, o sea justo en
        # la primera vez que alguien prueba esto.
        with tempfile.TemporaryDirectory() as tmp_vacio:
            hueco = Path(tmp_vacio)
            origen_v = servidor(hueco / "de")
            poblar(origen_v)
            for basura in Path(origen_v.almacen.binarios).rglob("*"):
                if basura.is_file():
                    basura.unlink()
            for basura in sorted(Path(origen_v.almacen.binarios).rglob("*"),
                                 key=lambda p: -len(p.parts)):
                if basura.is_dir():
                    basura.rmdir()
            comprobar("la carpeta de binarios existe pero esta vacia",
                      Path(origen_v.almacen.binarios).is_dir()
                      and not any(Path(origen_v.almacen.binarios).iterdir()))

            paquete_v = hueco / "vacio.tar.gz"
            mudanza.empaquetar(origen_v, paquete_v)
            destino_v = servidor(hueco / "a")
            hecho_v = mudanza.restaurar(destino_v, paquete_v, sobrescribir=True)
            comprobar("el paquete con la carpeta vacia se restaura igual",
                      len(hecho_v["puestas"]) > 0)
            comprobar("y la carpeta vacia llega al destino",
                      Path(destino_v.almacen.binarios).is_dir())

        print("\n--- El manifiesto tampoco elige donde se escribe ---")
        # Toda la validacion de rutas estaba en los MIEMBROS del tar, pero
        # despues se recorre la lista del manifiesto y con ella se compone la
        # ruta que se mueve. Unir con una ruta absoluta no la mete dentro: la
        # sustituye entera, o sea que `paso / "/etc/shadow"` es `/etc/shadow`.
        # Como no era un miembro del tar no habia nada que extraer, pero el
        # archivo EXISTE en el sistema, y el os.replace de despues lo movia a la
        # carpeta de datos. Corriendo como root eso borra cualquier cosa del
        # disco y ademas se la lleva. Es el peor fallo que ha tenido esto.
        with tempfile.TemporaryDirectory() as tmp_mal:
            malo = Path(tmp_mal)
            victima = malo / "victima.txt"
            victima.write_text("no me toques", encoding="utf-8")
            destino_m = servidor(malo / "destino")

            for etiqueta, nombre_falso in (
                ("una ruta absoluta", str(victima)),
                ("una ruta que sube de carpeta", "../../victima.txt"),
                ("una ruta con barras invertidas", "..\\..\\victima.txt"),
            ):
                trampa = tar_a_mano(
                    malo / f"trampa-{abs(hash(etiqueta))}.tar.gz",
                    miembros=[],
                    manifiesto={
                        "formato": 1, "programa": "mkbackup", "version": "x",
                        "cuando": "", "servidor": "malo",
                        "piezas": [{"clave": "usuarios", "nombre": nombre_falso,
                                    "carpeta": False}],
                    },
                )
                queja = se_queja(
                    lambda: mudanza.restaurar(destino_m, trampa, sobrescribir=True)
                )
                comprobar(f"se rechaza {etiqueta} en el manifiesto ({queja[:40]})",
                          bool(queja))
                comprobar(f"y el archivo de al lado sigue donde estaba ({etiqueta})",
                          victima.is_file()
                          and victima.read_text(encoding="utf-8") == "no me toques")

        print("\n--- Una bomba de descompresion no llena el disco ---")
        # Comprimir ceros sale casi gratis: unos pocos megas de .tar.gz pueden
        # declarar gigas dentro. Sin tope, desempaquetar llena el disco donde
        # vive el repositorio, que es lo que rompe los respaldos. Y no es un
        # susto pasajero: al terminar, esos gigas se mueven a su sitio.
        with tempfile.TemporaryDirectory() as tmp_bomba:
            bomba_dir = Path(tmp_bomba)
            ruta_bomba = bomba_dir / "bomba.tar.gz"
            enorme = 512 * 1024 * 1024
            with tarfile.open(ruta_bomba, "w:gz") as tar:
                manifiesto_bomba = {
                    "formato": 1, "programa": "mkbackup", "version": "x",
                    "cuando": "", "servidor": "malo",
                    # Declara cuatro kilobytes y mete medio giga.
                    "piezas": [{"clave": "git", "nombre": "configs.git",
                                "carpeta": True, "archivos": 1, "bytes": 4096}],
                }
                crudo = json.dumps(manifiesto_bomba).encode("utf-8")
                info = tarfile.TarInfo("manifiesto.json")
                info.size = len(crudo)
                tar.addfile(info, io.BytesIO(crudo))
                relleno = tarfile.TarInfo("configs.git/relleno")
                relleno.size = enorme
                tar.addfile(relleno, io.BytesIO(bytes(enorme)))

            comprimida = ruta_bomba.stat().st_size
            comprobar(f"la bomba comprime muchisimo ({comprimida // 1024} KB "
                      f"-> {enorme // (1024 * 1024)} MB)",
                      comprimida < enorme // 100)
            destino_b = servidor(bomba_dir / "destino")
            queja = se_queja(
                lambda: mudanza.restaurar(destino_b, ruta_bomba, sobrescribir=True)
            )
            comprobar(f"se rechaza antes de escribirla entera ({queja[:45]})",
                      bool(queja))
            # Y no puede haber quedado medio giga tirado por ahi.
            suelto = sum(f.stat().st_size
                         for f in bomba_dir.rglob("*") if f.is_file())
            comprobar(f"y no deja el disco lleno ({suelto // (1024 * 1024)} MB "
                      "sueltos)", suelto < enorme // 4)

        print("\n--- Si falla a mitad, el servidor queda como estaba ---")
        # El peor estado posible no es "no se restauro": es "se restauro la
        # mitad". Con config.yaml ya cambiado y usuarios.json solo con el
        # sufijo, el siguiente arranque siembra una cuenta de administrador con
        # el hash que traiga ESE config.yaml. Una restauracion fallida no puede
        # acabar entregando el panel.
        with tempfile.TemporaryDirectory() as tmp_roto:
            roto = Path(tmp_roto)
            origen_r = servidor(roto / "de")
            poblar(origen_r)
            paquete_r = roto / "copia.tar.gz"
            mudanza.empaquetar(origen_r, paquete_r)

            destino_r = servidor(roto / "a")
            poblar(destino_r)
            antes = {
                p: sha256(Path(p)) for p in
                (destino_r.inventario, destino_r.almacen.usuarios)
                if Path(p).is_file()
            }

            replace_bueno = mudanza.os.replace
            estado = {"n": 0}

            def replace_que_falla(origen, destino):
                estado["n"] += 1
                # Ya se han colocado unas cuantas piezas: justo el punto malo.
                if estado["n"] == 6:
                    raise OSError(18, "Invalid cross-device link")
                return replace_bueno(origen, destino)

            mudanza.os.replace = replace_que_falla
            try:
                queja = se_queja(
                    lambda: mudanza.restaurar(destino_r, paquete_r,
                                              sobrescribir=True)
                )
            finally:
                mudanza.os.replace = replace_bueno

            comprobar(f"la restauracion a medias se cuenta como fallo "
                      f"({queja[:45]})", bool(queja))
            comprobar("y dice que se dejo todo como estaba",
                      "como estaba" in queja or "sufijo" in queja)
            despues = {
                p: sha256(Path(p)) for p in
                (destino_r.inventario, destino_r.almacen.usuarios)
                if Path(p).is_file()
            }
            comprobar("el inventario y las cuentas siguen siendo los de antes",
                      antes == despues)
            # Lo importante de todo: el archivo de cuentas TIENE que existir.
            comprobar("el archivo de cuentas no se quedo desaparecido",
                      Path(destino_r.almacen.usuarios).is_file())

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallidas:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
