"""Prueba del historial: navegar versiones sin repartir credenciales.

Monta un repo git de verdad en un directorio temporal, con varios commits
sobre un .rsc de contenido realista de RouterOS, y comprueba tres cosas:
que el historial se lee bien (orden, conteo, diff), que los secretos salen
tapados y los falsos positivos NO, y que una ruta o un commit escritos por un
atacante no llegan a git.

Ejecutar:  python -m tests.test_historial
"""

import subprocess
import tempfile
from pathlib import Path

from mkbackup.historial import (
    MARCA,
    ErrorHistorial,
    cambios,
    contenido,
    diferencia,
    enmascarar,
    existe_repo,
    versiones,
)

FALLOS = []

RUTA = "acme/bts/BTS-Norte-01.rsc"


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


def commitear(repo: Path, ruta: str, texto: str, mensaje: str) -> None:
    destino = repo / ruta
    destino.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" igual que store.py: si no, en Windows cada respaldo
    # pareceria un cambio completo y el diff dejaria de probar nada.
    destino.write_text(texto, encoding="utf-8", newline="\n")
    git(repo, "add", "--", ruta)
    git(repo, "commit", "-q", "-m", mensaje, "--", ruta)


def preparar(base: Path) -> Path:
    repo = base / "configs.git"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "mkbackup")
    git(repo, "config", "user.email", "mkbackup@local")
    (repo / ".gitattributes").write_text("*.rsc text eol=lf\n", encoding="utf-8")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "Repositorio de configuraciones")
    return repo


# Contenido realista de /export, con secretos donde RouterOS los pone.
V1 = """# jan/02/2026 03:00:11 by RouterOS 7.14
/interface wireless security-profiles
set [ find default=yes ] wpa2-pre-shared-key="Clave-Vieja-2024"
/ip dns
set servers=8.8.8.8
/ppp secret
add name=cliente01 password=pppoe-viejo service=pppoe
/snmp community
set [ find default=yes ] name=publico
"""

V2 = """# jan/03/2026 03:00:09 by RouterOS 7.14
/interface wireless security-profiles
set [ find default=yes ] wpa2-pre-shared-key="Clave-Nueva-2026"
/ip dns
set servers=1.1.1.1
/ppp secret
add name=cliente01 password=pppoe-viejo service=pppoe
/snmp community
set [ find default=yes ] name=publico
"""

V3 = """# jan/04/2026 03:00:14 by RouterOS 7.14
/interface wireless security-profiles
set [ find default=yes ] wpa2-pre-shared-key="Clave-Nueva-2026"
/ip dns
set servers=1.1.1.1
/ppp secret
add name=cliente01 password=pppoe-viejo service=pppoe
add name=cliente02 password=otro-secreto service=pppoe
/snmp community
set [ find default=yes ] name=publico
"""


