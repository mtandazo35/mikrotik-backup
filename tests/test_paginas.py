"""Prueba de que el HTML que sirve el panel esta BIEN FORMADO.

Existe por dos averias reales, y las dos tienen la misma forma: el servidor
contestaba 200, el HTML "parecia" correcto, ninguna prueba se quejaba, y lo que
veia la persona estaba roto.

  - una llave de mas en el CSS. El navegador descarta TODO lo que viene despues
    de una llave que sobra, asi que media hoja de estilos dejo de aplicarse y el
    login aparecio descentrado. Lo encontro el usuario mirando la pantalla.
  - un `const` declarado dos veces en el mismo ambito. Eso es un SyntaxError, y
    un SyntaxError se detecta al PARSEAR: no se ejecuta ni la primera linea y la
    pagina se queda en blanco.
  - el <script> del panel se servia DESPUES de </html>, 13 KB fuera del
    documento. Funcionaba solo porque los navegadores recuperan de eso metiendo
    el script en el body ellos mismos.

Ninguna de las tres la ve una prueba que compruebe "la funcion devuelve algo con
la palabra tal dentro". Hay que mirar la estructura.

Se renderiza llamando a paginas.py directamente: ni servidor, ni red, ni datos.

Ejecutar:  python -m tests.test_paginas
"""

import html.parser
import re
from pathlib import Path

from mkbackup import paginas as pg
from mkbackup.config import Config

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


# Etiquetas que se cierran solas: si se contaran, todo saldria descuadrado.
HUECAS = {"br", "img", "input", "meta", "link", "hr", "source", "col", "area",
          "base", "embed", "param", "track", "wbr",
          # las del SVG de la marca y de las graficas
          "path", "circle", "rect", "line", "polygon", "polyline", "use", "stop"}


