"""Prueba de la subida de los respaldos a un repositorio git remoto.

Lo que mas se vigila aqui es el TOKEN. La credencial del remoto es lo unico
de este proyecto que, si se escapa, le abre a un tercero el repositorio con
las configuraciones de todos los clientes -y con sus secretos dentro, si
export.mostrar_secretos esta puesto-. Los dos sitios por los que se escapa un
token de verdad son el mensaje de error de la operacion que lo usa (que acaba
en el log del servicio, en el panel y en el aviso de Telegram) y el
.git/config del repositorio (que viaja en cada copia). Se comprueban los dos.

Lo segundo que se vigila es que un push NO se cuelgue: un servicio desatendido
que se queda esperando una contrasena que nadie va a teclear no falla, se
queda parado, y eso no lo ve nadie hasta que hacen falta las copias.

No hace falta red: el remoto es un `git init --bare` en otro directorio
temporal, asi que el push que se prueba es un push de verdad.

Ejecutar:  python -m tests.test_remoto
"""

import base64
import json
import subprocess
import tempfile
from pathlib import Path

from mkbackup import cli
from mkbackup.config import AJUSTES_EDITABLES, Config, ConfigAlmacen, ConfigBinario
from mkbackup.device import Resultado
from mkbackup.inventory import Equipo
from mkbackup.store import (
    Almacen,
    ErrorAlmacen,
    anotar_replica,
    leer_replica,
    ruta_replica,
)

FALLOS = []

# Reconocible a proposito: si esta cadena aparece en cualquier texto que salga
# del programa, la prueba lo caza sin ambiguedad.
TOKEN = "tok-SECRETO-12345"
USUARIO = "manuel"


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def git(repo: Path, *args: str) -> str:
    salida = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return salida.stdout


def crear_bare(ruta: Path) -> str:
    """Un repositorio vacio que hace de remoto. Devuelve su URL (la ruta)."""
    subprocess.run(["git", "init", "--bare", "-q", str(ruta)],
                   capture_output=True, text=True)
    return str(ruta)


def commits(repo: Path) -> int:
    """Commits alcanzables desde CUALQUIER rama.

    En el bare no se puede contar desde HEAD: el primer push crea una rama que
    puede no ser la que HEAD apunta, y entonces `rev-list HEAD` no ve nada.
    """
    return int((git(repo, "rev-list", "--count", "--all") or "0").strip() or 0)


def ramas(repo: Path) -> list:
    salida = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return sorted(l.strip() for l in salida.splitlines() if l.strip())


def hacer_config(base: Path, remoto: str = "") -> Config:
    base.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        almacen=ConfigAlmacen(
            git=str(base / "configs.git"),
            binarios=str(base / "binarios"),
            estado=str(base / "estado.json"),
            ajustes=str(base / "ajustes.json"),
            remoto=remoto,
        ),
        # El binario no pinta nada en la replicacion: solo el texto va a git.
        binario=ConfigBinario(activo=False),
        ssh=Config().ssh,
    )
    cfg.ssh.password = "x"  # solo para pasar la validacion
    return cfg


EQUIPO = Equipo(nombre="BTS-Norte-01", ip="10.0.0.1", grupo="bts", empresa="acme")


