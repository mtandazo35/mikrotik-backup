// Ejecuta el JS del panel contra un DOM de mentira y comprueba la geometria de
// la barra. Sin esto, "compila" es lo unico que se sabe, y una barra puede
// compilar perfectamente y salirse de su caja.
const fs = require("fs");

const js = fs.readFileSync(process.argv[2], "utf8");

// --- DOM minimo -------------------------------------------------------------
function nodo(id) {
  return { id, innerHTML: "", textContent: "", hidden: false, style: {},
           classList: { add() {}, remove() {}, toggle() {} } };
}
const nodos = {};
global.document = {
  getElementById: id => (nodos[id] = nodos[id] || nodo(id)),
  documentElement: {},
  addEventListener() {},
};
global.window = { matchMedia: () => ({ addEventListener() {}, matches: false }) };
global.getComputedStyle = () => ({
  getPropertyValue: k => ({
    "--v1": "#2a78d6", "--v2": "#eb6834", "--v3": "#1baf7a",
    "--v4": "#eda100", "--v5": "#e87ba4",
    "--v-bien": "#0ca30c", "--v-cambio": "#fab219",
    "--v-fallo": "#d03b3b", "--v-nada": "#898781",
  }[k] || ""),
});
global.setInterval = () => 0;
global.fetch = () => new Promise(() => {});

let fallos = 0;
function comprobar(desc, cond) {
  console.log(`[${cond ? "OK  " : "FALLA"}] ${desc}`);
  if (!cond) fallos++;
}

// Se evalua el guion entero: si tuviera un error de sintaxis o una constante
// repetida, revienta aqui.
eval(js);

// --- La geometria -----------------------------------------------------------
function anchos(html) {
  return [...html.matchAll(/<rect[^>]*x="([\d.]+)"[^>]*width="([\d.]+)"/g)]
    .map(m => ({ x: +m[1], w: +m[2] }));
}

const casos = [
  ["un solo equipo, respaldado", [
    { etq: "Sin cambios", valor: 1, color: "#0ca30c" },
    { etq: "Con cambios", valor: 0, color: "#fab219" },
    { etq: "Fallidos", valor: 0, color: "#d03b3b" },
    { etq: "Sin respaldar", valor: 0, color: "#898781" }]],
  ["flota de 300 con 1 fallo", [
    { etq: "Sin cambios", valor: 287, color: "#0ca30c" },
    { etq: "Con cambios", valor: 12, color: "#fab219" },
    { etq: "Fallidos", valor: 1, color: "#d03b3b" },
    { etq: "Sin respaldar", valor: 0, color: "#898781" }]],
  ["los cuatro estados a la vez", [
    { etq: "Sin cambios", valor: 40, color: "#0ca30c" },
    { etq: "Con cambios", valor: 30, color: "#fab219" },
    { etq: "Fallidos", valor: 20, color: "#d03b3b" },
    { etq: "Sin respaldar", valor: 10, color: "#898781" }]],
];

for (const [nombre, serie] of casos) {
  barra("b-estado", "l-estado", serie, "vacio");
  const html = nodos["b-estado"].innerHTML;
  const trozos = anchos(html);
  const vivas = serie.filter(s => s.valor > 0).length;
  const ultimo = trozos[trozos.length - 1];
  const derecha = ultimo ? ultimo.x + ultimo.w : 0;

  console.log(`\n--- ${nombre} ---`);
  comprobar(`hay un trozo por cada estado con valor (${trozos.length} de ${vivas})`,
            trozos.length === vivas);
  comprobar(`la barra no se sale de su caja (acaba en ${derecha.toFixed(2)} de 100)`,
            derecha <= 100.01);
  comprobar("ningun trozo tiene ancho negativo o cero",
            trozos.every(t => t.w > 0));
  comprobar("los trozos van en orden y no se solapan",
            trozos.every((t, i) => i === 0 || t.x >= trozos[i - 1].x + trozos[i - 1].w - 0.01));
  comprobar(`el estado mas pequeno se sigue viendo (el menor mide ` +
            `${Math.min(...trozos.map(t => t.w)).toFixed(2)})`,
            trozos.every(t => t.w >= 0.89));
  comprobar("cada trozo lleva su titulo para el raton",
            (html.match(/<title>/g) || []).length === vivas);
  comprobar("la leyenda trae una fila por estado vivo",
            (nodos["l-estado"].innerHTML.match(/<li>/g) || []).length === vivas);
  comprobar("las esquinas se recortan una sola vez, no trozo a trozo",
            (html.match(/rx="/g) || []).length === 2);
}

// --- Sin datos --------------------------------------------------------------
barra("b-estado", "l-estado", [{ etq: "x", valor: 0, color: "#000" }], "Nada aun.");
console.log("\n--- sin datos ---");
comprobar("con todo a cero se pinta la pista vacia y se dice",
          nodos["b-estado"].innerHTML.includes("var(--barra)")
          && nodos["l-estado"].innerHTML.includes("Nada aun."));

// --- Un cliente con nombre largo -------------------------------------------
barra("b-clientes", "l-clientes", [
  { etq: "Andinanet Telecomunicaciones Cia. Ltda.", valor: 8, color: "#2a78d6" },
  { etq: "Fibra Austral S.A.", valor: 3, color: "#eb6834" },
], "vacio");
console.log("\n--- nombres largos ---");
comprobar("el nombre largo va entero en la leyenda, no recortado en el SVG",
          nodos["l-clientes"].innerHTML.includes("Andinanet Telecomunicaciones"));
comprobar("y el SVG no lleva ni una etiqueta de texto dentro",
          !nodos["b-clientes"].innerHTML.includes("<text"));

console.log();
if (fallos) { console.log(`${fallos} comprobacion(es) fallaron`); process.exit(1); }
console.log("Todas las comprobaciones pasaron.");
