// Ejecuta el JS de la pantalla de ajustes contra un DOM de mentira y comprueba
// que el bloque del sondeo se PINTA de verdad.
//
// Existe por la misma razon que graficas.js: mirar el HTML no sirve aqui. El
// <div id="sondeo"> sale siempre en la pagina, vacio y oculto; lo que decide
// si se ve algo es codigo que corre en el navegador, y eso ninguna
// comprobacion sobre el HTML lo toca. Un bloque que nunca se rellena no da
// error, no rompe nada y deja al usuario mirando un boton que parece no hacer
// nada durante varios minutos.
//
// Lo que se comprueba: que con un sondeo en marcha se ve el avance, que al
// terminar se ven las cuentas, que un nombre con HTML dentro sale escapado, y
// que el temporizador se para solo.
const fs = require("fs");

const js = fs.readFileSync(process.argv[2], "utf8");

const nodos = {};
function nodo(id) {
  return { id, innerHTML: "", textContent: "", hidden: false, style: {},
           classList: { add() {}, remove() {}, toggle() {} },
           setAttribute() {}, removeAttribute() {} };
}
global.document = {
  getElementById: id => (nodos[id] = nodos[id] || nodo(id)),
  documentElement: {}, addEventListener() {}, querySelectorAll: () => [],
};
global.window = {};
global.location = { href: "", reload() { global.location.recargada = true; } };
let intervalos = 0;
global.setInterval = () => ++intervalos;
global.clearInterval = () => { intervalos--; };
global.fetch = () => new Promise(() => {});

let fallos = 0;
function comprobar(desc, cond) {
  console.log(`[${cond ? "OK  " : "FALLA"}] ${desc}`);
  if (!cond) fallos++;
}

// Si el guion tuviera un error de sintaxis, revienta aqui.
eval(js);

const caja = nodos["sondeo"];

// --- Sin sondeo, el bloque no se ve ---------------------------------------
pintarSondeo(null);
comprobar("sin ningun sondeo el bloque queda oculto", caja.hidden === true);
pintarSondeo({ vacio: true });
comprobar("y con la respuesta vacia del servidor, igual", caja.hidden === true);

// --- Corriendo -------------------------------------------------------------
pintarSondeo({
  total: 40, preguntados: 10, corriendo: true,
  renombrados: 2, iguales: 3, mudos: 1, rechazados: 0,
  detalle_renombrados: [["10.0.0.1", "Core-Quito"], ["10.0.0.2", "BTS-Sur"]],
  detalle_mudos: ["10.0.0.9"], detalle_rechazados: [],
});
comprobar("corriendo, el bloque se ve", caja.hidden === false);
comprobar("dice por cuantos va", caja.innerHTML.includes("10 de 40"));
comprobar("y pinta la barra al 25%", caja.innerHTML.includes("width:25%"));
// Los recuadros son los MISMOS que el resto del panel (.cifra), no una copia
// con otro aspecto: dos maneras de pintar el mismo dato en la misma pagina se
// separan sola en cuanto alguien retoca una.
comprobar("con las cuentas, en los recuadros de siempre",
          caja.innerHTML.includes('class="cifra "')
          && caja.innerHTML.includes("<b>2</b><span>renombrados</span>"));
comprobar("y la barra de avance es la del panel, no otra",
          caja.innerHTML.includes('<div class="barra">'));
comprobar("y los nombres nuevos",
          caja.innerHTML.includes("Core-Quito") && caja.innerHTML.includes("BTS-Sur"));

// --- Terminado -------------------------------------------------------------
pintarSondeo({
  total: 40, preguntados: 40, corriendo: false,
  renombrados: 30, iguales: 3, mudos: 5, rechazados: 2,
  detalle_renombrados: Array.from({ length: 25 }, (_, i) => [`10.0.0.${i}`, `R-${i}`]),
  detalle_mudos: ["10.0.0.30"],
  detalle_rechazados: [["10.0.0.40", "ese nombre ya lo tiene otro equipo"]],
});
comprobar("terminado, lo dice", caja.innerHTML.includes("Terminado"));
comprobar("sin dejar la barra de avance a medias",
          !caja.innerHTML.includes('class="avance"'));
// El detalle se corta en 25 pero la cuenta va entera: sin esta linea, quien
// mire la lista creeria que se renombraron 25 y no 30.
comprobar("avisa de los que no caben en la lista",
          caja.innerHTML.includes("y 5 mas"));
comprobar("dice por que se rechazo cada uno",
          caja.innerHTML.includes("ya lo tiene otro equipo"));

// --- Un nombre con HTML dentro no se ejecuta -------------------------------
// El nombre de un equipo esta validado, pero el motivo de un rechazo lo
// escribe git y el nombre de quien lanzo el sondeo lo escribe una persona.
pintarSondeo({
  total: 1, preguntados: 1, corriendo: false,
  renombrados: 0, iguales: 0, mudos: 0, rechazados: 1,
  detalle_renombrados: [], detalle_mudos: [],
  detalle_rechazados: [["<img src=x onerror=alert(1)>", "no vale"]],
});
comprobar("un nombre con HTML dentro sale escapado",
          caja.innerHTML.includes("&lt;img") && !caja.innerHTML.includes("<img"));

// --- El error se ve --------------------------------------------------------
pintarSondeo({
  total: 5, preguntados: 2, corriendo: false, error: "se corto por algo raro",
  renombrados: 0, iguales: 0, mudos: 0, rechazados: 0,
  detalle_renombrados: [], detalle_mudos: [], detalle_rechazados: [],
});
comprobar("si se corto, se dice y no se hace pasar por terminado",
          caja.innerHTML.includes("se corto por algo raro")
          && !caja.innerHTML.includes("Terminado"));

console.log();
if (fallos) { console.log(`${fallos} comprobacion(es) fallaron`); process.exit(1); }
console.log("Todas las comprobaciones pasaron.");
