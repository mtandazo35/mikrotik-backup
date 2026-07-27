"""HTML del panel. Aqui vive todo lo que se ve; en web.py, todo lo que decide.

Estan separados porque son dos cosas que cambian por motivos distintos: el
color de una etiqueta no deberia obligar a releer el codigo que valida
sesiones, y al reves.

Todo se genera en el servidor salvo la pantalla de estado, que es la unica que
se refresca sola cada pocos segundos. Las demas paginas son formularios y
tablas: hacerlas en JavaScript solo anadiria una capa que puede romperse entre
el dato y quien lo mira.

Regla de oro de este modulo: TODO lo que venga de fuera (nombres de equipos,
empresas, mensajes de error de git, avisos del inventario) pasa por `esc`
antes de entrar en el HTML. Un equipo llamado `<script>` no es un ataque
probable en una red de gestion, pero el dia que el inventario se cargue desde
un Excel que mando un tercero, esa costumbre es lo unico que separa un panel
de un problema.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from html import escape


# Una marca dibujada en SVG y no una imagen: el panel no carga nada externo
# (lo dice su propia cabecera CSP), y un archivo mas seria un archivo mas que
# servir. Es un disco con una flecha hacia abajo: guardar. Hereda el color del
# texto, asi que funciona igual en claro y en oscuro sin dos versiones.
MARCA = (
    '<svg class="marca-logo" viewBox="0 0 24 24" width="20" height="20" '
    'aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z"/>'
    '<path d="M4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7"/>'
    '<path d="M12 11v6m0 0-2.5-2.5M12 17l2.5-2.5"/>'
    "</svg>"
)

# Titulo y una linea que explique para que sirve la pantalla. La pestana dice
# donde estas; esto dice que puedes hacer aqui, que es lo que de verdad falta
# cuando alguien entra por primera vez.
TITULOS = {
    "estado": ("Estado", "Como fue el ultimo respaldo y cuando toca el siguiente"),
    "equipos": ("Equipos", "La flota: altas, bajas y a que ritmo se consulta cada router"),
    "cambios": ("Cambios", "Que ha cambiado en las configuraciones y cuando"),
    "ajustes": ("Ajustes", "Cada cuanto se respalda y con que credenciales se entra"),
    "usuarios": ("Usuarios", "Quien entra al panel y que puede hacer"),
    "auditoria": ("Auditoria", "Quien entro, quien lo intento y quien toco que"),
}


def esc(valor) -> str:
    """Escapa para HTML. Acepta None y numeros sin que haya que pensarlo."""
    return escape("" if valor is None else str(valor), quote=True)


def fecha(iso: str, zona=None) -> str:
    """Una marca ISO en hora local y legible: '26/07/2026 12:35'.

    Lo que se guarda en disco es UTC y asi se queda; la zona solo se aplica
    aqui, al pintar. Un '2026-07-26T17:35:30.729223+00:00' es correcto pero no
    se lee: quien mira el panel quiere saber si eso fue hace un rato.
    """
    if not iso:
        return "-"
    try:
        momento = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    if zona is not None:
        momento = momento.astimezone(zona)
    return momento.strftime("%d/%m/%Y %H:%M")


def _opciones(valores, elegido: str) -> str:
    partes = [f'<option value="">{"Todas" if not elegido else "Todas"}</option>']
    for v in valores:
        sel = " selected" if v == elegido else ""
        partes.append(f'<option value="{esc(v)}"{sel}>{esc(v)}</option>')
    return "".join(partes)


def filtros(accion: str, empresas, grupos, actual: dict) -> str:
    """Una sola fila de filtros por encima de lo que filtra.

    Van en la pagina y no dentro de cada tarjeta a proposito: un filtro
    metido en una tabla parece que solo afecta a esa tabla, y aqui afecta a
    todo lo que hay debajo.
    """
    hay = any(actual.get(k) for k in ("empresa", "grupo", "q"))
    limpiar = (
        f'<a class="boton secundario" href="{accion}">Quitar filtros</a>' if hay else ""
    )
    return f"""
  <form class="filtros" method="get" action="{accion}">
    <div>
      <label for="f-empresa">Empresa</label>
      <select id="f-empresa" name="empresa" onchange="this.form.submit()">{_opciones(empresas, actual.get("empresa", ""))}</select>
    </div>
    <div>
      <label for="f-grupo">Grupo</label>
      <select id="f-grupo" name="grupo" onchange="this.form.submit()">{_opciones(grupos, actual.get("grupo", ""))}</select>
    </div>
    <div>
      <label for="f-q">Nombre o IP</label>
      <input id="f-q" type="text" name="q" value="{esc(actual.get("q", ""))}"
             placeholder="10.20. o BTS-" autocomplete="off">
    </div>
    <div class="filtro-botones">
      <button type="submit">Filtrar</button>
      {limpiar}
    </div>
  </form>
