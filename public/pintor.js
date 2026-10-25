// Pintor del ramo: ejecuta en el canvas lo que ya decidió ramo_de_flores.py.
// Aquí no hay lógica (ni geometría, ni colores, ni qué carácter va en cada celda): solo se
// repiten llamadas al canvas, para no cruzar el puente Python→JS miles de veces por cuadro.

// Escena vectorial: `ordenes` es un Float64Array grabado por la clase Escena de Python, con
// [cabecera, ...n números] por orden; la cabecera empaqueta (tipo * 32 + nombre) * 8 + n.
window.reproducirEscena = (ctx, ordenes, textos, nombres) => {
  textos = textos.split('\n');
  const degradados = [];
  for (let i = 0; i < ordenes.length; ) {
    const c = ordenes[i], tipo = c >> 8, nombre = nombres[(c >> 3) & 31], n = c & 7;
    const a = ordenes.subarray(i + 1, i + 1 + n);
    i += 1 + n;
    if (tipo === 0) ctx[nombre].apply(ctx, a);                    // método con números
    else if (tipo === 1) degradados.push(ctx[nombre].apply(ctx, a)); // crea un degradado
    else if (tipo === 2) ctx[nombre] = a[0];                      // propiedad numérica
    else if (tipo === 3) ctx[nombre] = textos[a[0]];              // color o lineCap
    else if (tipo === 4) ctx[nombre] = degradados[a[0]];          // relleno con degradado
    else if (tipo === 5) degradados[a[0]].addColorStop(a[1], textos[a[2]]); // parada de color
    else ctx[nombre](textos[a[0]], a[1], a[2]);                   // fillText(texto, x, y)
  }
};

// celdas: Int32Array con [x, y, carácter, color] por glifo; color -1 = mismo que el anterior
window.pintarGlifos = (ctx, chars, colores, celdas, cw, ch) => {
  colores = colores.split('\n');
  for (let i = 0; i < celdas.length; i += 4) {
    if (celdas[i + 3] >= 0) ctx.fillStyle = colores[celdas[i + 3]];
    ctx.fillText(chars[celdas[i + 2]], celdas[i] * cw, celdas[i + 1] * ch);
  }
};