class Etiquetas(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pila = []
        self.problemas = []

    def handle_starttag(self, tag, attrs):
        if tag not in HUECAS:
            self.pila.append(tag)

    def handle_endtag(self, tag):
        if tag in HUECAS:
            return
        if not self.pila:
            self.problemas.append(f"</{tag}> sin abrir")
        elif self.pila[-1] == tag:
            self.pila.pop()
        elif tag in self.pila:
            while self.pila and self.pila[-1] != tag:
                self.problemas.append(f"<{self.pila.pop()}> sin cerrar")
            self.pila.pop()
        else:
            self.problemas.append(f"</{tag}> sin abrir")


def _sin_comentarios_css(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _bloques(texto: str, etiqueta: str):
    return re.findall(rf"<{etiqueta}[^>]*>(.*?)</{etiqueta}>", texto, re.S | re.I)


def _consts_de_primer_nivel(js: str):
    """Nombres declarados con const en el ambito de MAS AFUERA del script.

    Solo el primer nivel: dos funciones distintas pueden tener cada una su
    `const total` sin que pase nada, y contarlas todas daria falsos avisos sin
    parar. El fallo que hubo de verdad fue un `const` repetido arriba del todo.

    Se cuentan llaves, parentesis y corchetes para saber la profundidad, y se
    saltan cadenas, plantillas, expresiones regulares y comentarios, que es
    donde vive cualquier llave que no cuenta.
    """
    nombres = []
    profundidad = 0
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        if c in "\"'`":
            cierre = c
            i += 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == cierre:
                    break
                i += 1
        elif c == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and js[i + 1] == "*":
            fin = js.find("*/", i + 2)
            i = fin + 1 if fin != -1 else n
        elif c in "{([":
            profundidad += 1
        elif c in "})]":
            profundidad -= 1
        elif profundidad == 0 and js.startswith("const", i):
            antes = js[i - 1] if i else " "
            if not (antes.isalnum() or antes in "_$."):
                m = re.match(r"const\s+([A-Za-z_$][\w$]*)", js[i:])
                if m:
                    nombres.append(m.group(1))
                    i += m.end() - 1
        i += 1
    return nombres


SESION = {
    "nombre": "admin",
    "rol": "admin",
    "etiqueta": "Administrador",
    "puede": {"equipos", "cambios", "usuarios", "auditoria", "ajustes",
              "editar_equipos", "ajustes_editar"},
}


# La Config de VERDAD con sus valores por defecto, no un doble escrito a mano.
# Un doble hay que ir remendandolo cada vez que la pagina lee un campo nuevo, y
# mientras tanto la prueba pasa por los motivos equivocados; con la de verdad,
# si Ajustes empieza a leer algo que no existe, esto se entera.
CFG = Config()


def paginas_a_revisar():
    """(nombre, html) de todo lo que sabe pintar el panel, con datos minimos."""
    salida = [
        ("login", pg.login()),
        ("login con error", pg.login(error="Usuario o clave incorrectos")),
        ("panel", pg.panel(SESION, 3, "America/Guayaquil")),
        ("error 404", pg.error(404, "No existe")),
    ]

    equipo = {
        "nombre": "BTS-Norte-01", "empresa": "Andinanet", "ip": "10.20.1.1",
        "puerto": "22", "grupo": "bts", "intervalo": "", "usuario": "", "clave": "",
    }
    intentos = (
        ("equipos", lambda: pg.equipos([], [], SESION)),
        ("alta de equipo", lambda: pg.formulario_equipo(equipo, [], True)),
        ("alta con errores", lambda: pg.formulario_equipo(
            equipo, ["La IP no es valida", "Falta el nombre"], True)),
        ("importar", lambda: pg.importar(True)),
        ("importar sin xlsx", lambda: pg.importar(False)),
        ("cambios", lambda: pg.cambios([], SESION)),
        ("usuarios", lambda: pg.usuarios([], {}, {}, "admin", SESION)),
        ("auditoria", lambda: pg.auditoria([], [], {}, SESION)),
        ("cuenta", lambda: pg.cuenta("admin", "Administrador", [], SESION)),
        ("ajustes", lambda: pg.ajustes(CFG, {}, sesion=SESION)),
    )
    for nombre, hacer in intentos:
        try:
            salida.append((nombre, hacer()))
        except Exception as exc:  # noqa: BLE001
            # Que una firma cambie no puede dejar la prueba en verde callando:
            # se anota como fallo con el motivo.
            FALLOS.append(f"{nombre}: no se pudo pintar ({type(exc).__name__}: {exc})")
            print(f"[FALLA] {nombre}: no se pudo pintar ({exc})")
    return salida


def ejecutar_barra() -> None:
    """Corre tests/barra.js contra el JS que sale de panel(), con node."""
    import re
    import shutil
    import subprocess
    import tempfile

    print()
    if not shutil.which("node"):
        print("[ -- ] la barra no se ejecuta: no hay node (no cuenta como fallo)")
        return

    html = pg.panel(SESION, 3, "America/Guayaquil")
    guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not guiones:
        comprobar("el panel trae su JavaScript", False)
        return

    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "panel.js"
        js.write_text(guiones[0], encoding="utf-8")
        r = subprocess.run(
            ["node", str(Path(__file__).parent / "barra.js"), str(js)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    for linea in (r.stdout or "").splitlines():
        if linea.startswith("[OK  ]") or linea.startswith("[FALLA]"):
            print(linea)
        elif linea.startswith("---"):
            print(linea)
    if r.returncode != 0:
        FALLOS.append("la geometria de la barra del panel esta mal")
        print((r.stderr or "").strip()[:400])


def main() -> None:
    revisadas = paginas_a_revisar()
    comprobar(f"se pintan las paginas del panel ({len(revisadas)})",
              len(revisadas) >= 10)

    for nombre, texto in revisadas:
        # --- Estructura general --------------------------------------------
        comprobar(f"{nombre}: es un documento completo",
                  texto.lstrip().startswith("<!doctype html>")
                  and texto.rstrip().endswith("</html>"))

        # Lo de los 13 KB fuera del documento: NADA puede ir despues de
        # </html>, y el <script> tiene que estar dentro del body.
        cola = texto[texto.rindex("</html>") + len("</html>"):].strip()
        comprobar(f"{nombre}: no hay nada servido despues de </html> "
                  f"({len(cola)} bytes)", cola == "")
        if "<script" in texto and "</body>" in texto:
            comprobar(f"{nombre}: el script va DENTRO del body",
                      texto.index("<script") < texto.index("</body>"))

        # --- CSS: una llave de mas apaga media hoja ------------------------
        css = _sin_comentarios_css("\n".join(_bloques(texto, "style")))
        if css.strip():
            abre, cierra = css.count("{"), css.count("}")
            comprobar(f"{nombre}: las llaves del CSS cuadran ({abre}/{cierra})",
                      abre == cierra)
            profundidad, se_paso = 0, False
            for c in css:
                profundidad += (c == "{") - (c == "}")
                se_paso = se_paso or profundidad < 0
            comprobar(f"{nombre}: ninguna llave del CSS cierra antes de tiempo",
                      not se_paso)

        # --- JS: un const repetido arriba deja la pagina en blanco ---------
        for i, js in enumerate(_bloques(texto, "script")):
            if not js.strip():
                continue
            nombres = _consts_de_primer_nivel(js)
            repes = sorted({c for c in nombres if nombres.count(c) > 1})
            comprobar(f"{nombre}: script {i} sin const repetida arriba {repes or ''}",
                      not repes)
            comprobar(f"{nombre}: script {i} con las llaves cuadradas",
                      js.count("{") == js.count("}"))
            comprobar(f"{nombre}: script {i} sin '</script>' partiendo el bloque",
                      "</script>" not in js)

        # --- Etiquetas ------------------------------------------------------
        p = Etiquetas()
        p.feed(texto)
        comprobar(f"{nombre}: las etiquetas cuadran "
                  f"{(p.problemas + p.pila)[:3] or ''}",
                  not p.problemas and not p.pila)

    # --- Lo que NUNCA se puede servir ---------------------------------------
    # El panel pinta usuarios y ajustes: un hash o una clave que se cuele en el
    # HTML se lo lleva cualquiera con la pagina abierta.
    for nombre, texto in revisadas:
        for pista in ("pbkdf2_", "clave_hash", "password:"):
            comprobar(f"{nombre}: no sirve '{pista}'", pista not in texto)

    # --- El escapado, que es lo que separa un panel de un XSS ---------------
    veneno = '<script>alert(1)</script>'
    pintada = pg.formulario_equipo(
        {"nombre": veneno, "empresa": veneno, "ip": "1.2.3.4", "puerto": "22",
         "grupo": "", "intervalo": "", "usuario": "", "clave": ""},
        [veneno], True,
    )
    comprobar("un nombre de equipo con <script> dentro sale escapado",
              "<script>alert(1)</script>" not in pintada)
    comprobar("y tambien cuando viaja en un mensaje de error",
              pintada.count("&lt;script&gt;") >= 2)
    comprobar("esc() tapa las comillas, no solo los angulos",
              pg.esc('a"b\'c<d>e&f') == "a&quot;b&#x27;c&lt;d&gt;e&amp;f")

    # --- 8. La barra del panel, EJECUTADA -----------------------------------
    # Lo de arriba mira el HTML; la barra la dibuja el JavaScript en el
    # navegador, y ahi "compila" no basta: un reparto de anchos puede compilar
    # perfectamente y salirse de su caja. Paso de verdad -con 300 equipos y 1
    # fallo la suma daba 100.27 y el recorte se comia el sobrante, dejando el
    # trozo del fallo MAS estrecho que el minimo que queria garantizarse- y no
    # habia forma de verlo sin ejecutarlo.
    #
    # Necesita node. Si no esta, se dice y no se cuenta como fallo: no se le va
    # a exigir node a quien solo quiere respaldar routers.
    ejecutar_barra()

    # --- El login SIEMPRE centrado -----------------------------------------
    # Esto se ha ido a un lado dos veces, y las dos las tuvo que ver el usuario
    # en la pantalla porque nada lo comprobaba. Estuvo puesto a proposito (la
    # imagen de fondo suele tener su motivo y una tarjeta en medio lo tapa),
    # pero lo que se ve al abrir es un formulario descolgado en una esquina,
    # que parece un fallo de maquetacion. Con fondo y sin el, va centrado.
    print()
    for nombre, html_login in (("sin fondo", pg.login()),
                               ("con imagen de fondo", pg.login(fondo="abc123")),
                               ("con error", pg.login(error="Clave incorrecta")),
                               ("con fondo y error",
                                pg.login(error="Clave incorrecta", fondo="abc123"))):
        estilos = "\n".join(_bloques(html_login, "style"))
        # justify-items: start / end / left / right en el body es lo que la
        # descuelga. Se busca cualquiera de ellos.
        descolgada = re.search(
            r"justify-items\s*:\s*(start|end|left|right|flex-start|flex-end)",
            estilos)
        comprobar(f"login {nombre}: la tarjeta no se descuelga a un lado "
                  f"({descolgada.group(0) if descolgada else 'centrada'})",
                  descolgada is None)
        comprobar(f"login {nombre}: hay un centrado explicito",
                  re.search(r"(justify-items\s*:\s*center|place-items\s*:\s*center)",
                            estilos) is not None)
        # Un relleno lateral muy grande centra "dentro de una caja corrida", que
        # a ojo se ve igual de torcido que un justify-items: start.
        laterales = re.findall(r"padding:\s*[^;]*clamp\([^)]*vw[^)]*\)", estilos)
        comprobar(f"login {nombre}: ni un relleno lateral que lo desplace "
                  f"{laterales[:1] or ''}", not laterales)

    # --- Las graficas del panel, las dos formas -----------------------------
    # Las tartas se quitaron en un rediseno "porque una barra se lee mejor", y
    # el usuario las echo en falta. Tenia razon: la barra contesta "cuanta
    # flota esta cubierta" y las tartas contestan "como se reparte", que no es
    # la misma pregunta. Se comprueba que estan las dos cosas para que no
    # vuelva a desaparecer ninguna en la proxima limpieza.
    print()
    panel_html = pg.panel(SESION, 3, "America/Guayaquil")
    for id_svg, que in (("b-estado", "la barra de cobertura"),
                        ("t-estado", "la tarta del resultado"),
                        ("t-clientes", "la tarta por cliente")):
        comprobar(f"el panel trae {que}", f'id="{id_svg}"' in panel_html)
    for id_ul in ("l-estado", "l-tarta-estado", "l-clientes"):
        comprobar(f"y su leyenda ({id_ul})", f'id="{id_ul}"' in panel_html)
    # Cada grafica necesita su propia leyenda: si dos compartieran id, la
    # segunda pisaria a la primera y una de las dos se quedaria en blanco.
    ids = re.findall(r'id="(l-[\w-]+)"', panel_html)
    comprobar(f"ninguna leyenda comparte id con otra {ids}",
              len(ids) == len(set(ids)))

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