"""


ESTILO = """
  /* Tokens. Los valores viven aqui una sola vez: si manana hay que apretar el
     espaciado o suavizar las esquinas, se toca un numero y no cuarenta reglas. */
  :root {
    --fondo: #f2f3f5; --panel: #fff; --borde: #e3e5e9; --texto: #16181c;
    --suave: #616875; --ok: #1a7f37; --cambio: #9a6700; --fallo: #b42318;
    --curso: #0b5cad; --barra: #e7e9ed; --marca: #f4f6f9;
    --sombra: 0 1px 2px rgba(16,20,28,.04), 0 2px 6px rgba(16,20,28,.05);
    --radio: 10px; --radio-chico: 7px;
    --alto-control: 2.3rem;

    /* Colores de las graficas. Los de IDENTIDAD (que cliente es cada sector)
       se validaron contra la superficie real de este panel: banda de
       luminosidad, suelo de croma, separacion para daltonismo (peor par
       adyacente 9.1 en claro y 8.4 en oscuro, sobre un minimo de 8) y suelo
       de vision normal (19.6 y 19.3, sobre 15). El ORDEN de los slots es el
       mecanismo de seguridad, no decoracion: para un sexto cliente NO se
       genera un color nuevo (seria indistinguible de otro bajo daltonismo),
       se pliega en "Otros". */
    --v1: #2a78d6; --v2: #eb6834; --v3: #1baf7a; --v4: #eda100; --v5: #e87ba4;

    /* Colores de ESTADO. Son otra cosa: significan bien/regular/mal, asi que
       estan reservados y nunca se usan para identidad ni al reves. Tampoco
       cambian con el tema: son los mismos cuatro pasos en claro y en oscuro,
       porque el rojo de "fallo" no puede depender de como tenga el sistema
       operativo quien mira. Van SIEMPRE con su etiqueta al lado: el color
       solo no puede ser el que cuente que algo fallo. */
    --v-bien: #0ca30c; --v-cambio: #fab219; --v-fallo: #d03b3b; --v-nada: #898781;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      /* En oscuro las superficies se separan por LUMINOSIDAD, no por borde:
         el fondo baja y el panel sube, que es lo que da profundidad cuando la
         sombra no se ve. */
      --fondo: #101317; --panel: #191d23; --borde: #272d35; --texto: #e7e9ec;
      --suave: #98a1af; --ok: #46b46a; --cambio: #d9a400; --fallo: #f0685c;
      --curso: #5aa7f5; --barra: #272d35; --marca: #1f242b;
      --sombra: none;
      /* Los mismos cinco tonos, re-escalonados para el fondo oscuro; no es un
         volteo automatico del tema claro, se validaron aparte contra #1c2024. */
      --v1: #3987e5; --v2: #d95926; --v3: #199e70; --v4: #c98500; --v5: #d55181;
    }
  }

  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 1.75rem 1.5rem 3rem; background: var(--fondo);
    color: var(--texto);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .contenedor { max-width: 1140px; margin: 0 auto; }
  a { color: var(--curso); text-underline-offset: 2px; }

  /* Un aro de foco visible y uniforme en TODO lo que se puede enfocar. Sin
     esto, quien navega con el teclado se pierde: el aro por defecto del
     navegador desaparece sobre los fondos de color. */
  :focus-visible {
    outline: 2px solid var(--curso); outline-offset: 2px; border-radius: 4px;
  }

  /* --- Cabecera y navegacion --- */
  header { display: flex; align-items: baseline; gap: .75rem; margin-bottom: 1rem;
           flex-wrap: wrap; }
  h1 { font-size: 1.15rem; margin: 0; letter-spacing: -.015em; font-weight: 650; }
  h1 a, h1 { color: inherit; text-decoration: none;
             display: inline-flex; align-items: center; gap: .5rem; }
  .marca-logo { color: var(--curso); flex: none; }

  /* Titulo de la pantalla. Da el punto de entrada que faltaba: antes se caia
     directamente en una tarjeta y no habia nada que dijera donde estabas ni
     para que servia. */
  .encabezado { margin: 0 0 1.25rem; }
  .encabezado .titulo { font-size: 1.5rem; font-weight: 650; margin: 0 0 .2rem;
                        letter-spacing: -.02em; text-transform: none;
                        color: var(--texto); }
  .encabezado .sub { font-size: .9rem; }
  .sub { color: var(--suave); font-size: .85rem; }
  .sesion { margin-left: auto; display: flex; align-items: center; gap: .6rem; }
  .quien { color: var(--suave); font-size: .85rem; text-decoration: none;
           display: inline-flex; align-items: center; }
  .quien:hover { color: var(--texto); }
  .rol { margin-left: .45rem; font-size: .7rem; padding: .12rem .5rem;
         border: 1px solid var(--borde); border-radius: 99px; color: var(--suave); }
  nav { display: flex; gap: .15rem; margin-bottom: 1.5rem; flex-wrap: wrap;
        border-bottom: 1px solid var(--borde); }
  nav a { padding: .55rem .85rem; text-decoration: none; color: var(--suave);
          border-bottom: 2px solid transparent; margin-bottom: -1px;
          font-size: .9rem; border-radius: var(--radio-chico) var(--radio-chico) 0 0;
          transition: color .12s ease, background .12s ease; }
  nav a:hover { color: var(--texto); background: var(--marca); }
  nav a.activo { color: var(--curso); border-bottom-color: var(--curso);
                 font-weight: 600; }

  /* --- Bloques --- */
  .tarjeta { background: var(--panel); border: 1px solid var(--borde);
             border-radius: var(--radio); padding: 1.15rem 1.3rem;
             margin-bottom: 1rem; box-shadow: var(--sombra); }
  h2 { font-size: .75rem; text-transform: uppercase; letter-spacing: .06em;
       color: var(--suave); margin: 0 0 .85rem; font-weight: 650; }
  .barra-acciones { display: flex; gap: .6rem; align-items: center;
                    margin-bottom: 1rem; flex-wrap: wrap; }
  .crece { margin-left: auto; }

  /* --- Aviso de ciclo (solo en curso o interrumpido) --- */
  .ciclo { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
           font-size: .95rem; }
  .punto { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .ciclo.corriendo { color: var(--curso); }
  .ciclo.corriendo .punto { background: var(--curso); animation: latir 1.4s infinite; }
  .ciclo.interrumpida { color: var(--cambio); }
  .ciclo.interrumpida .punto { background: var(--cambio); }
  @keyframes latir { 0%,100% { opacity: 1; } 50% { opacity: .25; } }
  .ciclo .detalle { color: var(--suave); font-size: .87rem; }
  .barra { height: 6px; width: 100%; background: var(--barra); border-radius: 99px;
           overflow: hidden; margin-top: .5rem; }
  .barra > div { height: 100%; background: var(--curso); border-radius: 99px;
                 transition: width .4s ease; }

  /* --- Cifras --- */
  .cifras { display: grid; gap: .75rem; margin-bottom: 1rem;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .cifra { background: var(--panel); border: 1px solid var(--borde);
           border-radius: var(--radio); padding: .85rem 1.05rem;
           box-shadow: var(--sombra); }
  /* Sin tabular-nums a proposito: en un numero grande y suelto, los digitos de
     ancho fijo dejan huecos que se ven. Va en las tablas, donde si hay que
     alinear columnas. */
  .cifra b { display: block; font-size: 1.65rem; font-weight: 650;
             letter-spacing: -.025em; line-height: 1.15; }
  .cifra.malo b { color: var(--fallo); }
  .cifra.bien b { color: var(--ok); }
  .cifra span { color: var(--suave); font-size: .74rem; text-transform: uppercase;
                letter-spacing: .05em; display: block; margin-top: .15rem; }

  /* --- Filtros --- */
  .filtros { display: flex; gap: .8rem; align-items: flex-end; flex-wrap: wrap;
             background: var(--panel); border: 1px solid var(--borde);
             border-radius: var(--radio); padding: .95rem 1.15rem;
             margin-bottom: 1rem; box-shadow: var(--sombra); }
  .filtros > div { display: flex; flex-direction: column; min-width: 150px; }
  .filtros .filtro-botones { flex-direction: row; gap: .5rem; min-width: 0;
                             align-items: center; }
  .filtros label { margin-bottom: .3rem; }
  .filtros select, .filtros input:not([type=checkbox]) { min-width: 150px; }
  /* La casilla ocupa la misma altura que los demas controles para que quede
     alineada con ellos, en vez de flotando a media altura de la fila. */
  .filtros .casilla { height: var(--alto-control); margin: 0; white-space: nowrap; }

  /* --- Graficas --- */
  .tartas { display: grid; gap: 1rem; margin-bottom: 1rem;
            grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); }
  .grafica { display: flex; gap: 1.3rem; align-items: center; flex-wrap: wrap; }
  .grafica svg { width: 172px; height: 172px; flex: none; }
  /* El hueco entre sectores se hace con el color del fondo, no con un borde:
     un borde alrededor de cada sector engorda la tarta y ensucia los colores. */
  .grafica svg path, .grafica svg circle { stroke: var(--panel); stroke-width: 2; }
  .grafica svg path { transition: opacity .12s ease; }
  .grafica svg path:hover { opacity: .8; }
  .centro-cifra { font-size: 1.5rem; font-weight: 650; fill: var(--texto);
                  letter-spacing: -.02em; }
  .centro-texto { font-size: .6rem; fill: var(--suave); letter-spacing: .06em; }
  .leyenda { list-style: none; margin: 0; padding: 0; flex: 1; min-width: 165px;
             font-size: .87rem; }
  .leyenda li { display: flex; align-items: center; gap: .55rem; padding: .3rem 0;
                border-bottom: 1px solid var(--borde); }
  .leyenda li:last-child { border-bottom: 0; }
  .leyenda .marca { width: 9px; height: 9px; border-radius: 3px; flex: none; }
  .leyenda .nom { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .leyenda .val { margin-left: auto; flex: none; color: var(--suave);
                  font-variant-numeric: tabular-nums; }
  .sin-datos { color: var(--suave); padding: .5rem 0; }
  .casillas { display: grid; gap: .3rem;
              grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); }
  .casilla { display: flex; align-items: center; gap: .55rem; font-size: .88rem;
             color: var(--texto); text-transform: none; letter-spacing: 0;
             margin: 0; padding: .25rem 0; cursor: pointer; }
  .casilla input { width: auto; }
  .nivel { min-width: 130px; }
  details summary { cursor: pointer; padding: .35rem 0; color: var(--suave); }
  details summary:hover { color: var(--texto); }

  /* --- Tablas --- */
  .tabla-caja { overflow-x: auto; margin: 0 -.35rem; padding: 0 .35rem; }
  table { border-collapse: collapse; width: 100%; font-size: .89rem;
          font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: .55rem .7rem;
           border-bottom: 1px solid var(--borde); white-space: nowrap; }
  th { color: var(--suave); font-weight: 600; font-size: .72rem;
       text-transform: uppercase; letter-spacing: .05em;
       position: sticky; top: 0; background: var(--panel); z-index: 1; }
  tbody tr:last-child td { border-bottom: 0; }
  tbody tr { transition: background .1s ease; }
  tbody tr:hover { background: var(--marca); }
  tbody td:first-child { font-weight: 550; color: var(--texto); }
  td.acciones { text-align: right; }
  .vacio { color: var(--suave); }
  td.vacio, .tabla-caja td[colspan] { text-align: center; padding: 2rem 1rem;
                                      font-weight: 400; }

  /* --- Etiquetas --- */
  .etiqueta { font-size: .72rem; font-weight: 650; padding: .16rem .55rem;
              border-radius: 99px; border: 1px solid currentColor;
              display: inline-block; letter-spacing: .01em; }
  .e-pendiente { color: var(--suave); }
  .e-en_curso { color: var(--curso); }
  .e-sin_cambios { color: var(--ok); }
  .e-cambio { color: var(--cambio); }
  .e-fallo { color: var(--fallo); }
  .e-provisional { color: var(--cambio); }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         font-size: .85em; color: var(--suave); }

  /* --- Formularios --- */
  label { display: block; font-size: .74rem; color: var(--suave);
          margin-bottom: .35rem; text-transform: uppercase; letter-spacing: .05em;
          font-weight: 600; }
  input[type=text], input[type=password], input[type=number], input[type=file],
  select {
    width: 100%; height: var(--alto-control); padding: 0 .7rem; font: inherit;
    font-size: .92rem; color: var(--texto); background: var(--fondo);
    border: 1px solid var(--borde); border-radius: var(--radio-chico);
    transition: border-color .12s ease, background .12s ease;
  }
  input[type=file] { padding: .4rem .6rem; height: auto; }
  input:hover, select:hover { border-color: var(--suave); }
  input:focus, select:focus { outline: 2px solid var(--curso); outline-offset: -1px;
                              background: var(--panel); }
  input::placeholder { color: var(--suave); opacity: .7; }
  .campo { margin-bottom: 1.05rem; }
  .pista { font-size: .8rem; color: var(--suave); margin-top: .35rem;
           text-transform: none; letter-spacing: 0; font-weight: 400;
           line-height: 1.45; }
  .rejilla { display: grid; gap: 0 1.1rem;
             grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }

  /* Un campo con un boton pegado a la derecha. El input se lleva el sitio que
     sobre (1fr) y el boton solo el suyo (auto), y en pantalla estrecha se
     apilan solos: sin el minmax, el input se comprimia hasta no dejar leer lo
     que hay escrito, que es justo lo que este boton acaba de rellenar. */
  .con-boton { display: grid; gap: .5rem; align-items: start;
               grid-template-columns: minmax(0, 1fr) auto; }
  .con-boton button { white-space: nowrap; }
  @media (max-width: 460px) {
    .con-boton { grid-template-columns: 1fr; }
  }

  button, .boton {
    font: inherit; font-size: .89rem; font-weight: 600; cursor: pointer;
    height: var(--alto-control); padding: 0 1.05rem;
    border-radius: var(--radio-chico); border: 1px solid transparent;
    text-decoration: none; display: inline-flex; align-items: center;
    justify-content: center; color: #fff; background: var(--curso);
    transition: filter .12s ease, border-color .12s ease, color .12s ease;
    white-space: nowrap;
  }
  button:hover, .boton:hover { filter: brightness(1.08); }
  button:active, .boton:active { filter: brightness(.94); }
  .secundario { background: none; color: var(--suave); border-color: var(--borde); }
  .secundario:hover { color: var(--texto); border-color: var(--suave);
                      background: var(--marca); filter: none; }
  .peligro { background: none; color: var(--fallo); border-color: var(--borde); }
  .peligro:hover { border-color: var(--fallo); background: var(--marca);
                   filter: none; }
  .mini { font-size: .8rem; height: 1.85rem; padding: 0 .7rem; font-weight: 500; }

  /* --- Mensajes --- */
  .error, .aviso, .bien {
    border-left: 3px solid; border-radius: 0 var(--radio-chico) var(--radio-chico) 0;
    padding: .5rem .75rem; margin: 0 0 .75rem; font-size: .88rem;
    line-height: 1.45;
  }
  .error { border-color: var(--fallo); color: var(--fallo);
           background: color-mix(in srgb, var(--fallo) 7%, transparent); }
  .aviso { border-color: var(--cambio); color: var(--suave);
           background: color-mix(in srgb, var(--cambio) 8%, transparent); }
  .bien  { border-color: var(--ok); color: var(--ok);
           background: color-mix(in srgb, var(--ok) 7%, transparent); }

  /* --- Diferencias --- */
  .diff { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          font-size: .82rem; line-height: 1.55; overflow-x: auto;
          background: var(--fondo); border: 1px solid var(--borde);
          border-radius: var(--radio-chico); padding: .5rem 0; }
  .diff div { padding: 0 .85rem; white-space: pre; }
  .d-mas { background: color-mix(in srgb, var(--ok) 13%, transparent);
           color: var(--ok); }
  .d-menos { background: color-mix(in srgb, var(--fallo) 12%, transparent);
             color: var(--fallo); }
  .d-trozo { color: var(--suave); background: var(--marca); margin: .45rem 0 .2rem;
             padding-top: .15rem; padding-bottom: .15rem; }
  .d-oculta { border-right: 3px solid var(--cambio); }
  .leyenda-diff { font-size: .8rem; color: var(--suave); margin-top: .6rem; }

  footer { color: var(--suave); font-size: .8rem; text-align: center;
           margin-top: 2rem; }
  .caido { color: var(--fallo); }

  /* Quien pide menos movimiento no deberia tener un punto parpadeando ni
     transiciones: es una preferencia del sistema, no un capricho. */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: .001ms !important; animation-iteration-count: 1 !important;
      transition-duration: .001ms !important;
    }
  }

  /* --- Anchos de contenido ---
     Los formularios se leen mal en lineas largas: una columna de texto comoda
     ronda los 60-70 caracteres. Por eso se acotan, pero con max-width y no con
     width, para que en una pantalla estrecha ocupen lo que haya. */
  /* Dos cuadros que van juntos porque son la misma clase de cosa (lo que se
     cambia), y que se parten solos cuando dejan de caber. `align-items: start`
     evita que el corto se estire hasta la altura del largo: dos cuadros de
     distinta altura se leen mejor que uno con un hueco vacio dentro. */
  .pareja { display: grid; gap: 1rem; align-items: start;
            grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); }

  /* Cabecera de tabla que se puede pulsar para ordenar. El enlace hereda el
     estilo de la cabecera para que no parezca un enlace suelto dentro de la
     tabla, pero cambia el cursor y se subraya al pasar por encima. */
  th a { color: inherit; text-decoration: none; display: inline-block; }
  th a:hover { color: var(--texto); text-decoration: underline; }
  th.ordenada { color: var(--texto); }
  .flecha { font-size: .65em; vertical-align: .1em; }

  /* El resumen se pinta como un boton para que se lea como algo pulsable.
     Plegado ocupa una linea; abierto crece hacia abajo sin tapar la tabla,
     que es preferible a un desplegable flotante que se corta en el movil. */
  details.columnas { margin-bottom: 0; position: relative; }
  details.columnas summary {
    display: inline-flex; align-items: center; gap: .45rem;
    font-size: .89rem; font-weight: 600; color: var(--suave);
    background: var(--panel); border: 1px solid var(--borde);
    border-radius: var(--radio-chico); padding: 0 .95rem;
    height: var(--alto-control); list-style: none; cursor: pointer;
    box-shadow: var(--sombra); transition: color .12s ease, border-color .12s ease;
  }
  details.columnas summary::-webkit-details-marker { display: none; }
  details.columnas summary:hover { color: var(--texto); border-color: var(--suave); }
  details.columnas .caret { display: inline-block; transition: transform .15s ease;
                            font-size: .8em; }
  details.columnas[open] .caret { transform: rotate(90deg); }
  details.columnas .cuantas { color: var(--suave); font-weight: 400;
                              font-size: .82rem; }
  details.columnas > form {
    position: absolute; right: 0; top: calc(100% + .4rem); z-index: 20;
    min-width: 330px; background: var(--panel); border: 1px solid var(--borde);
    border-radius: var(--radio); padding: 1rem 1.15rem;
    box-shadow: 0 6px 20px rgba(16,20,28,.14), 0 1px 3px rgba(16,20,28,.08);
  }
  @media (prefers-color-scheme: dark) {
    details.columnas > form { box-shadow: 0 6px 20px rgba(0,0,0,.5); }
  }

  .vista-fondo { border: 1px solid var(--borde); border-radius: var(--radio-chico);
                 overflow: hidden; margin-top: .3rem; background: var(--fondo); }
  .vista-fondo img { display: block; width: 100%; height: auto; max-height: 170px;
                     object-fit: cover; }

  .paginador { display: flex; align-items: center; justify-content: center;
                gap: .8rem; margin-top: .9rem; flex-wrap: wrap; }
  /* El boton de un extremo no se esconde, se apaga: si desaparece, los otros
     se mueven de sitio al cambiar de pagina y hay que volver a buscarlos. */
  .apagado-boton { opacity: .4; pointer-events: none; }

  .angosto { max-width: 520px; }
  .estrecho { max-width: 620px; }
  .medio { max-width: 760px; }
  .apagado { background: none; box-shadow: none; }
  .lista-alcance { max-height: min(45vh, 300px); overflow-y: auto; }

  h2.aparte { margin-top: 1.6rem; }
  .pista.bajo-titulo { margin: -.5rem 0 1rem; }
  .separado { margin-top: 1.1rem; }
  .casilla.destacada { margin-bottom: .8rem; }

  /* --- Adaptable ---
     Tres cortes, cada uno por un motivo concreto y no por un tamano de moda:
     1024 es donde una tarjeta y su tabla dejan de convivir comodas, 760 es
     donde caben dos columnas de formulario, y 560 es un telefono en vertical. */

  @media (max-width: 1024px) {
    .tartas { grid-template-columns: 1fr; }
  }

  @media (max-width: 760px) {
    body { padding: 1.1rem 1rem 2rem; }
    .contenedor { max-width: 100%; }
    /* Los anchos de lectura dejan de aplicar: aqui manda la pantalla. */
    .angosto, .estrecho, .medio { max-width: 100%; }
    /* Una rejilla de formulario a dos columnas en pantalla estrecha deja los
       campos demasiado justos para escribir en ellos. */
    .rejilla, .casillas { grid-template-columns: 1fr; }
    .cifras { grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: .6rem; }
    .cifra b { font-size: 1.45rem; }

    /* La navegacion pasa a deslizarse en horizontal en vez de partirse en dos
       filas: con las pestanas en varias lineas se pierde la referencia de en
       que seccion se esta. */
    nav { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; }
    nav::-webkit-scrollbar { display: none; }
    nav a { flex: none; }

    header { gap: .5rem; }
    .sesion { margin-left: auto; }

    /* Los filtros ocupan el ancho: tres desplegables estrechos uno al lado del
       otro no se pueden ni tocar con el dedo. */
    .filtros { flex-direction: column; align-items: stretch; }
    .filtros > div { min-width: 0; width: 100%; }
    .filtros select, .filtros input:not([type=checkbox]) { min-width: 0; width: 100%; }
    .filtros .filtro-botones { flex-direction: row; }
    .filtros .filtro-botones button, .filtros .filtro-botones .boton { flex: 1; }

    details.columnas { position: static; width: 100%; }
    details.columnas summary { width: 100%; justify-content: center; }
    details.columnas > form { position: static; min-width: 0; margin-top: .5rem; }

    .grafica { flex-direction: column; align-items: stretch; gap: .8rem; }
    .grafica svg { width: 100%; max-width: 200px; height: auto; align-self: center; }
    .leyenda { min-width: 0; }
  }

  @media (max-width: 560px) {
    body { padding: .9rem .8rem 1.75rem; }
    .tarjeta, .filtros { padding: .95rem 1rem; }
    h1 { font-size: 1.05rem; }
    .cifras { grid-template-columns: 1fr 1fr; }

    /* Los botones de una barra de acciones pasan a repartirse el ancho en vez
       de quedar apelotonados a la izquierda. */
    .barra-acciones { gap: .5rem; }
    .barra-acciones > button, .barra-acciones > .boton { flex: 1 1 auto; }
    .barra-acciones .crece { flex-basis: 100%; margin-left: 0; }

    /* La tabla sigue deslizandose en horizontal, que es mejor que apretar las
       columnas hasta que no se lean; pero se avisa de que hay mas a la derecha
       con un degradado en el borde. */
    .tabla-caja { margin: 0 -1rem; padding: 0 1rem;
                  -webkit-overflow-scrolling: touch; }
    th, td { padding: .5rem .55rem; }
    /* La cabecera fija se quita: en una tabla que se desliza en las dos
       direcciones se despega y queda flotando sobre el contenido. */
    th { position: static; }
  }

  /* En pantallas muy anchas el contenido no crece sin fin: una tabla de 2000px
     obliga a mover la cabeza para seguir una fila. */
  @media (min-width: 1600px) {
    .contenedor { max-width: 1280px; }
  }