def probar_enmascarado() -> None:
    print("\n--- enmascarado ---")

    # Cada atributo de la lista, con el valor que tendria en un export real.
    casos = [
        ("set password=Sup3rS3creta", "Sup3rS3creta"),
        ("add secret=vpn-secreto-01", "vpn-secreto-01"),
        ("set psk=clave-psk-123", "clave-psk-123"),
        ('set pre-shared-key="ipsec 2026"', "ipsec 2026"),
        ('set wpa-pre-shared-key="Wifi-2024"', "Wifi-2024"),
        ('set wpa2-pre-shared-key="Wifi-2026"', "Wifi-2026"),
        ("set passphrase=frase-larga", "frase-larga"),
        ("add key=llave-ipsec", "llave-ipsec"),
        ("import private-key=abcdef0123", "abcdef0123"),
        ("set auth-password=snmpauth1", "snmpauth1"),
        ("set enc-password=snmpenc1", "snmpenc1"),
        ("set authentication-password=auth-larga", "auth-larga"),
        ("set encryption-password=enc-larga", "enc-larga"),
        ("set community=publico-secreto", "publico-secreto"),
        ("set token=tok3n-de-api", "tok3n-de-api"),
        ("set shared-secret=radius123", "radius123"),
        ("set eap-password=eap-clave", "eap-clave"),
        ("set caller-id-password=callerpass", "callerpass"),
    ]
    for linea, secreto in casos:
        salida, oculto = enmascarar(linea)
        atributo = linea.split("=", 1)[0].split(" ")[-1]
        comprobar(
            f"tapa el valor de {atributo}",
            oculto and secreto not in salida and MARCA in salida,
        )
        comprobar(
            f"conserva el nombre del atributo {atributo}",
            f"{atributo}=" in salida,
        )

    # Insensible a mayusculas.
    salida, oculto = enmascarar("set Password=MiClave")
    comprobar("enmascara sin importar las mayusculas",
              oculto and "MiClave" not in salida)

    # --- falsos positivos: esto NO se puede tapar -------------------------
    no_tocar = [
        "set key-type=rsa",                       # 'key' seguido de guion
        "set public-key=ssh-rsa-AAAAB3Nza",       # 'key' precedido de guion
        "add ssh-host-key=aabbcc",                # idem
        "set password-strength=medium",           # 'password' seguido de guion
        "set require-password=yes",               # atributo distinto, y es yes/no
        "set use-encryption=yes",
        "set name=publico",
        "set servers=8.8.8.8",
        "# password de ejemplo en un comentario",  # no hay asignacion
        "set password=no",                        # un no no es una credencial
        'set secret=""',                          # vacio: no hay nada que tapar
    ]
    for linea in no_tocar:
        salida, oculto = enmascarar(linea)
        comprobar(f"NO enmascara: {linea}", salida == linea and not oculto)

    # --- varios secretos en la misma linea --------------------------------
    salida, oculto = enmascarar(
        "add name=cliente password=clave1 caller-id-password=clave2 service=pppoe"
    )
    comprobar(
        "tapa todos los secretos de una misma linea",
        oculto and "clave1" not in salida and "clave2" not in salida
        and "name=cliente" in salida and "service=pppoe" in salida,
    )

    # --- rotacion de una clave: las dos versiones salen iguales -----------
    # Es la propiedad que se busca: se ve que la linea cambio, pero ni el
    # valor viejo ni el nuevo llegan al navegador.
    vieja, _ = enmascarar('set wpa2-pre-shared-key="Clave-Vieja-2024"')
    nueva, _ = enmascarar('set wpa2-pre-shared-key="Clave-Nueva-2026"')
    comprobar(
        "dos versiones que solo difieren en el secreto salen identicas",
        vieja == nueva and "Vieja" not in vieja and "Nueva" not in nueva,
    )