def respaldar(almacen: Almacen, texto: str) -> None:
    almacen.guardar(Resultado(equipo=EQUIPO, export=texto, version="7.14"))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        # --- 1. La credencial: que se le pasa a git y que se le tapa ---------

        cfg = hacer_config(base / "cred")
        almacen = Almacen(cfg)
        almacen.inicializar()

        previos, tapar, entorno = almacen._credencial()
        # Sin token tampoco se va de vacio: se apaga el ayudante de credenciales
        # del sistema. GIT_TERMINAL_PROMPT tapa el aviso por terminal, pero NO un
        # credential.helper del gitconfig de la maquina, que abre su propia
        # ventana o consulta un llavero y deja el proceso esperando a nadie.
        comprobar(f"sin token solo se apaga el ayudante de credenciales ({previos})",
                  previos == ["-c", "credential.helper="])
        comprobar("sin token no hay ningun secreto que tapar",
                  list(tapar) == [])
        # Sin esto un remoto que pide credenciales deja el proceso colgado
        # esperando a alguien que no hay. Es peor que fallar: no se ve.
        comprobar("sin token git tampoco puede pararse a preguntar "
                  "(GIT_TERMINAL_PROMPT=0)",
                  entorno.get("GIT_TERMINAL_PROMPT") == "0")

        cfg.almacen.remoto_usuario = USUARIO
        cfg.almacen.remoto_token = TOKEN
        previos, tapar, entorno = almacen._credencial()
        basica = base64.b64encode(f"{USUARIO}:{TOKEN}".encode()).decode("ascii")

        comprobar("con token la credencial va como una opcion -c mas, ademas de "
                  "la que apaga el ayudante",
                  previos[:2] == ["-c", "credential.helper="]
                  and len(previos) == 4 and previos[2] == "-c")
        comprobar(f"la opcion es http.extraheader con Authorization: Basic "
                  f"base64('{USUARIO}:<token>')",
                  previos[3] == f"http.extraheader=Authorization: Basic {basica}")
        # Lo que git recibe tiene que descodificar exactamente a usuario:token,
        # no a otra cosa que "parezca" bien.
        codificado = previos[3].split("Basic ")[-1]
        comprobar("la cabecera descodifica a usuario:token",
                  base64.b64decode(codificado).decode()
                  == f"{USUARIO}:{TOKEN}")
        comprobar("el token NO viaja en claro en la linea de comandos",
                  TOKEN not in " ".join(previos))
        comprobar("se marcan como secretos el token y su version en base64",
                  TOKEN in tapar and basica in tapar)
        comprobar("con token git sigue sin poder pararse a preguntar "
                  "(GIT_TERMINAL_PROMPT=0)",
                  entorno.get("GIT_TERMINAL_PROMPT") == "0")

        # GitHub y GitLab miran el token y no el usuario, asi que el campo se
        # deja vacio a menudo: entonces hay que poner algo, o la cabecera sale
        # como ':token' y el servidor la rechaza.
        cfg.almacen.remoto_usuario = ""
        previos, _, _ = almacen._credencial()
        sin_usuario = base64.b64decode(previos[-1].split("Basic ")[-1]).decode()
        comprobar(f"con el usuario vacio se usa x-access-token ({sin_usuario!r})",
                  sin_usuario == f"x-access-token:{TOKEN}")

        # --- 2. El token no se filtra por el mensaje de error ----------------
        # Es lo mas importante de este archivo. Un push contra un remoto que no
        # existe: git falla, y ese texto acaba en el log, en el panel y en el
        # correo de aviso.

        inexistente = str(base / "cred" / "no-existe.git")
        cfg.almacen.remoto = inexistente
        cfg.almacen.remoto_usuario = USUARIO
        respaldar(almacen, "/ip dns\nset servers=8.8.8.8\n")

        mensaje = ""
        try:
            almacen.replicar()
        except ErrorAlmacen as exc:
            mensaje = str(exc)

        comprobar("empujar a un remoto que no existe lanza ErrorAlmacen",
                  mensaje != "")
        comprobar("el mensaje de error dice algo util (no esta vacio)",
                  len(mensaje) > 20)
        comprobar("el token no sale en el mensaje de error", TOKEN not in mensaje)
        comprobar("la cabecera en base64 tampoco sale en el mensaje de error",
                  basica not in mensaje)

        # El otro sitio por el que se escapa una credencial: .git/config. Ahi
        # es donde acaba si se mete dentro de la URL, que es como se suele
        # hacer, y desde ahi viaja en cada copia del repositorio.
        repo = Path(cfg.almacen.git)
        config_git = (repo / ".git" / "config").read_text(encoding="utf-8")
        comprobar("el token NO acaba escrito en .git/config",
                  TOKEN not in config_git)
        comprobar("la cabecera en base64 tampoco acaba en .git/config",
                  basica not in config_git)
        comprobar("en .git/config no queda ninguna extraheader",
                  "extraheader" not in config_git.lower())

        # --- 3. replicar() empuja de verdad ---------------------------------

        bare = crear_bare(base / "remoto.git")
        cfg = hacer_config(base / "sube", remoto=bare)
        cfg.almacen.remoto_usuario = USUARIO
        cfg.almacen.remoto_token = TOKEN
        almacen = Almacen(cfg)
        almacen.inicializar()
        repo = Path(cfg.almacen.git)

        comprobar(f"el remoto empieza vacio ({commits(Path(bare))} commits)",
                  commits(Path(bare)) == 0)

        respaldar(almacen, "/ip dns\nset servers=8.8.8.8\n")
        respaldar(almacen, "/ip dns\nset servers=1.1.1.1\n")
        locales = commits(repo)

        detalle = almacen.replicar()
        comprobar(f"replicar devuelve un texto que nombra el remoto ({detalle})",
                  bare in detalle)
        comprobar(f"los commits llegan al remoto ({commits(Path(bare))} de "
                  f"{locales})",
                  commits(Path(bare)) == locales)

        historial = git(Path(bare), "log", "--all", "--oneline")
        comprobar("el historial del remoto nombra el equipo respaldado",
                  "acme/BTS-Norte-01" in historial)
        rama_local = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        contenido = git(Path(bare), "show", f"{rama_local}:acme/BTS-Norte-01.rsc")
        comprobar("el contenido que llego al remoto es el ultimo respaldo",
                  "1.1.1.1" in contenido)

        # Un push que SI funciona tampoco puede dejar la credencial guardada.
        config_git = (repo / ".git" / "config").read_text(encoding="utf-8")
        comprobar("tras un push con exito el token sigue sin estar en .git/config",
                  TOKEN not in config_git and basica not in config_git)

        # Replicar dos veces seguidas sin cambios nuevos no puede reventar:
        # el planificador puede llamar con el remoto ya al dia.
        sin_novedad = ""
        try:
            sin_novedad = almacen.replicar()
        except ErrorAlmacen as exc:
            sin_novedad = f"lanzo: {exc}"
        comprobar(f"replicar de nuevo sin commits nuevos no falla ({sin_novedad})",
                  sin_novedad.startswith("replicado"))

        # --- 4. remoto_rama: la rama del remoto puede llamarse distinto ------

        otro_bare = crear_bare(base / "otro-remoto.git")
        cfg.almacen.remoto = otro_bare
        cfg.almacen.remoto_rama = "respaldos"
        detalle = almacen.replicar()

        comprobar(f"con remoto_rama el push crea ESA rama en el remoto "
                  f"({ramas(Path(otro_bare))})",
                  ramas(Path(otro_bare)) == ["respaldos"])
        comprobar(f"y NO crea la rama local ({rama_local})",
                  rama_local not in ramas(Path(otro_bare)))
        comprobar(f"el texto devuelto nombra la rama ({detalle})",
                  "respaldos" in detalle)
        comprobar("los commits llegaron a la rama nueva",
                  commits(Path(otro_bare)) == commits(repo))
        comprobar("la rama local del repositorio de aqui no cambia",
                  git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
                  == rama_local)

        # --- 5. probar_remoto: el boton de "esto funciona" del panel ---------
        # Existe porque la alternativa para saber si el token vale es esperar
        # al proximo ciclo y leer el log, y quien acaba de pegarlo lo quiere
        # saber ahora.

        vacio = crear_bare(base / "vacio.git")
        cfg.almacen.remoto = vacio
        texto = almacen.probar_remoto()
        comprobar(f"contra un remoto vacio dice que se llega ({texto})",
                  "se llega" in texto)

        cfg.almacen.remoto = otro_bare
        texto = almacen.probar_remoto()
        comprobar(f"contra un remoto con ramas las enumera ({texto})",
                  "se llega" in texto and "respaldos" in texto)

        cfg.almacen.remoto = str(base / "inventado.git")
        fallo = ""
        try:
            almacen.probar_remoto()
        except ErrorAlmacen as exc:
            fallo = str(exc)
        comprobar("contra una direccion inventada lanza ErrorAlmacen", fallo != "")
        comprobar("y ese error tampoco lleva el token dentro",
                  TOKEN not in fallo and basica not in fallo)

        cfg.almacen.remoto = ""
        fallo = ""
        try:
            almacen.probar_remoto()
        except ErrorAlmacen as exc:
            fallo = str(exc)
        comprobar(f"sin direccion configurada avisa de que falta ({fallo})",
                  "direccion" in fallo)

        # --- 6. El parte de la ultima subida (replica.json) ------------------
        # Lo escribe el proceso que respalda y lo lee el panel, que es otro
        # proceso. Leerlo NO puede lanzar nunca: se llama al pintar Ajustes, y
        # un archivo a medias dejaria el panel en blanco.

        cfg = hacer_config(base / "parte")
        ruta = ruta_replica(cfg)
        comprobar(f"el parte vive junto al estado ({ruta.name})",
                  ruta.parent == Path(cfg.almacen.estado).parent
                  and ruta.name == "replica.json")
        comprobar("sin archivo, leer_replica devuelve {} y no lanza",
                  leer_replica(cfg) == {})

        anotar_replica(cfg, True, "replicado a /tmp/x (master)", 0)
        parte = leer_replica(cfg)
        comprobar(f"lo anotado se relee entero ({sorted(parte)})",
                  parte.get("ok") is True
                  and parte.get("detalle") == "replicado a /tmp/x (master)"
                  and parte.get("pendientes") == 0
                  and bool(parte.get("cuando")))

        anotar_replica(cfg, False, "no se pudo", 7)
        parte = leer_replica(cfg)
        comprobar(f"una anotacion pisa a la anterior (pendientes="
                  f"{parte.get('pendientes')})",
                  parte.get("ok") is False and parte.get("pendientes") == 7)

        ruta.write_text("{esto no es json", encoding="utf-8")
        corrupto = leer_replica(cfg)
        comprobar("con el archivo corrupto devuelve {} y no lanza",
                  corrupto == {})

        # Un JSON valido que no sea un mapa tambien tiene que caer de pie: el
        # panel hace .get() sobre lo que devuelva esto.
        ruta.write_text("[1, 2, 3]", encoding="utf-8")
        comprobar("con un JSON que no es un mapa tambien devuelve {}",
                  leer_replica(cfg) == {})

        # Y se recupera solo: la proxima anotacion vuelve a dejarlo legible.
        anotar_replica(cfg, True, "otra vez bien", 0)
        comprobar("tras corromperlo, la siguiente anotacion lo deja legible",
                  leer_replica(cfg).get("detalle") == "otra vez bien")

        # --- 7. cli._replicar: cada cuantos ciclos se empuja -----------------

        tercer_bare = crear_bare(base / "cada.git")
        cfg = hacer_config(base / "cada", remoto=tercer_bare)
        cfg.almacen.remoto_token = TOKEN
        cfg.almacen.remoto_cada = 3
        almacen = Almacen(cfg)
        almacen.inicializar()
        repo = Path(cfg.almacen.git)
        fallidos = []

        respaldar(almacen, "/ip dns\nset servers=8.8.8.8\n")
        cli._replicar(cfg, almacen, fallidos)
        comprobar(f"con remoto_cada=3 el primer ciclo NO empuja "
                  f"({commits(Path(tercer_bare))} commits en el remoto)",
                  commits(Path(tercer_bare)) == 0)
        comprobar("y el contador de pendientes sube a 1",
                  leer_replica(cfg).get("pendientes") == 1)

        respaldar(almacen, "/ip dns\nset servers=9.9.9.9\n")
        cli._replicar(cfg, almacen, fallidos)
        comprobar("el segundo ciclo con cambios tampoco empuja",
                  commits(Path(tercer_bare)) == 0)
        comprobar("y el contador de pendientes sube a 2",
                  leer_replica(cfg).get("pendientes") == 2)

        respaldar(almacen, "/ip dns\nset servers=1.1.1.1\n")
        cli._replicar(cfg, almacen, fallidos)
        comprobar(f"el tercero SI empuja ({commits(Path(tercer_bare))} commits "
                  f"en el remoto)",
                  commits(Path(tercer_bare)) == commits(repo))
        parte = leer_replica(cfg)
        comprobar("tras el push con exito los pendientes vuelven a 0",
                  parte.get("pendientes") == 0)
        comprobar(f"y queda anotado como bueno ({parte.get('detalle')})",
                  parte.get("ok") is True)
        comprobar("un push con exito no anota nada en la lista de fallidos",
                  fallidos == [])

        # remoto_cada = 0 apaga la subida sin tener que borrar la direccion,
        # que es lo que se quiere para pararla un rato sin perder el token.
        cfg.almacen.remoto_cada = 0
        antes = commits(Path(tercer_bare))
        respaldar(almacen, "/ip dns\nset servers=4.4.4.4\n")
        cli._replicar(cfg, almacen, fallidos)
        respaldar(almacen, "/ip dns\nset servers=5.5.5.5\n")
        cli._replicar(cfg, almacen, fallidos)
        comprobar(f"con remoto_cada=0 no se empuja nunca (el remoto sigue en "
                  f"{commits(Path(tercer_bare))} commits)",
                  commits(Path(tercer_bare)) == antes)
        parte = leer_replica(cfg)
        comprobar(f"pero los ciclos sin subir se siguen contando "
                  f"({parte.get('pendientes')} pendientes)",
                  parte.get("pendientes") == 2)
        comprobar("y el parte dice que esta apagada",
                  "apagada" in (parte.get("detalle") or ""))

        # Un push que falla NO puede poner el contador a cero: esos commits no
        # salieron de aqui y hay que reintentarlos, toque o no por contador.
        cfg.almacen.remoto = str(base / "cada" / "se-fue.git")
        cfg.almacen.remoto_cada = 1
        cli._replicar(cfg, almacen, fallidos)
        parte = leer_replica(cfg)
        comprobar(f"si el push falla los pendientes NO se ponen a cero "
                  f"({parte.get('pendientes')})",
                  parte.get("pendientes") == 3)
        comprobar("el fallo queda anotado como tal", parte.get("ok") is False)
        comprobar("el fallo de replicacion se suma a los fallidos del ciclo",
                  [n for n, _ in fallidos] == ["__replicacion__"])
        # Este texto lo pinta el panel en Ajustes: si el token se colara aqui,
        # quedaria escrito en disco y a la vista de cualquiera que entre.
        comprobar("el detalle del fallo que lee el panel no lleva el token",
                  TOKEN not in (parte.get("detalle") or ""))
        comprobar("el fallo que se manda a las notificaciones tampoco",
                  all(TOKEN not in texto for _, texto in fallidos))

        # --- 8. La lista blanca de ajustes ----------------------------------
        # El remoto SI se cambia desde el panel (un token caduca y rotarlo no
        # puede exigir entrar por SSH al servidor); las rutas locales no.

        claves = {"remoto", "remoto_rama", "remoto_usuario", "remoto_token",
                  "remoto_cada"}
        faltan = sorted(claves - set(AJUSTES_EDITABLES))
        comprobar(f"las cinco claves remoto* estan en la lista blanca "
                  f"(faltan: {faltan})",
                  not faltan)
        comprobar("y las cinco apuntan a la seccion almacen",
                  all(AJUSTES_EDITABLES[k][0] == "almacen" for k in claves))

        cfg = hacer_config(base / "ajustes")
        cuarto_bare = crear_bare(base / "ajustado.git")
        cfg.guardar_ajustes({
            "remoto": cuarto_bare,
            "remoto_rama": "respaldos",
            "remoto_usuario": USUARIO,
            "remoto_token": TOKEN,
            "remoto_cada": 4,
            # No esta en la lista blanca: apuntar los respaldos a otro sitio
            # del disco desde el panel seria dejar de guardarlos donde se cree.
            "binarios": str(base / "secuestrado"),
        })
        comprobar(f"guardar_ajustes deja puesta la direccion del remoto "
                  f"({cfg.almacen.remoto == cuarto_bare})",
                  cfg.almacen.remoto == cuarto_bare)
        comprobar("y la rama, el usuario y el token",
                  cfg.almacen.remoto_rama == "respaldos"
                  and cfg.almacen.remoto_usuario == USUARIO
                  and cfg.almacen.remoto_token == TOKEN)
        comprobar(f"remoto_cada se guarda como numero ({cfg.almacen.remoto_cada!r})",
                  cfg.almacen.remoto_cada == 4)
        comprobar("una clave que no esta en la lista blanca se ignora",
                  cfg.almacen.binarios == str(base / "ajustes" / "binarios"))

        escrito = json.loads(Path(cfg.almacen.ajustes).read_text(encoding="utf-8"))
        comprobar(f"la clave de fuera ni siquiera llega al archivo "
                  f"({sorted(escrito)})",
                  "binarios" not in escrito)

        # Lo que de verdad importa: otro proceso (el respaldo, que es quien
        # empuja) tiene que leer lo que puso el panel. Config nueva, mismas
        # rutas, aplicar_ajustes.
        otra = hacer_config(base / "ajustes")
        comprobar("una Config recien hecha aun no sabe nada del remoto",
                  otra.almacen.remoto == "")
        otra.aplicar_ajustes()
        comprobar(f"tras aplicar_ajustes el otro proceso ve el remoto "
                  f"({otra.almacen.remoto == cuarto_bare})",
                  otra.almacen.remoto == cuarto_bare
                  and otra.almacen.remoto_rama == "respaldos"
                  and otra.almacen.remoto_token == TOKEN
                  and otra.almacen.remoto_cada == 4)

        # Y con esos ajustes, un almacen nuevo replica sin tocar nada mas: es
        # el camino completo panel -> archivo -> respaldo.
        almacen = Almacen(otra)
        almacen.inicializar()
        respaldar(almacen, "/ip dns\nset servers=8.8.4.4\n")
        otra.almacen.remoto_token = ""  # el bare local no pide credencial
        almacen.replicar()
        comprobar(f"lo configurado desde el panel acaba empujando de verdad "
                  f"({ramas(Path(cuarto_bare))})",
                  ramas(Path(cuarto_bare)) == ["respaldos"])

    desde_el_panel()

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallidas:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


