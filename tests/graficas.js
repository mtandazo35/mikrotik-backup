// Ejecuta el JS del panel contra un DOM de mentira y comprueba que TODAS sus
// graficas se dibujan de verdad.
//
// Existe por un fallo concreto: al quitar un bloque del panel se fue por
// delante la llamada que dibujaba una de las tartas. El <svg> seguia en el
// HTML, asi que la comprobacion de estructura pasaba tan contenta; lo que
// quedaba en pantalla era un hueco. Un contenedor vacio no da ningun error, no
// rompe el layout y no lo ve ninguna prueba que mire el HTML.
//
// La regla que se comprueba: si el HTML declara un <svg> para una grafica,
// tiene que haber codigo que lo llene. Y al reves, ninguna leyenda puede
// quedarse sin su lista.
const fs = require("fs");

const js = fs.readFileSync(process.argv[2], "utf8");
const html = fs.readFileSync(process.argv[3], "utf8");

function nodo(id) {
  return { id, innerHTML: "", textContent: "", hidden: false, style: {},
           classList: { add() {}, remove() {}, toggle() {} },
           setAttribute() {}, removeAttribute() {} };
}
const nodos = {};
global.document = {
  getElementById: id => (nodos[id] = nodos[id] || nodo(id)),
  documentElement: {}, addEventListener() {}, querySelectorAll: () => [],
};
global.window = { matchMedia: () => ({ addEventListener() {}, matches: false }) };
global.getComputedStyle = () => ({
  getPropertyValue: k => ({
    "--v1": "#2a78d6", "--v2": "#eb6834", "--v3": "#1baf7a",
    "--v4": "#eda100", "--v5": "#e87ba4", "--v-bien": "#0ca30c",
    "--v-cambio": "#fab219", "--v-fallo": "#d03b3b", "--v-nada": "#898781",
  }[k] || ""),
});
global.setInterval = () => 0;
global.fetch = () => new Promise(() => {});

let fallos = 0;
function comprobar(desc, cond) {
  console.log(`[${cond ? "OK  " : "FALLA"}] ${desc}`);
  if (!cond) fallos++;
}

// Si el guion tuviera un error de sintaxis o una const repetida, revienta aqui.
eval(js);

// Una flota con de todo: varios clientes, varios estados, nombres largos y un
// cliente con un solo equipo (el sector diminuto).
const equipos = [];
const meter = (empresa, n, estado) => {
  for (let i = 0; i < n; i++)
    equipos.push({ nombre: `${empresa}-${i}`, empresa, estado,
                   ip: "10.0.0.1", grupo: "core" });
};
meter("Andinanet Telecomunicaciones Cia. Ltda.", 40, "sin_cambios");
meter("Andinanet Telecomunicaciones Cia. Ltda.", 3, "fallo");
meter("Fibra Austral S.A.", 22, "sin_cambios");
meter("Conecta Sur", 14, "cambio");
meter("Redes del Valle", 7, "sin_cambios");
meter("Cliente Chico", 1, "pendiente");

leerPaleta();
pintar({ equipos, total: equipos.length,
         programador: { proxima: "2030-01-01T00:00:00+00:00" } });

// --- Todo <svg id=...> del HTML tiene que acabar con algo dentro -----------
const svgs = [...html.matchAll(/<svg[^>]*\bid="([^"]+)"/g)].map(m => m[1]);
comprobar(`el panel declara graficas (${svgs.join(", ")})`, svgs.length > 0);
for (const id of svgs) {
  const n = nodos[id];
  comprobar(`la grafica ${id} tiene codigo que la dibuja`,
            !!n && n.innerHTML.length > 0);
  if (n && n.innerHTML) {
    comprobar(`la grafica ${id} pinta sectores, no solo el fondo`,
              /<path|<circle|<rect/.test(n.innerHTML));
  }
}

// --- Y toda leyenda declarada tiene que llenarse ---------------------------
const listas = [...html.matchAll(/<ul[^>]*\bid="(l-[^"]+)"/g)].map(m => m[1]);
for (const id of listas) {
  const n = nodos[id];
  comprobar(`la leyenda ${id} se rellena`,
            !!n && n.innerHTML.includes("<li>"));
}

// --- Los recuadros ---------------------------------------------------------
const cifras = nodos["cifras"] ? nodos["cifras"].innerHTML : "";
for (const etiqueta of ["Clientes", "Equipos", "Respaldados", "Fallidos",
                        "Proximo ciclo"]) {
  comprobar(`el recuadro "${etiqueta}" esta`, cifras.includes(`>${etiqueta}<`));
}
// Con cadenas y no con expresiones regulares: un "</b>" dentro de una regex
// la cierra antes de tiempo, y el error que da (flags invalidas) no se parece
// en nada al problema.
comprobar("ningun recuadro se pinta de verde: lo que va bien no se resalta",
          !cifras.includes('class="cifra bien"'));
comprobar("y el de fallos si se marca cuando los hay",
          cifras.includes('class="cifra malo"><b>3</b>'));

// --- Los sectores no se salen del circulo ----------------------------------
const dentro = (svg) => {
  const nums = [...svg.matchAll(/[ML] ([\d.]+) ([\d.]+)/g)];
  return nums.every(m => {
    const dx = +m[1] - 85, dy = +m[2] - 85;
    return Math.sqrt(dx * dx + dy * dy) <= 83.5;
  });
};
for (const id of svgs) {
  if (nodos[id] && nodos[id].innerHTML.includes("<path")) {
    comprobar(`los sectores de ${id} caben en el circulo`,
              dentro(nodos[id].innerHTML));
  }
}

// El texto de dentro no puede quedar fuera del sector mas pequeno: por debajo
// de cierto tamano no se escribe nada, y eso es lo correcto.
const chico = nodos["t-clientes"] ? nodos["t-clientes"].innerHTML : "";
const nombresDentro = [...chico.matchAll(/class="sector-nom"[^>]*>([^<]*)</g)]
  .map(m => m[1]);
comprobar(`los nombres de dentro estan recortados (${nombresDentro.join(" | ")})`,
          nombresDentro.every(n => n.length <= 17));

console.log();
if (fallos) { console.log(`${fallos} comprobacion(es) fallaron`); process.exit(1); }
console.log("Todas las comprobaciones pasaron.");