def probar_seguridad(repo: Path) -> None:
    print("\n--- validacion de parametros de la web ---")

    rutas_malas = [
        "../../etc/passwd",
        "acme/../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "..\\..\\etc\\passwd",
        "--output=algo",
        "--upload-pack=touch",
        "-acme/bts/x.rsc",
        "acme/*.rsc",
        "acme//bts/x.rsc",
        "acme/bts/x.rsc\nrm",
        "",
        "   ",
    ]
    for mala in rutas_malas:
        for nombre, llamada in (
            ("versiones", lambda m=mala: versiones(repo, m)),
            ("diferencia", lambda m=mala: diferencia(repo, m, "abcdef1")),
            ("contenido", lambda m=mala: contenido(repo, m, "abcdef1")),
        ):
            try:
                llamada()
            except ErrorHistorial:
                rechazada = True
            except Exception as exc:  # cualquier otra cosa es un descuido
                rechazada = False
                print(f"       ({nombre} lanzo {type(exc).__name__})")
            else:
                rechazada = False
            comprobar(f"{nombre} rechaza la ruta {mala!r}", rechazada)

    commits_malos = [
        "HEAD",
        "HEAD~1",
        "master",
        "abc",              # demasiado corto
        "zzzzzzz",          # no hexadecimal
        "ABCDEF1",          # mayusculas: git no las emite
        "abcdef1; rm -rf /",
        "--output=algo",
        "abcdef1 --output=x",
        "",
        "0" * 41,           # demasiado largo
    ]
    for malo in commits_malos:
        for nombre, llamada in (
            ("diferencia", lambda m=malo: diferencia(repo, RUTA, m)),
            ("contenido", lambda m=malo: contenido(repo, RUTA, m)),
        ):
            try:
                llamada()
            except ErrorHistorial:
                rechazado = True
            except Exception as exc:
                rechazado = False
                print(f"       ({nombre} lanzo {type(exc).__name__})")
            else:
                rechazado = False
            comprobar(f"{nombre} rechaza el commit {malo!r}", rechazado)

    # Ningun archivo de fuera del repo pudo salir por ninguna de esas rutas:
    # si alguna hubiera pasado, la comprobacion de arriba ya habria fallado.
    # Queda por ver que un hash valido pero inexistente tampoco revienta.
    inexistente = "0" * 40
    comprobar("diff de un commit inexistente devuelve lista vacia",
              diferencia(repo, RUTA, inexistente) == [])
    try:
        contenido(repo, RUTA, inexistente)
    except ErrorHistorial:
        ok = True
    except Exception:
        ok = False
    else:
        ok = False
    comprobar("contenido de un commit inexistente lanza ErrorHistorial", ok)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        # --- repo que todavia no existe -----------------------------------
        vacio = base / "no-existe"
        comprobar("repo inexistente: sin versiones, sin traceback",
                  versiones(vacio, RUTA) == [])
        comprobar("repo inexistente: sin cambios, sin traceback",
                  cambios(vacio) == [])
        comprobar("repo inexistente: existe_repo dice que no",
                  not existe_repo(vacio))

        repo = preparar(base)
        comprobar("existe_repo reconoce el repo recien creado", existe_repo(repo))

        # --- archivo sin historial ----------------------------------------
        comprobar("archivo nunca respaldado: lista vacia",
                  versiones(repo, "otra/empresa/AP-Suelto.rsc") == [])

        # --- tres versiones -----------------------------------------------
        commitear(repo, RUTA, V1, "Alta: acme/bts/BTS-Norte-01 (RouterOS 7.14)")
        commitear(repo, RUTA, V2, "Cambio: acme/bts/BTS-Norte-01 (RouterOS 7.14)")
        commitear(repo, RUTA, V3, "Cambio: acme/bts/BTS-Norte-01 (RouterOS 7.14)")
        # Otro equipo, de otra empresa: no debe colarse en el historial del
        # primero, que es justo lo que se paga con la ruta empresa/grupo/nombre.
        otra = "otracorp/core/Router-Principal.rsc"
        commitear(repo, otra, "/system identity\nset name=OTRACORP\n",
                  "Alta: otracorp/core/Router-Principal")

        print("\n--- versiones ---")
        v = versiones(repo, RUTA)
        comprobar("hay tres versiones del equipo", len(v) == 3)
        comprobar("el historial del equipo no trae los commits de otra empresa",
                  all("BTS-Norte-01" in x.mensaje for x in v))

        comprobar("la mas reciente va primero",
                  len(v) == 3 and v[0].fecha >= v[1].fecha >= v[2].fecha)
        comprobar("la ultima version es la del segundo cliente PPPoE",
                  v[0].mensaje.startswith("Cambio:"))
        comprobar("la primera version del archivo es el alta",
                  v[-1].mensaje.startswith("Alta:"))

        comprobar("cada version trae hash corto",
                  all(x.commit and len(x.commit) < 40 for x in v))
        comprobar("cada version trae fecha ISO con zona",
                  all(len(x.fecha) >= 20 and "T" in x.fecha for x in v))

        # El alta anade el archivo entero y no quita nada.
        comprobar("el alta cuenta solo lineas anadidas",
                  v[-1].lineas_mas == len(V1.splitlines()) and v[-1].lineas_menos == 0)
        # V2 -> V3 anade una linea (el cliente02) y cambia la del encabezado.
        comprobar("el ultimo cambio cuenta 2 altas y 1 baja",
                  v[0].lineas_mas == 2 and v[0].lineas_menos == 1)

        # --- limite maximo -------------------------------------------------
        comprobar("maximo=1 devuelve una sola version",
                  len(versiones(repo, RUTA, maximo=1)) == 1)
        comprobar("maximo=2 devuelve las dos mas recientes",
                  [x.commit for x in versiones(repo, RUTA, maximo=2)]
                  == [v[0].commit, v[1].commit])
        comprobar("maximo mayor que el historial no inventa versiones",
                  len(versiones(repo, RUTA, maximo=500)) == 3)

        # --- cambios de toda la flota --------------------------------------
        print("\n--- cambios de la flota ---")
        todos = cambios(repo)
        comprobar("cambios devuelve pares (ruta, version)",
                  all(isinstance(p, tuple) and len(p) == 2 for p in todos))
        comprobar("cambios ve los cuatro commits de configuracion",
                  len(todos) == 4)
        comprobar("cambios ordena del mas reciente al mas antiguo",
                  [p[0] for p in todos][0] == otra)
        comprobar("cambios NO incluye el .gitattributes del repo",
                  all(r.endswith(".rsc") for r, _ in todos))
        comprobar("cambios limita con maximo", len(cambios(repo, maximo=2)) == 2)
        comprobar("cada par trae la ruta con la que se puede volver a consultar",
                  versiones(repo, todos[0][0]) != [])

        # Un commit que toca DOS archivos tiene que dar DOS pares.
        (repo / "acme" / "bts" / "BTS-Sur-02.rsc").write_text(
            "/ip dns\nset servers=9.9.9.9\n", encoding="utf-8", newline="\n")
        (repo / "acme" / "bts" / "BTS-Norte-01.rsc").write_text(
            V3 + "/ip service\nset www disabled=yes\n",
            encoding="utf-8", newline="\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "Cambio: dos equipos a la vez")
        doble = [p for p in cambios(repo) if p[1].mensaje == "Cambio: dos equipos a la vez"]
        comprobar("un commit con dos archivos emite dos pares", len(doble) == 2)
        comprobar("los dos pares comparten commit y difieren en ruta",
                  len(doble) == 2
                  and doble[0][1].commit == doble[1][1].commit
                  and doble[0][0] != doble[1][0])

        # --- diferencia -----------------------------------------------------
        print("\n--- diferencia ---")
        # v[1] es el commit V1 -> V2: cambia el DNS y rota la PSK.
        d = diferencia(repo, RUTA, v[1].commit)
        comprobar("el diff no viene vacio", len(d) > 0)
        comprobar("el diff descarta la cabecera de git",
                  not any(l.texto.startswith(("diff --git", "index ")) for l in d))
        comprobar("los tipos son los del contrato",
                  {l.tipo for l in d} <= {"mas", "menos", "contexto", "trozo"})
        comprobar("hay al menos una cabecera de trozo",
                  any(l.tipo == "trozo" and l.texto.startswith("@@") for l in d))

        mas = [l.texto for l in d if l.tipo == "mas"]
        menos = [l.texto for l in d if l.tipo == "menos"]
        comprobar("el diff detecta la linea de DNS que cambio",
                  any("1.1.1.1" in t for t in mas) and any("8.8.8.8" in t for t in menos))
        comprobar("el texto de la linea no arrastra el marcador +/-",
                  not any(t.startswith(("+", "-")) for t in mas + menos))

        # La PSK rotada: se ve que la linea cambio, pero ninguno de los dos
        # valores llega al navegador.
        comprobar("el diff no filtra la PSK vieja ni la nueva",
                  not any("Clave-Vieja-2024" in l.texto or "Clave-Nueva-2026" in l.texto
                          for l in d))
        psk = [l for l in d if "wpa2-pre-shared-key" in l.texto]
        comprobar("la linea de la PSK aparece en el diff (par -/+)", len(psk) == 2)
        comprobar("las dos lineas de la PSK quedan marcadas como ocultas",
                  len(psk) == 2 and all(l.oculta for l in psk))
        comprobar("las dos lineas de la PSK son identicas tras enmascarar",
                  len(psk) == 2 and psk[0].texto == psk[1].texto)
        comprobar("la linea del DNS NO queda marcada como oculta",
                  all(not l.oculta for l in d if "servers=" in l.texto))

        # Con ocultar_secretos=False sale en claro: es una decision consciente.
        crudo = diferencia(repo, RUTA, v[1].commit, ocultar_secretos=False)
        comprobar("con ocultar_secretos=False la PSK sale en claro",
                  any("Clave-Nueva-2026" in l.texto for l in crudo))
        comprobar("con ocultar_secretos=False nada queda marcado como oculto",
                  all(not l.oculta for l in crudo))

        comprobar("un commit que no toco ese archivo da diff vacio",
                  diferencia(repo, otra, v[1].commit) == [])

        # --- contenido ------------------------------------------------------
        print("\n--- contenido ---")
        texto = contenido(repo, RUTA, v[-1].commit)
        comprobar("el contenido recupera la version antigua",
                  "8.8.8.8" in texto and "1.1.1.1" not in texto)
        comprobar("el contenido tapa la PSK", "Clave-Vieja-2024" not in texto
                  and MARCA in texto)
        comprobar("el contenido tapa el password PPPoE", "pppoe-viejo" not in texto)
        comprobar("el contenido conserva lo que no es secreto",
                  "/ip dns" in texto and "name=cliente01" in texto)
        crudo = contenido(repo, RUTA, v[-1].commit, ocultar_secretos=False)
        comprobar("con ocultar_secretos=False el contenido sale entero",
                  "Clave-Vieja-2024" in crudo and "pppoe-viejo" in crudo)
        comprobar("el contenido sin enmascarar es el del repo",
                  crudo == V1)

        # --- enmascarado y seguridad ---------------------------------------
        probar_enmascarado()
        probar_seguridad(repo)

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallidas:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