def desde_el_panel() -> None:
    """El formulario de Ajustes, enviado como lo envia el navegador.

    Lo de arriba prueba el motor; esto prueba el camino por el que se usa. Se
    levanta el panel de verdad (la misma clase que usa tests/test_panel.py) y se
    le mandan POST reales: es el unico sitio donde se ve si la validacion de la
    direccion, la lista blanca de ajustes y el "vacio no toca el token" se
    comportan juntos.
    """
    print("\n--- El formulario del panel ---")
    from tests.test_panel import Panel

    with tempfile.TemporaryDirectory() as tmp:
        panel = Panel(Path(tmp))
        try:
            panel.entrar()

            codigo, html = panel.pedir("/ajustes")
            comprobar(f"la pantalla ofrece configurar el remoto (dio {codigo})",
                      codigo == 200 and 'action="/ajustes/remoto"' in html)
            comprobar("y avisa de que tiene que ser privado",
                      "privado" in html.lower())

            # http:// sin cifrar NO: por ahi viajarian las configuraciones de
            # los clientes y el propio token.
            codigo, html = panel.pedir(
                "/ajustes/remoto", {"remoto": "http://ejemplo.com/r.git",
                                    "remoto_cada": "1"})
            comprobar(f"http:// sin cifrar se rechaza (dio {codigo})", codigo == 400)
            comprobar("y se guarda nada", not panel.cfg.almacen.remoto)

            # El token pegado dentro de la URL es la costumbre, y es justo lo
            # que hay que impedir: ahi deja de ser un secreto.
            codigo, html = panel.pedir(
                "/ajustes/remoto",
                {"remoto": f"https://oauth2:{TOKEN}@gitlab.com/e/r.git",
                 "remoto_cada": "1"})
            comprobar(f"el token dentro de la direccion se rechaza (dio {codigo})",
                      codigo == 400)
            comprobar("explicando por que",
                      "no pongas" in html.lower() or "quedaria escrito" in html)
            comprobar("y el token no se guarda por el camino",
                      TOKEN not in Path(panel.cfg.almacen.ajustes).read_text(
                          encoding="utf-8")
                      if Path(panel.cfg.almacen.ajustes).is_file() else True)

            codigo, html = panel.pedir(
                "/ajustes/remoto",
                {"remoto": "https://github.com/empresa/respaldos.git",
                 "remoto_rama": "main", "remoto_usuario": "x-access-token",
                 "remoto_token": TOKEN, "remoto_cada": "2"})
            comprobar(f"una direccion buena se guarda (dio {codigo})", codigo == 200)
            comprobar("la direccion queda puesta",
                      panel.cfg.almacen.remoto.endswith("respaldos.git"))
            comprobar("el token tambien", panel.cfg.almacen.remoto_token == TOKEN)
            comprobar("y 'cada cuantos ciclos' como numero",
                      panel.cfg.almacen.remoto_cada == 2)

            # LO QUE NO PUEDE PASAR: que el token vuelva al navegador. Ni en el
            # campo, ni escondido en un atributo, ni en un comentario.
            codigo, html = panel.pedir("/ajustes")
            comprobar("el token NUNCA vuelve a la pantalla", TOKEN not in html)
            comprobar("y el campo se sirve vacio",
                      'name="remoto_token" value=""' in html)
            comprobar("aunque se dice que hay uno guardado",
                      "Hay un token guardado" in html)

            # Vacio significa "no lo toques". Si guardara la cadena vacia, abrir
            # la pantalla y pulsar Guardar dejaria la subida sin credencial,
            # fallando en silencio hasta que alguien mirase el log.
            panel.pedir("/ajustes/remoto",
                        {"remoto": "https://github.com/empresa/respaldos.git",
                         "remoto_cada": "1"})
            comprobar("guardar con el token vacio NO lo borra",
                      panel.cfg.almacen.remoto_token == TOKEN)

            # Pero quitar la direccion si: una credencial para un sitio al que
            # ya no se sube es un secreto guardado sin motivo.
            panel.pedir("/ajustes/remoto", {"remoto": "", "remoto_cada": "1"})
            comprobar("quitar la direccion se lleva el token",
                      panel.cfg.almacen.remoto_token == "")
            comprobar("y el archivo de ajustes deja de tenerlo",
                      TOKEN not in Path(panel.cfg.almacen.ajustes).read_text(
                          encoding="utf-8"))

            # Con un campo cualquiera y no con {}: un diccionario vacio se
            # codifica como cuerpo vacio y el ayudante lo manda como GET, que es
            # otra ruta y otro resultado.
            # Con un remoto configurado, la tarjeta de borrar datos TIENE que
            # avisar: purgar reescribe este repositorio y la copia de alla se
            # queda como estaba. Sin ese aviso, quien borra se va convencido de
            # haber borrado algo que sigue estando.
            panel.pedir("/ajustes/remoto",
                        {"remoto": "https://github.com/empresa/respaldos.git",
                         "remoto_cada": "1"})
            codigo, html = panel.pedir("/ajustes")
            comprobar("con remoto puesto, borrar datos avisa de que alla no borra",
                      "Borrar aqui no borra alli" in html)
            comprobar("y nombra el repositorio al que se sube",
                      "empresa/respaldos.git" in html)
            panel.pedir("/ajustes/remoto", {"remoto": "", "remoto_cada": "1"})
            codigo, html = panel.pedir("/ajustes")
            comprobar("sin remoto, ese aviso no esta (no aplica)",
                      "Borrar aqui no borra alli" not in html)

            codigo, html = panel.pedir("/ajustes/remoto/probar", {"x": "1"})
            comprobar(f"probar sin direccion contesta sin caerse (dio {codigo})",
                      codigo == 400)
            comprobar("diciendo que no hay direccion puesta",
                      "direccion" in html and "repositorio" in html)
        finally:
            panel.cerrar()


if __name__ == "__main__":
    main()