"""

# Las secciones de la navegacion. La clave es la que se pasa como `activo`; la
# ultima columna es el permiso que hace falta para verla. Una seccion que no se
# puede usar no se pinta: ensenar una pestana que responde 403 al pulsarla solo
# hace perder el tiempo a quien la ve.
# Primero lo que mira todo el mundo (estado, flota, cambios) y al final lo de
# administrar (cuentas, registro, ajustes). Asi la parte que solo ve un
# administrador queda junta en un extremo, en vez de partir en dos las
# secciones de consulta.
SECCIONES = (
    ("/", "estado", "Estado", "ver"),
    ("/equipos", "equipos", "Equipos", "ver"),
    ("/cambios", "cambios", "Cambios", "ver"),
    ("/usuarios", "usuarios", "Usuarios", "usuarios"),
    ("/auditoria", "auditoria", "Auditoria", "usuarios"),
    ("/ajustes", "ajustes", "Ajustes", "ajustes"),
)


def _navegacion(activo: str, permisos) -> str:
    return "".join(
        f'<a href="{ruta}" class="{"activo" if clave == activo else ""}">{texto}</a>'
        for ruta, clave, texto, permiso in SECCIONES
        if permiso in permisos
    )


def envoltura(
    titulo: str,
    cuerpo: str,
    sesion=None,
    activo: str = "",
    cabeza: str = "",
    pie: str = "",
    guion: str = "",
) -> str:
    """El esqueleto comun de todas las paginas de dentro.

    `sesion` es un dict con nombre, rol, etiqueta del rol y el conjunto de
    permisos. Va como dict y no como objeto para que este modulo no dependa
    del de usuarios: aqui solo se pinta.

    `guion` es el JavaScript de la pagina, y existe para que entre DENTRO del
    body. Antes se pegaba detras de lo que devolvia esta funcion, o sea despues
    de </html>, y el panel funcionaba igual porque los navegadores recuperan de
    eso metiendo el script en el body ellos mismos. Que funcione por la
    tolerancia del navegador y no porque este bien no es una base: cualquier
    cosa que lea el HTML en serio -un proxy que reescribe, una CSP que firma
    los scripts, un parser estricto- se encuentra 13 KB de codigo fuera del
    documento.
    """
    sesion = sesion or {}
    permisos = sesion.get("puede", set())
    titulo_pagina, descripcion = TITULOS.get(activo, ("", ""))
    encabezado = ""
    if titulo_pagina:
        encabezado = (
            f'<div class="encabezado"><h2 class="titulo">{esc(titulo_pagina)}</h2>'
            f'<p class="sub">{esc(descripcion)}</p></div>'
        )
    barra = ""
    if sesion.get("nombre"):
        barra = (
            '<div class="sesion">'
            f'<a class="quien" href="/cuenta" title="Cambiar mi clave">'
            f'{esc(sesion["nombre"])}'
            f'<span class="rol">{esc(sesion.get("etiqueta", ""))}</span></a>'
            '<form method="post" action="/salir" style="display:inline">'
            '<button class="secundario mini" type="submit">Salir</button>'
            "</form></div>"
        )
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(titulo)}</title>
<style>{ESTILO}</style>
</head>
<body>
<div class="contenedor">
  <header>
    <h1><a href="/">{MARCA}<span>mkbackup</span></a></h1>
    {cabeza}
    {barra}
  </header>
  <nav>{_navegacion(activo, permisos)}</nav>
  {encabezado}
  {cuerpo}
  <footer>{pie}</footer>
</div>
{f"<script>{guion}</script>" if guion else ""}
</body>
</html>
"""


# --- Login ------------------------------------------------------------------


def login(error: str = "", fondo: str = "") -> str:
    """Sin navegacion ni nombre de usuario: aqui todavia no hay sesion."""
    aviso = f'<p class="error">{esc(error)}</p>' if error else ""

    # Con imagen de fondo la tarjeta se va a un lado y no al centro. La imagen
    # que se pone aqui suele tener su propio motivo (un logo, un equipo), y una
    # tarjeta en el centro lo tapa justo. A un lado conviven las dos cosas.
    # `fondo` no es un si/no: es una marca que cambia cuando cambia el
    # archivo. Va en la URL para que el navegador no siga sirviendo la imagen
    # vieja de su cache: la ruta es la misma, asi que sin esto una imagen
    # nueva no se ve hasta que caduque, y eso son 24 horas mirando la anterior.
    estilo_fondo = ""
    if fondo:
        estilo_fondo = """
  body {
    background-image:
      /* Un velo oscuro DEBAJO de la tarjeta y encima de la foto. Sin el, el
         formulario queda sobre lo que toque de la imagen y la legibilidad
         depende de la suerte. Con degradado y no plano para que la foto se
         siga viendo por el lado contrario. */
      linear-gradient(100deg,
        rgba(8,11,16,.88) 0%, rgba(8,11,16,.76) 32%,
        rgba(8,11,16,.40) 58%, rgba(8,11,16,.22) 100%),
      url("/fondo?v=__V__");
    background-size: cover, cover;
    /* La imagen es clara de punta a punta (medida: luminosidad media 147-167
       en las cinco franjas), asi que el velo hace falta en todas ellas; se
       deja mas suave a la derecha, que es donde esta el motivo de marca y
       donde la tarjeta no tapa nada. */
    background-position: center, center;
    background-repeat: no-repeat, no-repeat;
    justify-items: start;
    padding: 2rem clamp(1.5rem, 7vw, 6rem);
  }
  /* La tarjeta se queda opaca a proposito: nada de cristal esmerilado sobre
     una foto con tanto detalle, que es donde el texto deja de leerse. */
  form { box-shadow: 0 18px 50px rgba(0,0,0,.45); }
  h1, form p.sub { color: var(--texto); }

  @media (max-width: 760px) {
    /* En vertical no hay sitio para poner nada al lado: la tarjeta vuelve al
       centro y el velo se hace parejo para que se lea igual de bien. */
    body {
      justify-items: center;
      background-image:
        linear-gradient(rgba(8,11,16,.80), rgba(8,11,16,.80)), url("/fondo?v=__V__");
      /* En vertical, `cover` recorta casi la mitad del ancho. Centrada se
         perderia el logo de la derecha, asi que se desplaza el encuadre hacia
         alli: se sacrifica el monitor de la izquierda, que es lo que menos
         dice. */
      background-position: center, 62% center;
      padding: 1.5rem;
    }
  }
"""
        estilo_fondo = estilo_fondo.replace("__V__", esc(fondo))
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mkbackup - entrar</title>
<style>{ESTILO}
  /* El relleno de body es asimetrico en el resto del panel (deja aire abajo
     para que la ultima tarjeta no quede pegada al borde). Aqui hay que
     anularlo: centrar dentro de una caja con mas relleno abajo que arriba
     deja la tarjeta por encima del centro real, y se nota.
     dvh y no vh: en el movil, vh cuenta la barra del navegador aunque este
     visible, asi que la tarjeta queda descentrada justo en la pantalla mas
     pequena. Se deja vh antes como reserva para navegadores viejos. */
  body {{ display: grid; place-items: center; padding: 1.5rem;
         min-height: 100vh; min-height: 100dvh; }}
  form {{ background: var(--panel); border: 1px solid var(--borde);
         border-radius: var(--radio); box-shadow: var(--sombra);
         padding: 1.75rem; width: 100%; max-width: 340px; }}
  button {{ width: 100%; }}
{estilo_fondo}
</style>
</head>
<body>
<form method="post" action="/entrar">
  <h1>{MARCA}<span>mkbackup</span></h1>
  <p class="sub" style="margin:.3rem 0 1.4rem">Panel de respaldos de RouterOS</p>
  {aviso}
  <div class="campo">
    <label for="usuario">Usuario</label>
    <input id="usuario" name="usuario" type="text" autocomplete="username" autofocus required>
  </div>
  <div class="campo">
    <label for="clave">Contrase&ntilde;a</label>
    <input id="clave" name="clave" type="password" autocomplete="current-password" required>
  </div>
  <button type="submit">Entrar</button>
</form>
</body>
</html>
"""


# --- Estado (la unica pagina que se refresca sola) ---------------------------

_JS_PANEL = """
const REFRESCO = __REFRESCO__ * 1000;

// --- Graficas ---------------------------------------------------------------
// Los colores se leen del CSS y no se repiten aqui: asi el tema claro/oscuro
// se decide en un solo sitio y el JS no puede quedarse desincronizado.
const PAL = {};
const CLAVES = ["v1","v2","v3","v4","v5","v-bien","v-cambio","v-fallo","v-nada"];

function leerPaleta() {
  const s = getComputedStyle(document.documentElement);
  CLAVES.forEach(k => PAL[k] = s.getPropertyValue("--" + k).trim());
}

// Luminancia relativa (WCAG), para decidir si el porcentaje dentro de un
// sector se escribe en negro o en blanco. Sin esto, un 62% blanco sobre el
// amarillo del tema claro es ilegible.
function luz(hex) {
  const c = hex.replace("#", "");
  if (c.length < 6) return 1;
  const v = [0, 2, 4].map(i => {
    const x = parseInt(c.substr(i, 2), 16) / 255;
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
}

function punto(r, frac) {
  const a = frac * Math.PI * 2 - Math.PI / 2;   // 0 = las 12 en punto
  return [(85 + r * Math.cos(a)).toFixed(2), (85 + r * Math.sin(a)).toFixed(2)];
}

const RADIO = 76, HUECO = 47;

function tarta(idSvg, idLeyenda, series, etiquetaCentro) {
  const svg = document.getElementById(idSvg);
  const leyenda = document.getElementById(idLeyenda);
  const total = series.reduce((s, x) => s + x.valor, 0);

  if (!total) {
    svg.innerHTML = `<circle cx="85" cy="85" r="${(RADIO + HUECO) / 2}"
      fill="none" stroke="var(--barra)" stroke-width="${RADIO - HUECO}"/>`;
    leyenda.innerHTML = '<li class="sin-datos">Sin datos todavia.</li>';
    return;
  }

  const vivas = series.filter(s => s.valor > 0);
  let trozos = "", filas = "", acumulado = 0;

  for (const s of vivas) {
    const frac = s.valor / total;
    const pct = Math.round(frac * 100);
    const f0 = acumulado, f1 = acumulado + frac;
    acumulado = f1;

    if (vivas.length === 1) {
      // Un solo sector: un arco de 360 grados no se puede dibujar (inicio y
      // fin caen en el mismo punto y el trazo sale vacio). Se pinta el anillo
      // como un circulo con borde grueso.
      trozos += `<circle cx="85" cy="85" r="${(RADIO + HUECO) / 2}" fill="none"
        stroke="${s.color}" stroke-width="${RADIO - HUECO}"><title>${escapar(s.etq)}:
        ${s.valor} (100%)</title></circle>`;
    } else {
      const grande = frac > 0.5 ? 1 : 0;
      const [xa, ya] = punto(RADIO, f0), [xb, yb] = punto(RADIO, f1);
      const [xc, yc] = punto(HUECO, f1), [xd, yd] = punto(HUECO, f0);
      trozos += `<path d="M ${xa} ${ya} A ${RADIO} ${RADIO} 0 ${grande} 1 ${xb} ${yb}
        L ${xc} ${yc} A ${HUECO} ${HUECO} 0 ${grande} 0 ${xd} ${yd} Z"
        fill="${s.color}"><title>${escapar(s.etq)}: ${s.valor} (${pct}%)</title></path>`;
    }

    // El porcentaje solo se escribe dentro si el sector da de si; en uno
    // estrecho el texto se saldria o se recortaria, y para eso esta la
    // leyenda, que lleva el numero exacto de todas formas.
    if (frac >= 0.09) {
      const [xt, yt] = punto((RADIO + HUECO) / 2, f0 + frac / 2);
      const tinta = luz(s.color) > 0.45 ? "#0b0b0b" : "#ffffff";
      trozos += `<text x="${xt}" y="${yt}" fill="${tinta}" font-size="12"
        font-weight="600" text-anchor="middle" dominant-baseline="central"
        >${pct}%</text>`;
    }

    filas += `<li><span class="marca" style="background:${s.color}"></span>
      <span class="nom" title="${escapar(s.etq)}">${escapar(s.etq)}</span>
      <span class="val">${s.valor} &middot; ${pct}%</span></li>`;
  }

  svg.innerHTML = trozos + `
    <text x="85" y="80" class="centro-cifra" text-anchor="middle">${total}</text>
    <text x="85" y="97" class="centro-texto" text-anchor="middle"
      >${escapar(etiquetaCentro)}</text>`;
  leyenda.innerHTML = filas;
}

function reloj(segundos) {
  if (segundos === null || segundos === undefined) return "-";
  const s = Math.round(segundos);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}

// Formato fijo dia/mes/ano y 24 horas, anclado a la zona configurada. NO se
// usa toLocaleString a secas por dos motivos: sigue el idioma del navegador
// (en uno en ingles el 26 de julio sale como "7/26/2026" y quien lo lee no
// sabe si es el mes o el dia), y sigue la zona de quien mira, que no tiene
// por que ser la del servidor. Las paginas del servidor formatean igual.
const ZONA = "__ZONA__";

function formateador(opciones) {
  try {
    return new Intl.DateTimeFormat("es-EC", Object.assign({ timeZone: ZONA }, opciones));
  } catch (e) {
    // Zona desconocida para el navegador: se cae a la suya, que es peor que
    // acertar pero mejor que no pintar ninguna fecha.
    return new Intl.DateTimeFormat("es-EC", opciones);
  }
}

const F_FECHA = formateador({ day: "2-digit", month: "2-digit", year: "numeric",
                              hour: "2-digit", minute: "2-digit", hour12: false });
const F_HORA = formateador({ hour: "2-digit", minute: "2-digit",
                             second: "2-digit", hour12: false });

function fecha(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return isNaN(d) ? "-" : F_FECHA.format(d).replace(",", "");
}

function escapar(t) {
  return String(t === null || t === undefined ? "" : t).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function pintar(d) {
  const total = d.total || 0, hechos = d.hechos || 0;

  // El cartelon de "TERMINO CON FALLOS" se quito: repetia lo que ya dicen las
  // cifras y la tarta de abajo. Se conserva un aviso SOLO para los dos casos
  // que no se ven en ningun otro sitio: que haya un ciclo corriendo ahora
  // mismo (con su avance) y que uno se haya cortado a medias. Ese segundo es
  // la razon de ser del latido: sin el, una ejecucion muerta se queda
  // "en curso" para siempre y el panel miente justo cuando hay que mirarlo.
  const aviso = document.getElementById("aviso-ciclo");
  if (d.situacion === "corriendo") {
    aviso.className = "tarjeta ciclo corriendo";
    aviso.hidden = false;
    aviso.innerHTML =
      '<span class="punto"></span><b>Respaldo en curso</b> ' +
      '<span class="detalle">' + hechos + " de " + total + " equipos, lleva " +
      reloj(d.transcurrido) + "</span>" +
      '<div class="barra"><div style="width:' +
      (total ? Math.round((hechos / total) * 100) : 0) + '%"></div></div>';
  } else if (d.situacion === "interrumpida") {
    aviso.className = "tarjeta ciclo interrumpida";
    aviso.hidden = false;
    aviso.innerHTML =
      '<span class="punto"></span><b>Ejecucion interrumpida</b> ' +
      '<span class="detalle">El proceso dejo de responder tras ' + hechos +
      " de " + total + " equipos (sin senal hace " + reloj(d.latido_hace) +
      "). Empezo " + escapar(fecha(d.inicio)) + "</span>";
  } else {
    aviso.hidden = true;
  }

  // El proximo ciclo lo publica el programador; si no corre, no hay dato y se
  // dice, en vez de dejar un hueco que parece un cero.
  const prog = d.programador || {};
  let proximo = "programador detenido";
  if (prog.proxima) {
    const faltan = (new Date(prog.proxima) - new Date()) / 1000;
    proximo = faltan > 0 ? "en " + reloj(faltan) : "ahora mismo";
  }

  // Los totales se cuentan sobre los equipos y no sobre los contadores del
  // ciclo: los contadores solo saben del ciclo en curso, y con intervalos por
  // equipo un ciclo puede tocar solo a una parte de la flota.
  const equipos = d.equipos || [];
  const cuantos = e => equipos.filter(x => x.estado === e).length;
  const bien = cuantos("sin_cambios"), conCambio = cuantos("cambio");
  const fallidos = cuantos("fallo");
  const sinRespaldar = equipos.length - bien - conCambio - fallidos;
  const clientes = [...new Set(equipos.map(e => e.empresa))];

  // El color acompana al numero, no lo sustituye: la etiqueta sigue diciendo
  // "Fallidos". Un cero no se pinta de rojo, que seria alarmar por costumbre.
  document.getElementById("cifras").innerHTML = [
    ["Clientes", clientes.length, ""],
    ["Equipos", equipos.length, ""],
    ["Respaldados", bien + conCambio, (bien + conCambio) && !fallidos ? "bien" : ""],
    ["Fallidos", fallidos, fallidos ? "malo" : ""],
    ["Proximo ciclo", proximo, ""],
  ].map(([t, v, clase]) => `<div class="cifra ${clase}"><b>${escapar(v)}</b>` +
        `<span>${t}</span></div>`).join("");

  tarta("t-estado", "l-estado", [
    { etq: "Sin cambios", valor: bien, color: PAL["v-bien"] },
    { etq: "Con cambios", valor: conCambio, color: PAL["v-cambio"] },
    { etq: "Fallidos", valor: fallidos, color: PAL["v-fallo"] },
    { etq: "Sin respaldar", valor: sinRespaldar, color: PAL["v-nada"] },
  ], "equipos");

  const porCliente = {};
  equipos.forEach(e => { porCliente[e.empresa] = (porCliente[e.empresa] || 0) + 1; });
  // Se ensenan los cuatro mayores y el resto se pliega en "Otros": pasado ese
  // punto los colores dejan de distinguirse bajo daltonismo, y la tabla de
  // abajo tiene el detalle completo de todas formas.
  const mayores = clientes.slice()
    .sort((a, b) => porCliente[b] - porCliente[a] || a.localeCompare(b))
    .slice(0, 4)
    // El color se asigna por orden alfabetico, NO por tamano: si fuera por
    // ranking, un cliente cambiaria de color en cuanto otro le adelantara, y
    // quien ya aprendio de que color es el suyo dejaria de reconocerlo.
    .sort((a, b) => a.localeCompare(b));

  const serieClientes = mayores.map((n, i) => (
    { etq: n, valor: porCliente[n], color: PAL["v" + (i + 1)] }
  ));
  const resto = clientes.filter(n => mayores.indexOf(n) < 0);
  if (resto.length) {
    serieClientes.push({
      etq: `Otros (${resto.length} clientes)`,
      valor: resto.reduce((s, n) => s + porCliente[n], 0),
      color: PAL.v5,
    });
  }
  tarta("t-clientes", "l-clientes", serieClientes, "equipos");

  const orden = { fallo: 0, en_curso: 1, cambio: 2, pendiente: 3, sin_cambios: 4 };
  // Los mismos equipos que ya se contaron arriba, ordenados para la tabla:
  // primero lo que hay que mirar (fallos), al final lo que no pide atencion.
  const ordenados = equipos.slice().sort(
    (a, b) => (orden[a.estado] ?? 9) - (orden[b.estado] ?? 9) ||
              a.nombre.localeCompare(b.nombre));

  document.getElementById("equipos").innerHTML = ordenados.length
    ? ordenados.map(e => `<tr>
        <td>${escapar(e.nombre)}</td>
        <td>${escapar(e.empresa)}</td>
        <td><code>${escapar(e.ip)}</code></td>
        <td>${escapar(e.grupo)}</td>
        <td><span class="etiqueta e-${escapar(e.estado)}">${escapar(e.estado.replace("_", " "))}</span></td>
        <td><code>${escapar(e.detalle)}</code></td>
      </tr>`).join("")
    : `<tr><td colspan="6" class="vacio">Sin datos todavia.</td></tr>`;

  const avisos = d.avisos_inventario || [];
  document.getElementById("caja-avisos").hidden = avisos.length === 0;
  document.getElementById("avisos").innerHTML =
    avisos.map(a => `<div class="aviso">${escapar(a)}</div>`).join("");

  const historial = d.historial || [];
  document.getElementById("caja-historial").hidden = historial.length === 0;
  document.getElementById("historial").innerHTML = historial.map(h => `<tr>
      <td>${escapar(fecha(h.inicio))}</td>
      <td>${escapar((h.ok || 0) + "/" + (h.total || 0))}</td>
      <td>${escapar(h.cambios || 0)}</td>
      <td>${escapar(h.fallidos || 0)}</td>
      <td>${escapar(h.terminada ? reloj(h.duracion) : "interrumpida")}</td>
    </tr>`).join("");
}

async function refrescar() {
  const fuente = document.getElementById("fuente");
  try {
    const resp = await fetch("/api/estado", { cache: "no-store" });
    // La sesion caduco con la pestana abierta: al login, en vez de dejar
    // cifras congeladas que parecen actuales.
    if (resp.status === 401) { location.href = "/entrar"; return; }
    if (!resp.ok) throw new Error(resp.status);
    pintar(await resp.json());
    fuente.className = "sub";
    fuente.textContent = "actualizado " + F_HORA.format(new Date());
  } catch (err) {
    // Que se vea que el panel esta ciego, en vez de dejar cifras viejas que
    // parecen actuales.
    fuente.className = "sub caido";
    fuente.textContent = "sin conexion con el servidor";
  }
}

leerPaleta();
refrescar();
setInterval(refrescar, REFRESCO);

// Si el sistema cambia de tema claro a oscuro con la pestana abierta, los
// colores del CSS cambian pero los que ya se escribieron en el SVG no: hay
// que releerlos y repintar.
if (window.matchMedia) {
  const oscuro = window.matchMedia("(prefers-color-scheme: dark)");
  const alCambiar = () => { leerPaleta(); refrescar(); };
  if (oscuro.addEventListener) oscuro.addEventListener("change", alCambiar);
}
"""


def panel(sesion, refresco: int, zona: str = "America/Guayaquil") -> str:
    cuerpo = """
  <div id="aviso-ciclo" hidden></div>

  <div class="cifras" id="cifras"></div>

  <div class="tartas">
    <div class="tarjeta">
      <h2>Resultado del ultimo respaldo</h2>
      <div class="grafica">
        <svg id="t-estado" viewBox="0 0 170 170" role="img"
             aria-label="Resultado del ultimo respaldo por equipo"></svg>
        <ul class="leyenda" id="l-estado"></ul>
      </div>
    </div>
    <div class="tarjeta">
      <h2>Equipos por cliente</h2>
      <div class="grafica">
        <svg id="t-clientes" viewBox="0 0 170 170" role="img"
             aria-label="Reparto de equipos por cliente"></svg>
        <ul class="leyenda" id="l-clientes"></ul>
      </div>
    </div>
  </div>

  <div class="tarjeta">
    <h2>Equipos</h2>
    <div class="tabla-caja">
      <table>
        <thead><tr><th>Equipo</th><th>Empresa</th><th>IP</th><th>Grupo</th>
                   <th>Estado</th><th>Detalle</th></tr></thead>
        <tbody id="equipos"></tbody>
      </table>
    </div>
  </div>

  <div class="tarjeta" id="caja-avisos" hidden>
    <h2>Avisos del inventario</h2>
    <div id="avisos"></div>
  </div>

  <div class="tarjeta" id="caja-historial" hidden>
    <h2>Ejecuciones anteriores</h2>
    <div class="tabla-caja">
      <table>
        <thead><tr><th>Inicio</th><th>Equipos</th><th>Cambios</th><th>Fallidos</th>
                   <th>Duracion</th></tr></thead>
        <tbody id="historial"></tbody>
      </table>
    </div>
  </div>
"""
    return envoltura(
        "mkbackup",
        cuerpo,
        sesion,
        activo="estado",
        cabeza='<span class="sub" id="fuente"></span>',
        pie="Los respaldos los dispara el programador. Este panel no los lanza.",
        guion=(
            _JS_PANEL.replace("__REFRESCO__", str(int(refresco)))
            # La zona va como texto dentro de una cadena JS: se limita a lo que
            # puede tener un nombre IANA para que no haya forma de cerrar la
            # comilla y colar codigo desde la configuracion.
            .replace(
                "__ZONA__", "".join(c for c in zona if c.isalnum() or c in "/_+-")
            )
        ),
    )


# --- Equipos ----------------------------------------------------------------


def equipos(
    lista,
    avisos,
    editable: bool,
    mensaje: str = "",
    sesion=None,
    empresas=(),
    grupos=(),
    filtro=None,
    total_sin_filtrar: int = 0,
    orden: str = "",
    descendente: bool = False,
    columnas=(),
    hechos=None,
    zona=None,
) -> str:
    """Inventario. `lista` son objetos Equipo del modulo inventory.

    `hechos` es lo ULTIMO QUE SE SUPO de cada equipo (modelo, version de
    RouterOS, cuando se respaldo bien): no sale del inventario, que es lo que
    alguien configuro, sino de lo que contesto el propio router la ultima vez.
    Por eso puede faltar: un equipo recien dado de alta no tiene nada todavia.
    """
    filtro = filtro or {}
    hechos = hechos or {}
    visibles = columnas or list(COLUMNAS_EQUIPOS_DEFECTO)

    def cabecera(clave, titulo, ordenable):
        if not ordenable:
            return f"<th>{esc(titulo)}</th>"
        vuelta = "asc" if (clave == orden and descendente) else "desc"
        partes = [("orden", clave), ("dir", vuelta)]
        for k in ("empresa", "grupo", "q"):
            if filtro.get(k):
                partes.append((k, filtro[k]))
        for c in visibles:
            partes.append(("col", c))
        flecha = ""
        if clave == orden:
            flecha = (' <span class="flecha">&#9660;</span>' if descendente
                      else ' <span class="flecha">&#9650;</span>')
        activa = ' class="ordenada"' if clave == orden else ""
        return (f'<th{activa}><a href="/equipos?{urlencode(partes)}">'
                f"{esc(titulo)}{flecha}</a></th>")

    cabeceras = "".join(
        cabecera(c, titulo, ordenable)
        for c, titulo, ordenable in COLUMNAS_EQUIPOS if c in visibles
    )

    filas = []
    for e in lista:
        # Regla de todo el proyecto: si el nombre es la IP, es que el equipo no
        # supo decir quien era y esta pendiente de identificar.
        provisional = e.nombre == e.ip
        nombre = esc(e.nombre)
        if provisional:
            nombre += ' <span class="etiqueta e-provisional">sin identificar</span>'

        sabido = hechos.get(e.nombre, {})
        # Un intervalo 0 no se pinta como "0": significa que hereda el general,
        # y un cero ahi se leeria como "cada cero minutos".
        cadencia = (f"{esc(e.intervalo_minutos)} min"
                    if getattr(e, "intervalo_minutos", 0)
                    else '<span class="vacio">general</span>')
        # Nunca se manda la clave al navegador, ni enmascarada: lo que no sale
        # del servidor no se puede leer en un "ver codigo fuente".
        credencial = (f"<code>{esc(e.usuario)}</code>" if getattr(e, "usuario", "")
                      else '<span class="vacio">general</span>')

        ultimo_ok = sabido.get("ultimo_ok")
        if ultimo_ok:
            ultimo = esc(fecha(ultimo_ok, zona))
            # Si el ultimo intento fallo, la fecha buena de arriba se queda
            # vieja sin avisar. Se dice, porque "respaldado el martes" con el
            # equipo caido desde el miercoles es justo lo que engana.
            if sabido.get("resultado") == "fallo":
                ultimo += ' <span class="etiqueta e-fallo">fallando</span>'
        elif sabido.get("ultimo_intento"):
            ultimo = '<span class="etiqueta e-fallo">nunca, ultimo intento fallo</span>'
        else:
            ultimo = '<span class="vacio">todavia no</span>'

        celdas = {
            "nombre": nombre,
            "empresa": esc(e.empresa),
            "ip": f"<code>{esc(e.ip)}</code>",
            "puerto": f"<code>{esc(e.puerto)}</code>",
            "grupo": esc(e.grupo),
            "modelo": esc(sabido.get("modelo")) or '<span class="vacio">-</span>',
            "version": esc(sabido.get("version")) or '<span class="vacio">-</span>',
            "ultimo": ultimo,
            "cada": cadencia,
            "usuario": credencial,
        }

        acciones = ""
        if editable:
            acciones = (
                # El nombre NO se mete dentro del confirm. Dentro de un
                # atributo, el navegador decodifica las entidades ANTES de que
                # el JavaScript lea la cadena, asi que un nombre con un
                # apostrofe escapado volveria a serlo y cerraria la cadena.
                f'<a class="boton secundario mini" href="/equipos/editar?nombre={esc(e.nombre)}">Editar</a> '
                f'<form method="post" action="/equipos/baja" style="display:inline"'
                f' onsubmit="return confirm(&#39;Dar de baja este equipo? '
                f'Los respaldos ya guardados NO se borran.&#39;)">'
                f'<input type="hidden" name="nombre" value="{esc(e.nombre)}">'
                f'<button class="peligro mini" type="submit">Baja</button></form>'
            )
        filas.append(
            "<tr>" + "".join(f"<td>{celdas[c]}</td>" for c in visibles if c in celdas)
            + f'<td><a href="/historial?equipo={esc(e.nombre)}">ver cambios</a></td>'
            + f'<td class="acciones">{acciones}</td></tr>'
        )

    if not filas:
        hay_filtro = any(filtro.get(k) for k in ("empresa", "grupo", "q"))
        filas.append(
            f'<tr><td colspan="{len(visibles) + 2}" class="vacio">'
            + ("Ningun equipo coincide con el filtro."
               if hay_filtro
               else "El inventario esta vacio. Anade un equipo o importa una plantilla.")
            + "</td></tr>"
        )

    if editable:
        botones = ('<a class="boton" href="/equipos/nuevo">Anadir equipo</a>'
                   '<a class="boton secundario" href="/importar">Importar desde Excel</a>')
    else:
        botones = ('<span class="sub">El panel esta en modo solo lectura '
                   "(web.editar_inventario: false).</span>")

    bloque_avisos = ""
    if avisos:
        detalle = "".join(f'<div class="aviso">{esc(a)}</div>' for a in avisos)
        bloque_avisos = f'<div class="tarjeta"><h2>Avisos del inventario</h2>{detalle}</div>'

    cuenta = f"{len(lista)} equipos"
    if total_sin_filtrar and len(lista) != total_sin_filtrar:
        cuenta = f"{len(lista)} de {total_sin_filtrar} equipos"

    cuerpo = f"""
  {f'<div class="bien">{esc(mensaje)}</div>' if mensaje else ""}
  {filtros("/equipos", empresas, grupos, filtro)}
  <div class="barra-acciones">{botones}
    <span class="sub crece">{cuenta}</span>
    {selector_columnas("/equipos", COLUMNAS_EQUIPOS, visibles, filtro, orden, descendente)}
  </div>
  <div class="tarjeta">
    <div class="tabla-caja">
      <table>
        <thead><tr>{cabeceras}<th>Historial</th><th></th></tr></thead>
        <tbody>{"".join(filas)}</tbody>
      </table>
    </div>
  </div>
  {bloque_avisos}
"""
    return envoltura("mkbackup - equipos", cuerpo, sesion, activo="equipos")


def formulario_equipo(datos: dict, errores, alta: bool, original: str = "",
                      sesion=None) -> str:
    lista_errores = "".join(f'<div class="error">{esc(e)}</div>' for e in errores)
    titulo = "Anadir equipo" if alta else f"Editar {esc(original)}"
    accion = "/equipos/nuevo" if alta else "/equipos/editar"
    oculto = (
        f'<input type="hidden" name="original" value="{esc(original)}">'
        if not alta else ""
    )
    pista_nombre = (
        "Es el /system identity del propio router: con el boton de al lado se "
        "le pregunta ahora, con la IP, el puerto y las credenciales que haya "
        "en este formulario. Si lo dejas vacio se le pregunta al guardar, y si "
        "no responde se guarda con la IP y se renombra solo en el primer "
        "respaldo que salga bien."
    )
    # La clave nunca se devuelve al navegador, ni siquiera para rellenar el
    # campo al editar: se manda vacia y vacia significa "deja la que ya tiene".
    # Asi la contrasena no viaja de vuelta en cada carga de la pagina.
    pista_clave = (
        "Se guarda tal cual, sin recortar espacios."
        if alta
        else "Dejala vacia para conservar la que ya tiene."
    )
    cuerpo = f"""
  <div class="tarjeta estrecho">
    <h2>{titulo}</h2>
    {lista_errores}
    <form method="post" action="{accion}">
      {oculto}
      <div class="campo">
        <label for="nombre">Nombre del router</label>
        <div class="con-boton">
          <input id="nombre" type="text" name="nombre" value="{esc(datos.get('nombre'))}"
                 autocomplete="off" autofocus>
          <!-- Boton de envio con name/value propios: el mismo formulario, el
               mismo destino, y el servidor distingue por "accion". Asi no hace
               falta JavaScript ni otra ruta, y lo que ya se tecleo en el resto
               de campos viaja y vuelve intacto. -->
          <button type="submit" name="accion" value="identidad" class="secundario"
                  title="Abre una sesion SSH al equipo y lee su /system identity">
            Preguntar al router
          </button>
        </div>
        <div class="pista">{pista_nombre}</div>
      </div>
      <div class="campo">
        <label for="empresa">Empresa</label>
        <input id="empresa" type="text" name="empresa" value="{esc(datos.get('empresa'))}"
               autocomplete="off" required>
        <div class="pista">Cada empresa tiene su carpeta en el repositorio.</div>
      </div>
      <div class="rejilla">
        <div class="campo">
          <label for="ip">IP o nombre DNS</label>
          <input id="ip" type="text" name="ip" value="{esc(datos.get('ip'))}"
                 autocomplete="off" required>
        </div>
        <div class="campo">
          <label for="puerto">Puerto SSH</label>
          <input id="puerto" type="text" name="puerto" value="{esc(datos.get('puerto') or '22')}"
                 autocomplete="off">
          <div class="pista">Por donde se pide el acceso. 22 salvo que lo hayas movido.</div>
        </div>
      </div>
      <div class="rejilla">
        <div class="campo">
          <label for="grupo">Grupo</label>
          <input id="grupo" type="text" name="grupo" value="{esc(datos.get('grupo'))}"
                 autocomplete="off">
          <div class="pista">Para clasificar y filtrar: core, bts, borde...
               No afecta a las carpetas del repositorio. Vacio, mikrotiks.</div>
        </div>
        <div class="campo">
          <label for="intervalo">Cada cuantos minutos consultarlo</label>
          <input id="intervalo" type="text" name="intervalo"
                 value="{esc(datos.get('intervalo') or '')}" autocomplete="off">
          <div class="pista">Vacio = el intervalo general de Ajustes. Un router
               que casi no cambia no necesita el mismo ritmo que un core: subelo
               (1440 = una vez al dia) y le quitas trabajo a el y a la red.</div>
        </div>
      </div>

      <h2 class="aparte">Acceso al router</h2>
      <p class="pista bajo-titulo">
        Vacios = se usan el usuario y la clave generales del sistema. Rellenalos
        solo si este equipo tiene los suyos.</p>
      <div class="rejilla">
        <div class="campo">
          <label for="usuario">Usuario SSH</label>
          <input id="usuario" type="text" name="usuario"
                 value="{esc(datos.get('usuario'))}" autocomplete="off">
        </div>
        <div class="campo">
          <label for="clave">Clave SSH</label>
          <input id="clave" type="password" name="clave" value=""
                 autocomplete="new-password">
          <div class="pista">{esc(pista_clave)}</div>
        </div>
      </div>

      <div class="barra-acciones">
        <button type="submit">Guardar</button>
        <a class="boton secundario" href="/equipos">Cancelar</a>
      </div>
    </form>
  </div>
"""
    return envoltura("mkbackup - " + titulo, cuerpo, sesion, activo="equipos")


# --- Importacion ------------------------------------------------------------


def importar(hay_xlsx: bool, resultado: dict | None = None, error: str = "",
             sesion=None) -> str:
    aviso_xlsx = ""
    if not hay_xlsx:
        aviso_xlsx = (
            '<div class="aviso">Falta la libreria openpyxl, asi que por ahora '
            "solo se puede usar la plantilla CSV (Excel la abre igual). "
            "Para habilitar .xlsx: <code>pip install openpyxl</code></div>"
        )

    boton_xlsx = (
        '<a class="boton secundario" href="/importar/plantilla?formato=xlsx">Plantilla .xlsx</a>'
        if hay_xlsx else ""
    )

    bloque_resultado = ""
    if resultado is not None:
        rechazos = "".join(
            f'<tr><td>{esc(r["fila"])}</td><td>{esc(r["equipo"])}</td>'
            f'<td>{esc(r["motivo"])}</td></tr>'
            for r in resultado.get("rechazos", [])
        )
        tabla_rechazos = ""
        if rechazos:
            tabla_rechazos = f"""
      <h2 class="aparte">Filas que no se pudieron dar de alta</h2>
      <div class="tabla-caja"><table>
        <thead><tr><th>Fila</th><th>Equipo</th><th>Motivo</th></tr></thead>
        <tbody>{rechazos}</tbody>
      </table></div>"""

        avisos = "".join(
            f'<div class="aviso">{esc(a)}</div>' for a in resultado.get("avisos", [])
        )
        clase = "bien" if resultado.get("altas") else "aviso"
        bloque_resultado = f"""
  <div class="tarjeta">
    <div class="{clase}">Se dieron de alta {esc(resultado.get("altas", 0))} equipos
      de {esc(resultado.get("leidas", 0))} filas leidas.</div>
    {avisos}
    {tabla_rechazos}
    <div class="barra-acciones separado">
      <a class="boton" href="/equipos">Ver el inventario</a>
    </div>
  </div>"""

    bloque_error = f'<div class="error">{esc(error)}</div>' if error else ""

    cuerpo = f"""
  {bloque_error}
  {bloque_resultado}
  <div class="tarjeta estrecho">
    <h2>1. Descarga la plantilla</h2>
    {aviso_xlsx}
    <p class="sub">Columnas: nombre, empresa, ip, puerto, grupo. Solo la IP es
       obligatoria: el nombre puede quedar vacio y se le pregunta al router.</p>
    <div class="barra-acciones">
      <a class="boton secundario" href="/importar/plantilla?formato=csv">Plantilla .csv</a>
      {boton_xlsx}
    </div>

    <h2 class="aparte">2. Subela llena</h2>
    <form method="post" action="/importar" enctype="multipart/form-data">
      <div class="campo">
        <input type="file" name="archivo" accept=".csv,.xlsx" required>
      </div>
      <button type="submit">Importar</button>
    </form>
    <p class="pista">Los equipos que ya existan con el mismo nombre se saltan y
       se listan al terminar: importar dos veces el mismo archivo no duplica
       nada ni pisa lo que ya hay.</p>
  </div>
"""
    return envoltura("mkbackup - importar", cuerpo, sesion, activo="equipos")


# --- Cambios e historial ----------------------------------------------------


def cambios(
    lista,
    hay_diferencias: bool,
    zona=None,
    por_ruta=None,
    empresas=(),
    grupos=(),
    filtro=None,
    sesion=None,
    columnas=(),
) -> str:
    """`lista` son pares (ruta_relativa, Version) mas recientes primero.

    `por_ruta` mapea ruta del repo -> Equipo del inventario. Sirve para dos
    cosas: ensenar el nombre legible de la empresa en vez del de su carpeta
    ("Conecta Sur Cia. Ltda." y no "conecta-sur-cia.-ltda"), y saber a que
    grupo e IP pertenece cada cambio para poder filtrar. Un equipo dado de
    baja ya no esta en el inventario, pero sus cambios siguen en el repo: para
    esos se cae al nombre de la carpeta, que es lo unico que queda.
    """
    filtro = filtro or {}
    por_ruta = por_ruta or {}
    visibles = columnas or list(COLUMNAS_CAMBIOS_DEFECTO)

    cabeceras = "".join(
        f"<th>{esc(titulo)}</th>"
        for c, titulo, _ in COLUMNAS_CAMBIOS if c in visibles
    )

    filas = []
    for ruta, v in lista:
        equipo_inv = por_ruta.get(ruta)
        nombre = ruta.split("/")[-1].removesuffix(".rsc")
        if equipo_inv is not None:
            empresa, grupo = equipo_inv.empresa, equipo_inv.grupo
        else:
            empresa = ruta.split("/")[0] if "/" in ruta else ""
            grupo = ""
        enlace = esc(nombre)
        if hay_diferencias:
            enlace = (f'<a href="/diferencia?ruta={esc(ruta)}&commit={esc(v.commit)}">'
                      f"{esc(nombre)}</a>")
        celdas = {
            "fecha": esc(fecha(v.fecha, zona)),
            "equipo": enlace,
            "empresa": esc(empresa),
            "grupo": esc(grupo),
            "version": f"<code>{esc(v.commit)}</code>",
            "lineas": (f'<span style="color:var(--ok)">+{esc(v.lineas_mas)}</span> '
                       f'<span style="color:var(--fallo)">-{esc(v.lineas_menos)}</span>'),
            "mensaje": esc(v.mensaje),
        }
        filas.append(
            "<tr>" + "".join(f"<td>{celdas[c]}</td>" for c in visibles if c in celdas)
            + "</tr>"
        )

    if not filas:
        hay_filtro = any(filtro.get(k) for k in ("empresa", "grupo", "q"))
        filas.append(
            f'<tr><td colspan="{len(visibles)}" class="vacio">'
            + ("Ningun cambio coincide con el filtro."
               if hay_filtro else "Todavia no hay ningun cambio guardado.")
            + "</td></tr>"
        )

    cuerpo = f"""
  {filtros("/cambios", empresas, grupos, filtro)}
  <div class="barra-acciones">
    <span class="crece"></span>
    {selector_columnas("/cambios", COLUMNAS_CAMBIOS, visibles, filtro)}
  </div>
  <div class="tarjeta">
    <h2>Ultimos cambios de la flota</h2>
    <div class="tabla-caja">
      <table>
        <thead><tr>{cabeceras}</tr></thead>
        <tbody>{"".join(filas)}</tbody>
      </table>
    </div>
  </div>
"""
    return envoltura("mkbackup - cambios", cuerpo, sesion, activo="cambios")


def historial(equipo: str, ruta: str, versiones, hay_diferencias: bool, zona=None,
              sesion=None) -> str:
    filas = []
    for v in versiones:
        celda = esc(v.commit)
        if hay_diferencias:
            celda = (
                f'<a href="/diferencia?ruta={esc(ruta)}&commit={esc(v.commit)}">'
                f"<code>{esc(v.commit)}</code></a>"
            )
        filas.append(
            f"<tr><td>{esc(fecha(v.fecha, zona))}</td><td>{celda}</td>"
            f'<td><span style="color:var(--ok)">+{esc(v.lineas_mas)}</span> '
            f'<span style="color:var(--fallo)">-{esc(v.lineas_menos)}</span></td>'
            f"<td>{esc(v.mensaje)}</td></tr>"
        )
    if not filas:
        filas.append(
            '<tr><td colspan="4" class="vacio">Este equipo todavia no tiene '
            "ningun respaldo guardado.</td></tr>"
        )
    cuerpo = f"""
  <div class="barra-acciones">
    <a class="boton secundario" href="/equipos">Volver al inventario</a>
    <span class="sub crece"><code>{esc(ruta)}</code></span>
  </div>
  <div class="tarjeta">
    <h2>Versiones de {esc(equipo)}</h2>
    <div class="tabla-caja">
      <table>
        <thead><tr><th>Fecha</th><th>Version</th><th>Lineas</th><th>Commit</th></tr></thead>
        <tbody>{"".join(filas)}</tbody>
      </table>
    </div>
  </div>
"""
    return envoltura(f"mkbackup - {equipo}", cuerpo, sesion, activo="cambios")


def diferencia(equipo: str, ruta: str, commit: str, lineas, oculto: bool,
               sesion=None) -> str:
    clases = {"mas": "d-mas", "menos": "d-menos", "trozo": "d-trozo", "contexto": ""}
    pintadas = "".join(
        f'<div class="{clases.get(l.tipo, "")}'
        f'{" d-oculta" if getattr(l, "oculta", False) else ""}">{esc(l.texto)}</div>'
        for l in lineas
    )
    if not pintadas:
        pintadas = '<div class="vacio" style="padding:.8rem">Sin diferencias.</div>'

    leyenda = ""
    if oculto:
        leyenda = (
            '<p class="leyenda-diff">Las lineas marcadas a la derecha llevan una '
            "credencial (password, PSK, secret...) cuyo valor esta oculto. Se ve "
            "que la linea cambio, no lo que dice. Si el unico cambio fue el "
            "propio secreto, las dos versiones se ven iguales."
            "</p>"
        )

    cuerpo = f"""
  <div class="barra-acciones">
    <a class="boton secundario" href="/historial?equipo={esc(equipo)}">Todas las versiones</a>
    <span class="sub crece"><code>{esc(ruta)}</code> &middot; <code>{esc(commit)}</code></span>
  </div>
  <div class="tarjeta">
    <h2>Que cambio en {esc(equipo)}</h2>
    <div class="diff">{pintadas}</div>
    {leyenda}
  </div>
"""
    return envoltura(f"mkbackup - {equipo} {commit}", cuerpo, sesion, activo="cambios")


# --- Ajustes ----------------------------------------------------------------


def ajustes(cfg, programador: dict, mensaje: str = "", error: str = "", zona=None,
            sesion=None, fondo: str = "") -> str:
    bloque_error = f'<div class="error">{esc(error)}</div>' if error else ""
    bloque_ok = f'<div class="bien">{esc(mensaje)}</div>' if mensaje else ""

    proxima = programador.get("proxima")
    ultima = programador.get("ultima")
    estado_prog = (
        f'<p class="sub">Ultimo ciclo: '
        f'{esc(fecha(ultima, zona) if ultima else "todavia ninguno")}. '
        f'Proximo: '
        f'{esc(fecha(proxima, zona) if proxima else "el programador no esta corriendo")}.'
        f"</p>"
    )

    marcado = "checked" if cfg.planificador.al_arrancar else ""

    # La vista previa usa la misma marca que el login, asi que en cuanto se
    # sube otra imagen se ve la nueva y no la que tuviera el navegador.
    if fondo:
        vista = (
            f'<div class="vista-fondo"><img src="/fondo?v={esc(fondo)}" alt=""></div>'
            '<form method="post" action="/ajustes/fondo/quitar" class="separado">'
            '<button class="peligro" type="submit">Quitar el fondo</button></form>'
        )
    else:
        vista = ('<p class="sub">Ahora mismo la pantalla de entrada no tiene '
                 "imagen de fondo.</p>")

    bloque_fondo = f"""
    <p class="sub">La imagen que se ve detras del formulario al entrar.</p>
    {vista}
    <form method="post" action="/ajustes/fondo" enctype="multipart/form-data"
          class="separado">
      <div class="campo">
        <label for="fondo">Subir una imagen</label>
        <input id="fondo" type="file" name="archivo"
               accept="image/jpeg,image/png,image/webp,image/avif" required>
        <div class="pista">JPG, PNG, WebP o AVIF. Se comprueba el contenido, no
             el nombre. Cuanto mas pese, mas tarda en aparecer el formulario:
             por debajo de 500 KB no se nota, y 1920 px de ancho sobran.</div>
      </div>
      <button type="submit">Guardar el fondo</button>
    </form>
    <p class="pista separado">Se sirve SIN pedir login, porque es el fondo de la
       propia pantalla de entrada: cualquiera que llegue al panel puede verla.</p>"""

    # Nunca se manda la clave al navegador, ni siquiera para rellenar el campo:
    # vacia significa "deja la que ya hay". Lo que si se dice es si hay alguna
    # puesta, porque un campo vacio sin mas no distingue "no la toques" de "no
    # hay ninguna", y esa diferencia es justo la que deja una flota sin acceso.
    pista_ssh = (
        "Vacia para no cambiarla." if cfg.ssh.password
        else "No hay ninguna clave puesta."
    )
    nota_clave = (
        "Si en vez de clave usas una privada, esta configurada en "
        f"{cfg.ssh.clave_privada} y se edita en el archivo."
        if cfg.ssh.clave_privada
        else "Un equipo con usuario o clave propios manda sobre esto, campo a campo."
    )

    cuerpo = f"""
  {bloque_error}{bloque_ok}
  <div class="pareja">
  <div class="tarjeta">
    <h2>Cada cuanto se buscan cambios</h2>
    {estado_prog}
    <form method="post" action="/ajustes">
      <div class="campo">
        <label for="intervalo">Intervalo en minutos</label>
        <input id="intervalo" type="number" name="intervalo_minutos" min="1"
               value="{esc(cfg.planificador.intervalo_minutos)}" required>
        <div class="pista">El programador lo recoge en el siguiente ciclo, sin
             reiniciar nada. Con muchos equipos, un intervalo mas corto que lo
             que tarda un ciclo completo solo encadena ciclos: el panel avisa
             si pasa.</div>
      </div>
      <div class="campo">
        <label><input type="checkbox" name="al_arrancar" value="1" {marcado}
               style="width:auto"> Respaldar nada mas arrancar el servicio</label>
        <div class="pista">Util si el servidor se reinicia a menudo: sin esto,
             podria no respaldar nunca.</div>
      </div>
      <button type="submit">Guardar</button>
    </form>
  </div>

  <div class="tarjeta">
    <h2>Acceso general a los routers</h2>
    <p class="sub">Con esto se entra a cualquier equipo que no traiga usuario y
       clave propios en su ficha. Es lo que hay que cambiar cuando se rota la
       clave de la flota.</p>
    <form method="post" action="/ajustes/ssh">
      <div class="rejilla">
        <div class="campo">
          <label for="ssh-usuario">Usuario SSH</label>
          <input id="ssh-usuario" type="text" name="ssh_usuario"
                 value="{esc(cfg.ssh.usuario)}" autocomplete="off">
        </div>
        <div class="campo">
          <label for="ssh-clave">Clave SSH</label>
          <input id="ssh-clave" type="password" name="ssh_password" value=""
                 autocomplete="new-password">
          <div class="pista">{esc(pista_ssh)}</div>
        </div>
      </div>
      <button type="submit">Guardar el acceso</button>
      <p class="pista separado">{esc(nota_clave)}</p>
    </form>
  </div>

  </div>

  <div class="tarjeta estrecho">
    <h2>Pantalla de entrada</h2>
    {bloque_fondo}
  </div>

  <div class="tarjeta apagado">
    <h2>Lo que solo se cambia por archivo</h2>
    <p class="sub">Esto vive en <code>{esc(cfg.ruta)}</code> y no se toca desde
       el panel: son credenciales y rutas, y el panel corre sin permiso de
       escritura sobre esa carpeta a proposito.</p>
    <div class="tabla-caja"><table>
      <tbody>
        <tr><td>Equipos en paralelo</td><td><code>{esc(cfg.concurrencia)}</code></td></tr>
        <tr><td>Reintentos por equipo</td><td><code>{esc(cfg.reintentos)}</code></td></tr>
        <tr><td>Repositorio</td><td><code>{esc(cfg.almacen.git)}</code></td></tr>
        <tr><td>Inventario</td><td><code>{esc(cfg.inventario)}</code></td></tr>
        <tr><td>Backup binario</td><td><code>{"si" if cfg.binario.activo else "no"}</code></td></tr>
        <tr><td>Secretos en el diff</td>
            <td><code>{"ocultos" if cfg.web.ocultar_secretos else "VISIBLES"}</code></td></tr>
      </tbody>
    </table></div>
  </div>
"""
    return envoltura("mkbackup - ajustes", cuerpo, sesion, activo="ajustes")


# --- Usuarios ---------------------------------------------------------------


# Las columnas de cada tabla: clave, titulo y si se puede ordenar por ella.
# El orden de esta tupla es el orden en que salen; lo que se puede elegir es
# cuales se ven, no como se barajan, porque una tabla que cambia de orden de
# columnas segun quien la mire deja de ser reconocible.
COLUMNAS_EQUIPOS = (
    ("nombre", "Equipo", True),
    ("empresa", "Empresa", True),
    ("ip", "IP", True),
    ("puerto", "Puerto", True),
    ("grupo", "Grupo", True),
    ("modelo", "Modelo", False),
    ("version", "RouterOS", False),
    ("ultimo", "Ultimo respaldo", False),
    ("cada", "Cada", True),
    ("usuario", "Usuario", False),
)
# Lo que se ve si nadie ha elegido nada. Puerto y usuario quedan fuera porque
# casi siempre son el valor por defecto: ocupan sitio para decir "lo de
# siempre". Se activan cuando hacen falta.
COLUMNAS_EQUIPOS_DEFECTO = (
    "nombre", "empresa", "ip", "grupo", "modelo", "version", "ultimo",
)

COLUMNAS_CAMBIOS = (
    ("fecha", "Fecha", False),
    ("equipo", "Equipo", False),
    ("empresa", "Empresa", False),
    ("grupo", "Grupo", False),
    ("version", "Version", False),
    ("lineas", "Lineas", False),
    ("mensaje", "Mensaje", False),
)
COLUMNAS_CAMBIOS_DEFECTO = ("fecha", "equipo", "empresa", "lineas", "mensaje")


def columnas_validas(pedidas, disponibles, defecto):
    """Las columnas a ensenar, o las de por defecto si lo pedido no vale.

    Se filtra contra las que existen porque esto llega de la URL: una columna
    inventada no puede acabar en un `getattr`. Y si no queda ninguna se
    vuelve al valor por defecto, porque una tabla sin columnas no es una
    preferencia, es una pantalla rota.
    """
    reales = {c for c, _, _ in disponibles}
    elegidas = [c for c in pedidas if c in reales]
    return elegidas or list(defecto)


def selector_columnas(accion: str, disponibles, visibles, filtro: dict,
                      orden: str = "", descendente: bool = False) -> str:
    """Un desplegable con una casilla por columna.

    Va dentro de un <details> y no siempre abierto: es una preferencia que se
    toca una vez y luego estorba. Los filtros y el orden viajan como campos
    ocultos para no perderlos al aplicar.
    """
    casillas = "".join(
        f'<label class="casilla"><input type="checkbox" name="col" value="{esc(c)}"'
        f'{" checked" if c in visibles else ""}> {esc(titulo)}</label>'
        for c, titulo, _ in disponibles
    )
    ocultos = "".join(
        f'<input type="hidden" name="{k}" value="{esc(v)}">'
        for k, v in (
            ("empresa", filtro.get("empresa", "")),
            ("grupo", filtro.get("grupo", "")),
            ("q", filtro.get("q", "")),
            ("orden", orden),
            ("dir", "desc" if descendente else ""),
        )
        if v
    )
    return f"""
  <details class="columnas">
    <summary><span class="caret">&#9656;</span> Columnas
      <span class="cuantas">{len(visibles)} de {len(disponibles)}</span></summary>
    <form method="get" action="{accion}">
      {ocultos}
      <div class="casillas">{casillas}</div>
      <div class="barra-acciones separado">
        <button type="submit">Aplicar</button>
      </div>
    </form>
  </details>
"""


def _reloj(segundos: int) -> str:
    """Segundos en algo legible: '4m 12s'."""
    segundos = max(0, int(segundos))
    if segundos < 60:
        return f"{segundos}s"
    return f"{segundos // 60}m {segundos % 60}s"


def _resumen_alcance(u) -> str:
    """Una linea que diga de un vistazo hasta donde llega esa cuenta."""
    if u.alcance.todo:
        return "toda la flota"
    partes = [f"{e} ({n})" for e, n in sorted(u.alcance.empresas.items())]
    if u.alcance.equipos:
        partes.append(f"{len(u.alcance.equipos)} equipos sueltos")
    return ", ".join(partes) if partes else "sin acceso a nada"


def usuarios(lista, roles, permisos_por_usuario, yo: str, sesion=None,
             mensaje: str = "", zona=None) -> str:
    """Cuentas y roles del panel."""
    filas = []
    for u in lista:
        soy_yo = u.nombre == yo
        if soy_yo:
            baja = '<span class="vacio">tu cuenta</span>'
        else:
            baja = (
                '<form method="post" action="/usuarios/baja" style="display:inline"'
                ' onsubmit="return confirm(&#39;Borrar esta cuenta? Se cerraran'
                ' sus sesiones abiertas.&#39;)">'
                f'<input type="hidden" name="nombre" value="{esc(u.nombre)}">'
                '<button class="peligro mini" type="submit">Borrar</button></form>'
            )
        cuantos = len(permisos_por_usuario.get(u.nombre, ()))
        filas.append(
            f"<tr><td>{esc(u.nombre)}"
            + (' <span class="etiqueta">tu</span>' if soy_yo else "")
            + f"</td><td>{esc(u.rol)}</td><td>{cuantos} permisos</td>"
            f"<td>{esc(_resumen_alcance(u))}</td>"
            f"<td>{esc(fecha(u.ultimo_acceso, zona) if u.ultimo_acceso else 'nunca')}</td>"
            f'<td class="acciones">'
            f'<a class="boton secundario mini" href="/usuarios/editar?nombre={esc(u.nombre)}">Editar</a> '
            f"{baja}</td></tr>"
        )

    filas_rol = []
    for nombre in sorted(roles):
        usados = sum(1 for u in lista if u.rol == nombre)
        borrar = ""
        if nombre != "admin" and not usados:
            borrar = (
                '<form method="post" action="/usuarios/rol/baja" style="display:inline">'
                f'<input type="hidden" name="rol" value="{esc(nombre)}">'
                '<button class="peligro mini" type="submit">Borrar</button></form>'
            )
        filas_rol.append(
            f"<tr><td>{esc(nombre)}</td><td>{len(roles[nombre])} permisos</td>"
            f"<td>{usados} cuentas</td>"
            f'<td class="acciones">'
            f'<a class="boton secundario mini" href="/usuarios/rol?rol={esc(nombre)}">Editar</a> '
            f"{borrar}</td></tr>"
        )

    cuerpo = f"""
  {f'<div class="bien">{esc(mensaje)}</div>' if mensaje else ""}
  <div class="barra-acciones">
    <a class="boton" href="/usuarios/nuevo">Anadir usuario</a>
    <a class="boton secundario" href="/usuarios/rol">Nuevo rol</a>
    <span class="sub crece">{len(lista)} cuentas</span>
  </div>
  <div class="tarjeta">
    <h2>Cuentas</h2>
    <div class="tabla-caja">
      <table>
        <thead><tr><th>Usuario</th><th>Rol</th><th>Permisos</th><th>Alcance</th>
                   <th>Ultimo acceso</th><th></th></tr></thead>
        <tbody>{"".join(filas)}</tbody>
      </table>
    </div>
  </div>
  <div class="tarjeta">
    <h2>Roles</h2>
    <p class="pista">Un rol es una plantilla de permisos. Cambiarlo afecta a
       todas las cuentas que lo tengan; lo que hayas ajustado a mano sobre una
       cuenta concreta se respeta.</p>
    <div class="tabla-caja">
      <table>
        <thead><tr><th>Rol</th><th>Permisos</th><th>En uso</th><th></th></tr></thead>
        <tbody>{"".join(filas_rol)}</tbody>
      </table>
    </div>
  </div>
"""
    return envoltura("mkbackup - usuarios", cuerpo, sesion, activo="usuarios")


def _casillas(nombre_campo: str, opciones, marcadas, etiquetas) -> str:
    """Lista de casillas. `opciones` son claves; `etiquetas` su texto."""
    filas = []
    for clave in opciones:
        puesta = " checked" if clave in marcadas else ""
        filas.append(
            f'<label class="casilla"><input type="checkbox" name="{nombre_campo}"'
            f' value="{esc(clave)}"{puesta}> {esc(etiquetas.get(clave, clave))}</label>'
        )
    return "".join(filas)


def formulario_permisos(datos: dict, roles: dict, permisos, etiquetas_permiso,
                        empresas, equipos, errores, alta: bool, sesion=None,
                        aviso: str = "") -> str:
    """Alta y edicion de una cuenta: quien es, que puede y sobre que.

    Las tres cosas van en la misma pantalla a proposito. Separarlas invita a
    crear una cuenta y dejarse el alcance para luego, y una cuenta a medio
    configurar en un panel multitenant es justo el accidente que hay que
    evitar.
    """
    lista_errores = "".join(f'<div class="error">{esc(e)}</div>' for e in errores)
    nombre = datos.get("nombre", "")
    titulo = "Anadir usuario" if alta else f"Editar {esc(nombre)}"
    accion = "/usuarios/nuevo" if alta else "/usuarios/editar"
    marcados = set(datos.get("permisos", ()))
    alcance = datos.get("alcance") or {}
    todo = bool(alcance.get("todo"))
    por_empresa = alcance.get("empresas", {})
    por_equipo = alcance.get("equipos", {})

    opciones_rol = "".join(
        f'<option value="{esc(r)}"{" selected" if r == datos.get("rol") else ""}>'
        f"{esc(r)}</option>"
        for r in sorted(roles)
    )
    # Cada rol lleva sus permisos al HTML para que elegirlo marque las casillas
    # sin ir al servidor. El servidor vuelve a validar lo que llegue: esto es
    # comodidad, no una comprobacion.
    mapa_roles = json.dumps({r: list(p) for r, p in roles.items()})

    campo_nombre = (
        f'<input id="nombre" type="text" name="nombre" value="{esc(nombre)}"'
        f' autocomplete="off" autofocus required>'
        if alta
        else f'<input type="hidden" name="nombre" value="{esc(nombre)}">'
             f"<p><code>{esc(nombre)}</code></p>"
    )

    def selector(prefijo, clave, actual):
        opciones = [("", "sin acceso"), ("ver", "solo ver"), ("editar", "ver y editar")]
        celdas = "".join(
            f'<option value="{v}"{" selected" if v == actual else ""}>{t}</option>'
            for v, t in opciones
        )
        return (f'<select name="{prefijo}:{esc(clave)}" class="nivel">{celdas}</select>')

    filas_empresa = "".join(
        f"<tr><td>{esc(e)}</td><td>{selector('empresa', e, por_empresa.get(e, ''))}</td></tr>"
        for e in empresas
    ) or '<tr><td colspan="2" class="vacio">Todavia no hay empresas.</td></tr>'

    filas_equipo = "".join(
        f"<tr><td>{esc(eq.nombre)}</td><td>{esc(eq.empresa)}</td>"
        f"<td>{selector('equipo', eq.nombre, por_equipo.get(eq.nombre, ''))}</td></tr>"
        for eq in equipos
    ) or '<tr><td colspan="3" class="vacio">Todavia no hay equipos.</td></tr>'

    cuerpo = f"""
  <div class="tarjeta medio">
    <h2>{titulo}</h2>
    {f'<div class="aviso">{esc(aviso)}</div>' if aviso else ""}
    {lista_errores}
    <form method="post" action="{accion}">
      <div class="rejilla">
        <div class="campo">
          <label for="nombre">Usuario</label>
          {campo_nombre}
        </div>
        <div class="campo">
          <label for="rol">Rol (plantilla)</label>
          <select id="rol" name="rol" onchange="aplicarRol(this.value)">{opciones_rol}</select>
          <div class="pista">Elegir un rol marca sus casillas. Despues puedes
               ajustarlas para esta persona en concreto.</div>
        </div>
      </div>
      <div class="campo">
        <label for="clave">Clave</label>
        <input id="clave" type="password" name="clave" value=""
               autocomplete="new-password" {"required" if alta else ""}>
        <div class="pista">{"Minimo 8 caracteres." if alta
                            else "Vacia para no cambiarla."}</div>
      </div>

      <h2 class="aparte">Que puede hacer</h2>
      <div class="casillas">{_casillas("permisos", permisos, marcados, etiquetas_permiso)}</div>

      <h2 class="aparte">Sobre que equipos</h2>
      <label class="casilla destacada">
        <input type="checkbox" name="todo" value="1" {"checked" if todo else ""}
               onchange="alternarAlcance(this.checked)">
        <b>Toda la flota</b>, incluidas las empresas que se creen mas adelante
      </label>
      <div id="detalle-alcance" {"hidden" if todo else ""}>
        <p class="pista">Un permiso sobre un equipo suelto SUMA al de su
           empresa: sirve para dar acceso a un router de una empresa que por lo
           demas no ve, o para dejarle editar uno concreto de una que solo mira.</p>
        <div class="tabla-caja" class="lista-alcance">
          <table>
            <thead><tr><th>Empresa</th><th>Acceso</th></tr></thead>
            <tbody>{filas_empresa}</tbody>
          </table>
        </div>
        <details class="separado">
          <summary class="sub">Equipos sueltos</summary>
          <div class="tabla-caja" class="lista-alcance">
            <table>
              <thead><tr><th>Equipo</th><th>Empresa</th><th>Acceso</th></tr></thead>
              <tbody>{filas_equipo}</tbody>
            </table>
          </div>
        </details>
      </div>

      <div class="barra-acciones separado">
        <button type="submit">Guardar</button>
        <a class="boton secundario" href="/usuarios">Cancelar</a>
      </div>
    </form>
  </div>
<script>
const ROLES = {mapa_roles};
function aplicarRol(rol) {{
  const permisos = ROLES[rol] || [];
  document.querySelectorAll('input[name="permisos"]').forEach(c => {{
    c.checked = permisos.indexOf(c.value) >= 0;
  }});
}}
function alternarAlcance(todo) {{
  document.getElementById("detalle-alcance").hidden = todo;
}}
</script>
"""
    return envoltura("mkbackup - " + titulo, cuerpo, sesion, activo="usuarios")


def formulario_rol(nombre: str, permisos_marcados, permisos, etiquetas_permiso,
                   errores, alta: bool, en_uso: int = 0, sesion=None) -> str:
    lista_errores = "".join(f'<div class="error">{esc(e)}</div>' for e in errores)
    titulo = "Nuevo rol" if alta else f"Rol {esc(nombre)}"
    campo = (
        f'<input id="rol" type="text" name="rol" value="{esc(nombre)}" autofocus required>'
        if alta
        else f'<input type="hidden" name="rol" value="{esc(nombre)}"><p><code>{esc(nombre)}</code></p>'
    )
    aviso = ""
    if en_uso:
        aviso = (f'<div class="aviso">Lo usan {en_uso} cuentas: lo que cambies '
                 "aqui les cambia a todas a la vez.</div>")
    cuerpo = f"""
  <div class="tarjeta estrecho">
    <h2>{titulo}</h2>
    {aviso}{lista_errores}
    <form method="post" action="/usuarios/rol">
      <div class="campo">
        <label for="rol">Nombre del rol</label>
        {campo}
      </div>
      <div class="casillas">
        {_casillas("permisos", permisos, permisos_marcados, etiquetas_permiso)}
      </div>
      <div class="barra-acciones separado">
        <button type="submit">Guardar</button>
        <a class="boton secundario" href="/usuarios">Cancelar</a>
      </div>
    </form>
  </div>
"""
    return envoltura("mkbackup - " + titulo, cuerpo, sesion, activo="usuarios")


def auditoria(eventos, usuarios_vistos, filtro: dict, sesion=None, zona=None,
              desde: int = 0, total: int = 0, por_pagina: int = 50,
              vigiladas=(), bloqueo_minutos: int = 5, mensaje: str = "") -> str:
    """Registro de accesos y cambios.

    Los intentos fallidos y los bloqueos se pintan en rojo: mirando la columna
    de color se ve de un vistazo si alguien estuvo probando, que es justo para
    lo que se abre esta pantalla.
    """
    from .auditoria import EVENTOS, SOSPECHOSOS

    # --- Direcciones con intentos fallidos ---------------------------------
    filas_ip = []
    for f in vigiladas:
        if f["bloqueada"]:
            estado = (f'<span class="etiqueta e-fallo">bloqueada</span>'
                      f' <span class="sub">{esc(_reloj(f["restantes"]))}</span>')
        else:
            estado = (f'<span class="etiqueta e-cambio">{esc(f["intentos"])} '
                      f"intentos</span>")
        filas_ip.append(
            f'<tr><td><code>{esc(f["ip"])}</code></td><td>{estado}</td>'
            f'<td class="acciones">'
            f'<form method="post" action="/auditoria/desbloquear" style="display:inline">'
            f'<input type="hidden" name="ip" value="{esc(f["ip"])}">'
            f'<button class="secundario mini" type="submit">Desbloquear</button>'
            f"</form></td></tr>"
        )

    if filas_ip:
        bloque_ips = f"""
  <div class="tarjeta">
    <h2>Direcciones con intentos fallidos</h2>
    <div class="tabla-caja">
      <table>
        <thead><tr><th>Direccion</th><th>Estado</th><th></th></tr></thead>
        <tbody>{"".join(filas_ip)}</tbody>
      </table>
    </div>
    <p class="pista">Tras varios fallos seguidos, una direccion se bloquea
       {bloqueo_minutos} minutos. Desbloquear borra su cuenta de intentos: es
       para cuando el bloqueo fue un despiste, no un ataque. Esta lista vive en
       la memoria del panel, asi que reiniciarlo la vacia entera.</p>
  </div>"""
    else:
        bloque_ips = ""

    filas = []
    for e in eventos:
        clase = "e-fallo" if e.get("evento") in SOSPECHOSOS else ""
        filas.append(
            f"<tr><td>{esc(fecha(e.get('cuando'), zona))}</td>"
            f'<td><span class="etiqueta {clase}">'
            f"{esc(EVENTOS.get(e.get('evento'), e.get('evento')))}</span></td>"
            f"<td>{esc(e.get('usuario') or '-')}</td>"
            f"<td><code>{esc(e.get('ip'))}</code></td>"
            f"<td>{esc(e.get('detalle'))}</td></tr>"
        )
    if not filas:
        filas.append(
            '<tr><td colspan="5" class="vacio">No hay eventos que coincidan.</td></tr>'
        )

    # El paginador conserva los filtros: cambiar de pagina no puede deshacer
    # lo que acabas de filtrar, o cada vez que avanzas pierdes la busqueda.
    def enlace(inicio):
        partes = [("desde", str(inicio))]
        for clave in ("evento", "usuario", "sospechosos"):
            if filtro.get(clave):
                partes.append((clave, filtro[clave]))
        return "/auditoria?" + urlencode(partes)

    pagina = desde // por_pagina + 1
    paginas = max(1, -(-total // por_pagina))   # division hacia arriba
    if total > por_pagina:
        anterior = (
            f'<a class="boton secundario mini" href="{enlace(max(0, desde - por_pagina))}">Anteriores</a>'
            if desde > 0 else
            '<span class="boton secundario mini apagado-boton">Anteriores</span>'
        )
        siguiente = (
            f'<a class="boton secundario mini" href="{enlace(desde + por_pagina)}">Siguientes</a>'
            if desde + por_pagina < total else
            '<span class="boton secundario mini apagado-boton">Siguientes</span>'
        )
        paginador = (
            f'<div class="paginador">{anterior}'
            f'<span class="sub">Pagina {pagina} de {paginas}</span>{siguiente}</div>'
        )
    else:
        paginador = ""

    desde_uno = desde + 1 if total else 0
    hasta = min(desde + por_pagina, total)
    titulo_tabla = (
        f"Eventos {desde_uno} a {hasta} de {total}" if total else "Sin eventos"
    )

    opciones_evento = "".join(
        f'<option value="{esc(c)}"{" selected" if c == filtro.get("evento") else ""}>'
        f"{esc(t)}</option>"
        for c, t in EVENTOS.items()
    )
    marcado = "checked" if filtro.get("sospechosos") == "1" else ""

    cuerpo = f"""
  {f'<div class="bien">{esc(mensaje)}</div>' if mensaje else ""}
  {bloque_ips}
  <form class="filtros" method="get" action="/auditoria">
    <div>
      <label for="f-evento">Evento</label>
      <select id="f-evento" name="evento" onchange="this.form.submit()">
        <option value="">Todos</option>{opciones_evento}
      </select>
    </div>
    <div>
      <label for="f-usuario">Usuario</label>
      <select id="f-usuario" name="usuario" onchange="this.form.submit()">
        {_opciones(usuarios_vistos, filtro.get("usuario", ""))}
      </select>
    </div>
    <div>
      <label class="casilla">
        <input type="checkbox" name="sospechosos" value="1" {marcado}
               onchange="this.form.submit()">
        Solo intentos y bloqueos
      </label>
    </div>
    <div class="filtro-botones">
      <button type="submit">Filtrar</button>
      <a class="boton secundario" href="/auditoria">Quitar filtros</a>
    </div>
  </form>
  <div class="tarjeta">
    <h2>{esc(titulo_tabla)}</h2>
    <div class="tabla-caja">
      <table>
        <thead><tr><th>Cuando</th><th>Evento</th><th>Usuario</th><th>Desde</th>
                   <th>Detalle</th></tr></thead>
        <tbody>{"".join(filas)}</tbody>
      </table>
    </div>
    {paginador}
    <p class="pista">Se guardan los ultimos eventos hasta 5 MB, y despues se
       conserva una generacion anterior del archivo. Aqui nunca se escribe una
       clave: de un cambio de credenciales solo queda que se cambio y quien.</p>
  </div>
"""
    return envoltura("mkbackup - auditoria", cuerpo, sesion, activo="auditoria")


def cuenta(nombre: str, etiqueta: str, errores, sesion=None, mensaje: str = "") -> str:
    lista_errores = "".join(f'<div class="error">{esc(e)}</div>' for e in errores)
    cuerpo = f"""
  {f'<div class="bien">{esc(mensaje)}</div>' if mensaje else ""}
  <div class="tarjeta angosto">
    <h2>Mi cuenta</h2>
    <p class="sub">{esc(nombre)} &middot; {esc(etiqueta)}</p>
    {lista_errores}
    <form method="post" action="/cuenta">
      <div class="campo">
        <label for="actual">Clave actual</label>
        <input id="actual" type="password" name="actual" autocomplete="current-password"
               autofocus required>
      </div>
      <div class="campo">
        <label for="nueva">Clave nueva</label>
        <input id="nueva" type="password" name="nueva" autocomplete="new-password" required>
        <div class="pista">Minimo 8 caracteres.</div>
      </div>
      <div class="campo">
        <label for="repetida">Repite la clave nueva</label>
        <input id="repetida" type="password" name="repetida" autocomplete="new-password" required>
      </div>
      <button type="submit">Cambiar la clave</button>
    </form>
    <p class="pista">Al cambiarla se cierran tus otras sesiones y tendras que
       volver a entrar aqui.</p>
  </div>
"""
    return envoltura("mkbackup - mi cuenta", cuerpo, sesion)


def error(codigo: int, texto: str) -> str:
    cuerpo = f"""
  <div class="tarjeta">
    <h2>Error {esc(codigo)}</h2>
    <p>{esc(texto)}</p>
    <div class="barra-acciones"><a class="boton secundario" href="/">Volver</a></div>
  </div>
"""
    return envoltura(f"mkbackup - error {codigo}", cuerpo)
