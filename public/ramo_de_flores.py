# Ramo de lirios ASCII — port en Python de ramo_de_flores.html.
# Corre en el navegador con PyScript (Pyodide). Toda la lógica está aquí; pintor.js solo
# ejecuta en el canvas, de una vez por cuadro, las llamadas que este archivo prepara.
import math
from array import array
from bisect import bisect_right
from itertools import groupby

import numpy as np
from pyodide.ffi import create_once_callable, create_proxy, to_js
from pyscript import document, window

# ---------- utilidades ----------
M32 = 0xFFFFFFFF


def mulberry32(a):
    # misma secuencia que la versión JS: se trabaja en 32 bits sin signo (equivalente bit a bit)
    a &= M32

    def r():
        nonlocal a
        a = (a + 0x6D2B79F5) & M32
        t = ((a ^ (a >> 15)) * (1 | a)) & M32
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & M32)) & M32) ^ t
        return (t ^ (t >> 14)) / 4294967296

    return r


def clamp(v, a=0, b=1):
    return min(b, max(a, v))


def ease(t):
    return 1 - math.pow(1 - clamp(t), 3)


def smooth(a, b, t):
    t = clamp((t - a) / (b - a))
    return t * t * (3 - 2 * t)


def mix(a, b, t):
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]


def num(v):
    # número → texto exactamente como String(v) en JS, para que los colores CSS sean los mismos
    if 1e-4 <= abs(v) < 1e16 and v != int(v):
        return repr(v)  # en este rango repr() de Python ya escribe igual que JS (caso común)
    if not math.isfinite(v):
        return "NaN" if v != v else ("Infinity" if v > 0 else "-Infinity")
    if v == 0:
        return "0"
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    s = repr(float(v))
    sign = "-" if s[0] == "-" else ""
    mant, _, exp = s.lstrip("-").partition("e")
    ent, _, frac = mant.partition(".")
    dig = (ent + frac).lstrip("0")
    n = len(ent) - (len(ent + frac) - len(dig)) + int(exp or 0)
    dig = dig.rstrip("0")
    k = len(dig)
    if k <= n <= 21:
        return sign + dig + "0" * (n - k)
    if 0 < n <= 21:
        return sign + dig[:n] + "." + dig[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + dig
    e = n - 1
    return sign + dig[0] + ("." + dig[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))


def css(c, al=1):
    return f"rgba({int(c[0])},{int(c[1])},{int(c[2])},{num(al)})"


def lerp_ang(a, b, t):
    d = b - a
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return a + d * t


def js_round(v):
    # Math.round de JS: las mitades van hacia +∞ (round() de Python redondea al par)
    f = math.floor(v)
    return f + 1 if v - f >= 0.5 else f


TAU = math.pi * 2


class Ctx2D:
    # contexto 2D con cada método enlazado una sola vez: buscarlo a través del puente
    # Python→JS en cada llamada costaría más que la llamada misma
    def __init__(self, ctx):
        object.__setattr__(self, "_ctx", ctx)

    def __getattr__(self, name):
        attr = getattr(self._ctx, name)
        if callable(attr):
            object.__setattr__(self, name, attr)
        return attr

    def __setattr__(self, name, value):
        setattr(self._ctx, name, value)


# métodos que se graban al dibujar la escena, con la cantidad de números que recibe cada uno
METODOS = {"save": 0, "restore": 0, "translate": 2, "rotate": 1, "scale": 2, "setTransform": 6, "clearRect": 4,
           "beginPath": 0, "moveTo": 2, "lineTo": 2, "quadraticCurveTo": 4, "bezierCurveTo": 6, "arc": 5,
           "ellipse": 7, "fill": 0, "stroke": 0, "rect": 4, "clip": 0, "closePath": 0}
# más los que crean un degradado, fillText y las propiedades que se asignan (máximo 32 nombres)
ORDENES = (*METODOS, "createLinearGradient", "createRadialGradient", "fillText",
           "fillStyle", "strokeStyle", "lineWidth", "lineCap", "font", "textAlign", "textBaseline", "globalAlpha")
INDICE = {nombre: i for i, nombre in enumerate(ORDENES)}
# tipos de orden (pintor.js los interpreta igual)
LLAMAR, CREAR_DEGRADADO, PROP_NUMERO, PROP_TEXTO, PROP_DEGRADADO, PARADA_COLOR, LLAMAR_TEXTO = range(7)


def cabecera(tipo, nombre, n):
    # tipo, método y cantidad de números que siguen, empaquetados en un solo número
    return (tipo * 32 + INDICE.get(nombre, 0)) * 8 + n


class Escena:
    # Contexto 2D que no dibuja al momento: graba cada llamada como números (una cabecera y
    # sus argumentos) y pintor.js las ejecuta todas, en el mismo orden, justo antes de leer
    # los píxeles. La escena son ~9.000 llamadas por cuadro y cruzar a JS una por una sale caro.
    def __init__(self, ctx):
        object.__setattr__(self, "ctx", ctx)
        object.__setattr__(self, "nombres", to_js(list(ORDENES)))
        self._vaciar()

    def _vaciar(self):
        object.__setattr__(self, "ordenes", [])  # lista plana de números; se pasa a Float64Array al final
        object.__setattr__(self, "textos", [])
        object.__setattr__(self, "n_degradados", 0)

    def _texto(self, s):
        self.textos.append(s)
        return len(self.textos) - 1

    def __setattr__(self, nombre, valor):
        if isinstance(valor, Degradado):
            self.ordenes.extend((cabecera(PROP_DEGRADADO, nombre, 1), valor.i))
        elif isinstance(valor, str):
            self.ordenes.extend((cabecera(PROP_TEXTO, nombre, 1), self._texto(valor)))
        else:
            self.ordenes.extend((cabecera(PROP_NUMERO, nombre, 1), valor))

    def _degradado(self, nombre, args):
        self.ordenes.extend((cabecera(CREAR_DEGRADADO, nombre, len(args)), *args))
        object.__setattr__(self, "n_degradados", self.n_degradados + 1)
        return Degradado(self, self.n_degradados - 1)

    def trazo(self, puntos):
        # igual que moveTo(primer punto) seguido de lineTo(cada uno de los demás)
        linea = cabecera(LLAMAR, "lineTo", 2)
        self.ordenes.extend((cabecera(LLAMAR, "moveTo", 2), *puntos[0]))
        self.ordenes.extend([v for x, y in puntos[1:] for v in (linea, x, y)])

    def createLinearGradient(self, *args):
        return self._degradado("createLinearGradient", args)

    def createRadialGradient(self, *args):
        return self._degradado("createRadialGradient", args)

    def fillText(self, texto, x, y):
        self.ordenes.extend((CAB_TEXTO, self._texto(texto), x, y))

    def getImageData(self, *args):
        # antes de leer los píxeles se ejecuta todo lo grabado en este cuadro
        datos = window.Float64Array.new(len(self.ordenes))
        datos.assign(array("d", self.ordenes))
        window.reproducirEscena(self.ctx, datos, "\n".join(self.textos), self.nombres)
        self._vaciar()
        return self.ctx.getImageData(*args)


def _orden(nombre, n):
    cab = (cabecera(LLAMAR, nombre, n),)  # cada método se usa siempre con n argumentos numéricos

    def llamar(self, *args):
        self.ordenes.extend(cab + args)
    return llamar


for _nombre, _n in METODOS.items():
    setattr(Escena, _nombre, _orden(_nombre, _n))
CAB_TEXTO = cabecera(LLAMAR_TEXTO, "fillText", 3)  # texto, x, y


class Degradado:
    CAB = cabecera(PARADA_COLOR, "", 3)  # degradado, posición, color

    def __init__(self, escena, i):
        self.escena, self.i = escena, i

    def addColorStop(self, pos, color):
        self.escena.ordenes.extend((self.CAB, self.i, pos, self.escena._texto(color)))


# ---------- lienzos ----------
cvs = document.getElementById("c")
ctx = Ctx2D(cvs.getContext("2d"))
off = document.createElement("canvas")  # escena vectorial a resolución de "celdas"
octx = Escena(off.getContext("2d", to_js({"willReadFrequently": True}, dict_converter=window.Object.fromEntries)))
RAMP = " .,:-=+*xoX#%@"  # de menos a más denso
STAR_ON, STAR_OFF = RAMP.index("+"), RAMP.index(".")  # estrella brillante / tenue
FONT = '"JetBrains Mono", ui-monospace, Menlo, Consolas, monospace'
RM = window.matchMedia("(prefers-reduced-motion: reduce)").matches

W = H = DPR = fs = cw = ch = cols = rows = S = bx = by = U = None
S0 = BX0 = BY0 = None  # tamaño y base del ramo antes de hacerle sitio al mensaje
stars = []
img = None  # píxeles RGBA del lienzo de celdas (se reutiliza entre cuadros)


def resize(*_):
    global W, H, DPR, fs, cw, ch, cols, rows, S, bx, by, U, S0, BX0, BY0, stars, img
    DPR = min(window.devicePixelRatio or 1, 2)
    W = window.innerWidth
    H = window.innerHeight
    cvs.width = W * DPR
    cvs.height = H * DPR
    fs = max(5, min(8, js_round(min(W, H) / 135)))
    ch = fs
    cw = fs * 0.6
    cols = math.ceil(W / cw)
    rows = math.ceil(H / ch)
    off.width = cols
    off.height = rows
    S = min(H * 0.76, W * 1.25)  # tamaño del ramo en px
    bx = W / 2
    by = H * 0.47 + S * 0.55  # punto base de los tallos
    U = cw / S  # una celda en unidades de escena
    S0, BX0, BY0 = S, bx, by
    r = mulberry32(99)
    stars = []
    n = js_round(cols * rows * 0.004)
    for _ in range(n):
        stars.append((int(r() * cols), int(r() * rows), r() * TAU, 0.6 + r() * 2.2))  # x, y, fase, velocidad
    img = np.empty(cols * rows * 4, np.uint8)
    componer()
    medir_portada()


# ---------- paleta ----------
class Pal:
    def __init__(self, d, b, l, spot=False):
        self.d, self.b, self.l, self.spot = d, b, l, spot


# tonos del ramo de idea.png, de más claro a más intenso; calibrados para cómo se ven tras el
# refuerzo x1.45 del paso ASCII (un rosa demasiado pálido terminaría viéndose blanco)
PAL = {
    "blanco": Pal([96, 128, 70], [222, 228, 214], [250, 252, 246], False),  # garganta verdosa
    "nacarado": Pal([150, 50, 96], [226, 210, 214], [250, 244, 246], True),  # blanco de centro rosado
    "rubor": Pal([176, 66, 96], [226, 116, 128], [238, 136, 146], True),  # rosa claro, cálido
    "rosa": Pal([140, 26, 72], [228, 80, 128], [246, 116, 156], True),
    "fucsia": Pal([120, 10, 54], [214, 44, 100], [238, 80, 132], True),  # rosa intenso
}
GREEN = Pal([22, 64, 28], [74, 150, 64], [150, 214, 110])
STEM = [104, 196, 88]

# ---------- escena (unidades: base en 0,0; y negativo hacia arriba; alto ≈ 1) ----------
rnd = mulberry32(7)
# x, y, tamaño, color — en orden de dibujo (atrás → adelante)
# 23 lirios en una cúpula redonda y tupida, como en idea.png: los del borde, más pequeños
# (quedan atrás); los del centro, más grandes (adelante). Caben en un celular vertical
# (|x| + tamaño ≤ 0.385). Dos flores que se tocan nunca comparten nivel de tono
# (blanco/nacarado < rubor < rosa < fucsia) y casi siempre están a dos niveles o más.
SPEC = [
    (0.098, -1.041, 0.095, "rosa"),
    (0.119, -0.368, 0.095, "fucsia"),
    (-0.037, -1.055, 0.095, "blanco"),
    (-0.154, -0.388, 0.095, "rubor"),
    (-0.164, -1.005, 0.095, "fucsia"),
    (0.211, -0.965, 0.096, "nacarado"),
    (0.226, -0.452, 0.096, "nacarado"),
    (-0.257, -0.905, 0.096, "blanco"),
    (0.284, -0.850, 0.096, "rosa"),
    (0.288, -0.573, 0.097, "fucsia"),
    (-0.286, -0.629, 0.099, "blanco"),
    (-0.286, -0.770, 0.099, "fucsia"),
    (0.285, -0.712, 0.100, "blanco"),
    (-0.016, -0.409, 0.103, "nacarado"),
    (-0.189, -0.524, 0.105, "rosa"),
    (0.019, -0.919, 0.109, "rubor"),
    (0.096, -0.512, 0.110, "rosa"),
    (-0.121, -0.854, 0.111, "rosa"),
    (0.142, -0.823, 0.111, "fucsia"),
    (-0.155, -0.699, 0.113, "rubor"),
    (0.143, -0.662, 0.113, "rubor"),
    (-0.044, -0.584, 0.115, "fucsia"),
    (0.001, -0.745, 0.118, "blanco"),
]
BUDS = [(-0.27, -1.035, 0.075), (0.235, -1.07, 0.075), (0.32, -0.975, 0.065)]


class Obj:
    # objeto con atributos libres, como los literales {…} de JS
    def __init__(self, **kw):
        self.__dict__.update(kw)


def make_stem(x, y):
    x0 = x * 0.18 + (rnd() - 0.5) * 0.03
    return Obj(p0=[x0, 0.01], p1=[x0 + (x - x0) * 0.22, y * 0.55], p2=[x, y])


def make_stem_leaves(st):
    out = []
    side = 1 if rnd() < 0.5 else -1
    for t in (0.28 + rnd() * 0.1, 0.48 + rnd() * 0.14):
        out.append(Obj(t=t, side=side, L=0.07 + rnd() * 0.05, w=0.014 + rnd() * 0.006, off=0.5 + rnd() * 0.35, bendK=0.25 + rnd() * 0.3))
        side = -side
    return out


def make_lily(i, x, y, size, col):
    st = make_stem(x, y)
    stem_start = 0.1 + rnd() * 0.5
    stem_dur = 1.5 + rnd() * 0.5
    return Obj(
        x=x, y=y, size=size, pal=PAL[col], seed=i * 17 + 3, bud=False,
        stem=st, leaves=make_stem_leaves(st),
        rot=rnd() * TAU, tilt=0.62 + rnd() * 0.36, tiltAng=rnd() * TAU, ph=rnd() * TAU,
        stemStart=stem_start, stemDur=stem_dur,
        budStart=stem_start + stem_dur - 0.35, budDur=0.9,
        bloomStart=2.9 + rnd() * 1.4, bloomDur=1.5,
    )


def make_bud(i, x, y, size):
    st = make_stem(x, y)
    stem_start = 0.3 + rnd() * 0.6
    stem_dur = 1.4 + rnd() * 0.4
    return Obj(
        x=x, y=y, size=size, pal=PAL["blanco"], seed=900 + i, bud=True,
        stem=st, leaves=make_stem_leaves(st),
        rot=0, tilt=1, tiltAng=0, ph=rnd() * TAU,
        stemStart=stem_start, stemDur=stem_dur, budStart=stem_start + stem_dur - 0.3, budDur=1.1,
        bloomStart=math.inf, bloomDur=1,
    )


lilies = [make_lily(i, *s) for i, s in enumerate(SPEC)]
buds = [make_bud(i, *b) for i, b in enumerate(BUDS)]
all_stems = [*buds, *lilies]

# hojas de la base: abanico denso
base_leaves = []
_n = 26
for _i in range(_n):
    _f = _i / (_n - 1) - 0.5
    _ang = -math.pi / 2 + _f * 2.5 + (rnd() - 0.5) * 0.25
    _k = abs(math.cos(_ang))
    base_leaves.append(Obj(
        x=(rnd() - 0.5) * 0.09, y=-rnd() * 0.05, ang=_ang,
        L=(0.13 + rnd() * 0.15) * (1.15 - _k * 0.35), w=0.022 + rnd() * 0.016,
        bend=(1 if math.cos(_ang) >= 0 else -1) * _k * (0.25 + rnd() * 0.35),
        start=0.5 + rnd() * 1.6, dur=1.0 + rnd() * 0.4,
    ))
base_leaves.sort(key=lambda lf: lf.L)  # orden estable, igual que Array.prototype.sort
base_leaves.reverse()  # las largas atrás


# ---------- geometría ----------
def q_at(s, t):
    u = 1 - t
    return [u * u * s.p0[0] + 2 * u * t * s.p1[0] + t * t * s.p2[0], u * u * s.p0[1] + 2 * u * t * s.p1[1] + t * t * s.p2[1]]


def q_tan(s, t):
    dx = 2 * (1 - t) * (s.p1[0] - s.p0[0]) + 2 * t * (s.p2[0] - s.p1[0])
    dy = 2 * (1 - t) * (s.p1[1] - s.p0[1]) + 2 * t * (s.p2[1] - s.p1[1])
    return math.atan2(dy, dx)


# ---------- dibujo vectorial ----------
# colores que no cambian nunca: se formatean una sola vez
STEM_CSS = css(STEM)
LEAF_STOPS = (css(GREEN.d), css(GREEN.b), css(GREEN.l))
CONTORNO = "rgba(5,5,7,0.85)"
_spot_rnd = {}


def spot_rands(seed, k):
    # las pintas de cada pétalo usan siempre la misma secuencia aleatoria: se calcula una vez
    v = _spot_rnd.get((seed, k))
    if v is None:
        r = mulberry32(seed * 31 + k)
        v = _spot_rnd[(seed, k)] = [r() for _ in range(12)]
    return v


def draw_stem(o, prog):
    if prog <= 0:
        return
    s = o.cur
    octx.strokeStyle = STEM_CSS
    octx.lineWidth = max(0.0055, U * 1.1)
    octx.lineCap = "round"
    octx.beginPath()
    N = 28
    octx.trazo([q_at(s, (i / N) * prog) for i in range(N + 1)])  # moveTo + 28 lineTo
    octx.stroke()


def draw_leaf(x, y, ang, L, w, bend, sc):
    if sc <= 0.01:
        return
    L *= sc
    w *= sc
    octx.save()
    octx.translate(x, y)
    octx.rotate(ang)
    g = octx.createLinearGradient(0, 0, L, 0)
    g.addColorStop(0, LEAF_STOPS[0])
    g.addColorStop(0.4, LEAF_STOPS[1])
    g.addColorStop(1, LEAF_STOPS[2])
    octx.fillStyle = g
    octx.beginPath()
    octx.moveTo(0, 0)
    octx.quadraticCurveTo(L * 0.45, -w + bend * L * 0.12, L, bend * L * 0.35)
    octx.quadraticCurveTo(L * 0.55, w + bend * L * 0.12, 0, 0)
    octx.fill()
    octx.strokeStyle = "rgba(200,245,160,0.45)"
    octx.lineWidth = U * 0.7
    octx.beginPath()
    octx.moveTo(0, 0)
    octx.quadraticCurveTo(L * 0.5, bend * L * 0.12, L * 0.95, bend * L * 0.33)
    octx.stroke()
    octx.restore()


def petal(L, w, tonos, k, o, e):
    # tonos: (oscuro, base, claro, nervio, pintas) ya en texto CSS; son los mismos para los 6 pétalos
    d, b, l, nervio, pintas = tonos
    g = octx.createLinearGradient(0, 0, L, 0)
    g.addColorStop(0, d)
    g.addColorStop(0.28, b)
    g.addColorStop(0.8, l)
    g.addColorStop(1, b)
    octx.fillStyle = g
    octx.beginPath()
    octx.moveTo(0, 0)
    octx.bezierCurveTo(L * 0.22, -w * 0.95, L * 0.62, -w * 0.8, L, -w * 0.08)
    octx.bezierCurveTo(L * 0.66, w * 0.7, L * 0.24, w * 0.9, 0, 0)
    # contorno oscuro por fuera del pétalo (el relleno tapa la mitad interior del trazo): en
    # ASCII deja una línea de "aire" que separa cada flor de las que tiene detrás
    octx.strokeStyle = CONTORNO
    octx.lineWidth = U * 1.6
    octx.stroke()
    octx.fill()
    # nervio central
    octx.strokeStyle = nervio
    octx.lineWidth = max(w * 0.1, U * 0.6)
    octx.beginPath()
    octx.moveTo(L * 0.08, 0)
    octx.quadraticCurveTo(L * 0.5, -w * 0.05, L * 0.9, -w * 0.06)
    octx.stroke()
    # pintas típicas del lirio
    if pintas:
        r = spot_rands(o.seed, k)
        octx.fillStyle = pintas
        for i in range(0, 12, 2):
            px = L * (0.1 + r[i] * 0.38)
            py = (r[i + 1] - 0.5) * w * 0.8
            octx.beginPath()
            octx.arc(px, py, max(U * 0.55, L * 0.018), 0, TAU)
            octx.fill()


def draw_lily(o, cx, cy, axis, g, e):
    if g <= 0.01:
        return
    p = o.pal
    ct = 0.12 + 0.1 * g if o.bud else min(1, 0.22 * g + smooth(0, 0.55, e) * 0.85)
    C = Pal(mix(GREEN.d, p.d, ct), mix(GREEN.b, p.b, ct), mix(GREEN.l, p.l, ct))
    pintas = css(C.d, 0.9 * smooth(0.4, 0.9, e)) if p.spot and e > 0.4 else None
    tonos = (css(C.d), css(C.b), css(C.l), css(C.l, 0.5), pintas)
    octx.save()
    octx.translate(cx, cy)
    tl = 1 + (o.tilt - 1) * e  # perspectiva: algunos se ven de lado
    octx.rotate(o.tiltAng)
    octx.scale(1, tl)
    octx.rotate(-o.tiltAng)
    L0 = o.size * g * (0.72 + 0.28 * e)
    spread = ease(e)
    for k in (0, 2, 4, 1, 3, 5):  # 3 sépalos atrás, 3 pétalos al frente
        fin = o.rot + k * math.pi / 3
        bud_a = axis + (k - 2.5) * 0.07
        a = lerp_ang(bud_a, fin, spread)
        L = L0 * (0.93 if k % 2 else 1)
        w = L * (0.36 if k % 2 else 0.30) * (0.32 + 0.68 * e)
        octx.save()
        octx.rotate(a)
        petal(L, w, tonos, k, o, e)
        octx.restore()
    if e > 0.5:
        sa = smooth(0.5, 0.95, e)
        glow = octx.createRadialGradient(0, 0, 0, 0, 0, o.size * 0.16)
        glow.addColorStop(0, f"rgba(255,232,150,{num(0.55 * sa)})")
        glow.addColorStop(1, "rgba(255,232,150,0)")
        octx.fillStyle = glow
        octx.beginPath()
        octx.arc(0, 0, o.size * 0.16, 0, TAU)
        octx.fill()
        octx.lineWidth = U * 0.8
        filamento = f"rgba(222,232,190,{num(0.8 * sa)})"
        antera = f"rgba(206,98,38,{num(sa)})"
        for j in range(6):  # estambres
            ang = o.rot + math.pi / 6 + j * math.pi / 3 + math.sin(j * 7.1) * 0.2
            ln = o.size * (0.5 + 0.12 * math.sin(j * 3.3)) * sa
            ex = math.cos(ang) * ln
            ey = math.sin(ang) * ln
            octx.strokeStyle = filamento
            octx.beginPath()
            octx.moveTo(0, 0)
            octx.quadraticCurveTo(ex * 0.5 + ey * 0.1, ey * 0.5 - ex * 0.1, ex, ey)
            octx.stroke()
            octx.fillStyle = antera
            octx.beginPath()
            octx.ellipse(ex, ey, U * 1.4, U * 0.8, ang + math.pi / 2, 0, TAU)
            octx.fill()
        pa = o.rot + 0.4  # pistilo
        pl = o.size * 0.66 * sa
        octx.strokeStyle = f"rgba(176,206,128,{num(0.85 * sa)})"
        octx.beginPath()
        octx.moveTo(0, 0)
        octx.lineTo(math.cos(pa) * pl, math.sin(pa) * pl)
        octx.stroke()
        octx.fillStyle = f"rgba(150,190,100,{num(sa)})"
        octx.beginPath()
        octx.arc(math.cos(pa) * pl, math.sin(pa) * pl, U * 1.2, 0, TAU)
        octx.fill()
    octx.restore()


# ---------- listón ----------
CINTA = (css([150, 30, 80]), css([226, 82, 138]), css([252, 170, 200]))  # satín rosa: sombra, base, brillo
T_LISTON = max(o.bloomStart + o.bloomDur for o in lilies) - 1.1  # se anuda mientras abren las últimas flores


def ease_back(t):
    # sube rápido, se pasa un poco y vuelve: el "tirón" al anudar
    t = clamp(t) - 1
    return 1 + 2.7 * t ** 3 + 1.7 * t ** 2


def forma(trazar, relleno, celda=None):
    # contorno oscuro por fuera (como en los pétalos) y después el relleno; `celda` es el ancho
    # de una celda en las unidades en que se está dibujando
    octx.beginPath()
    trazar()
    octx.closePath()
    octx.strokeStyle = CONTORNO
    octx.lineWidth = (celda or U) * 1.6
    octx.stroke()
    octx.fillStyle = relleno
    octx.fill()


def satin(x0, y0, x1, y1):
    g = octx.createLinearGradient(x0, y0, x1, y1)
    g.addColorStop(0, CINTA[0])
    g.addColorStop(0.45, CINTA[2])
    g.addColorStop(1, CINTA[1])
    return g


PERLA = css([252, 242, 246])
FLORECITA = css([250, 248, 242])  # la "nube" (gypsophila) que asoma alrededor del moño
TALLITO = css([96, 170, 86])


def _ramitas():
    # ramitas de nube: tallo (inicio, control, punta) y sus florecitas (x, y, radio); fijas
    r = mulberry32(23)
    out = []
    for px, py in ((-0.24, -0.11), (-0.15, -0.19), (0.16, -0.19), (0.25, -0.1), (-0.27, 0.0), (0.28, 0.01)):
        ini, ctrl = (px * 0.25, py * 0.25), (px * 0.55 + 0.03 * (r() - 0.5), py * 0.75)
        flores = [(px + (r() - 0.5) * 0.06, py + (r() - 0.5) * 0.05, 0.006 + r() * 0.005) for _ in range(7)]
        flores += [(px * f + (r() - 0.5) * 0.03, py * f + (r() - 0.5) * 0.03, 0.005 + r() * 0.003) for f in (0.6, 0.7, 0.8)]
        out.append((ini, ctrl, (px, py), flores))
    return out


RAMITAS = _ramitas()


def draw_ribbon(T):
    # moño de florería atando los tallos: banda con perlas, cuatro lazadas rosadas y dos blancas,
    # nudo con perla, puntas largas, cintas rizadas y ramitas de nube asomando alrededor
    k = 1 if RM else ease_back((T - T_LISTON) / 0.8)
    if k <= 0.01:
        return
    v = 0 if RM else math.sin(T * 1.4) * 0.006  # vaivén de las puntas
    octx.save()
    octx.translate(0, -0.1)  # el nudo, a media altura de los tallos
    octx.scale(k, k)

    # ramitas de nube, por detrás de todo
    octx.strokeStyle = TALLITO
    octx.lineWidth = U * 0.9
    octx.beginPath()
    for ini, ctrl, punta, _ in RAMITAS:
        octx.moveTo(*ini)
        octx.quadraticCurveTo(*ctrl, *punta)
    octx.stroke()
    octx.fillStyle = FLORECITA
    octx.beginPath()
    for _, _, _, flores in RAMITAS:
        for x, y, rad in flores:
            octx.moveTo(x + rad, y)
            octx.arc(x, y, rad, 0, TAU)
    octx.fill()

    # banda que envuelve los tallos (luz al centro: se ve redonda) con una fila de perlas arriba y abajo
    def banda():
        octx.moveTo(-0.1, -0.03)
        octx.quadraticCurveTo(0, -0.018, 0.1, -0.03)
        octx.lineTo(0.098, 0.03)
        octx.quadraticCurveTo(0, 0.045, -0.098, 0.03)
    forma(banda, gradiente(-0.1, 0, 0.1, 0, CINTA[0], CINTA[2], CINTA[0]))
    octx.fillStyle = PERLA
    octx.beginPath()
    for i in range(9):
        x = -0.088 + i * 0.022
        curva = 1 - (x / 0.1) ** 2
        for y in (-0.03 + 0.006 * curva, 0.03 + 0.0075 * curva):
            octx.moveTo(x + 0.0065, y)
            octx.arc(x, y, 0.0065, 0, TAU)
    octx.fill()

    # puntas largas que caen y cintas rizadas a los costados
    for lado in (-1, 1):
        def punta(lado=lado):
            octx.moveTo(lado * 0.01, 0.01)
            octx.bezierCurveTo(lado * 0.05, 0.05, lado * 0.03, 0.09, lado * 0.075 + v, 0.135)
            octx.lineTo(lado * 0.095 + v, 0.115)
            octx.lineTo(lado * 0.115 + v, 0.14)
            octx.bezierCurveTo(lado * 0.07, 0.08, lado * 0.08, 0.04, lado * 0.035, 0)
        forma(punta, satin(0, 0, lado * 0.1, 0.14))
    octx.beginPath()
    for lado in (-1, 1):
        octx.moveTo(lado * 0.02, 0.01)
        octx.bezierCurveTo(lado * 0.12, 0.02, lado * 0.16, 0.08, lado * 0.12, 0.1)
        octx.bezierCurveTo(lado * 0.08, 0.12, lado * 0.1, 0.05, lado * 0.15, 0.06)
        octx.bezierCurveTo(lado * 0.2, 0.07, lado * 0.2, 0.12, lado * 0.17 + v, 0.13)
    octx.strokeStyle = CONTORNO
    octx.lineWidth = U * 2.6
    octx.stroke()
    octx.strokeStyle = SATIN[2]
    octx.lineWidth = U * 1.3
    octx.stroke()

    # lazadas: dos rosadas hacia abajo, dos grandes hacia arriba y dos blancas por delante
    for lado in (-1, 1):
        def abajo(lado=lado):
            octx.moveTo(lado * 0.015, 0.005)
            octx.bezierCurveTo(lado * 0.06, 0.06, lado * 0.17, 0.09, lado * 0.19, 0.05)
            octx.bezierCurveTo(lado * 0.2, 0.02, lado * 0.12, 0, lado * 0.015, -0.005)
        forma(abajo, satin(0, 0, lado * 0.19, 0.06))
    for lado in (-1, 1):
        def arriba(lado=lado):
            octx.moveTo(lado * 0.015, -0.005)
            octx.bezierCurveTo(lado * 0.05, -0.13, lado * 0.24, -0.15, lado * 0.22, -0.05)
            octx.bezierCurveTo(lado * 0.2, 0, lado * 0.1, 0.01, lado * 0.015, 0.005)
        forma(arriba, satin(0, 0, lado * 0.22, -0.1))
        octx.fillStyle = CINTA[0]  # el hueco de la lazada, alargado hacia la punta
        octx.beginPath()
        octx.ellipse(lado * 0.15, -0.07, 0.045, 0.01, -lado * 0.35, 0, TAU)
        octx.fill()
    for lado in (-1, 1):
        def blanca(lado=lado):  # más alta y angosta, abierta hacia arriba y afuera
            octx.moveTo(lado * 0.012, -0.01)
            octx.bezierCurveTo(lado * 0.02, -0.12, lado * 0.12, -0.14, lado * 0.12, -0.07)
            octx.bezierCurveTo(lado * 0.12, -0.02, lado * 0.06, -0.005, lado * 0.012, 0.004)
        forma(blanca, gradiente(0, 0, lado * 0.12, -0.1, SATIN[0], SATIN[1], SATIN[2], SATIN[1]))
        octx.strokeStyle = SATIN[0]  # un pliegue del satín
        octx.lineWidth = U * 1.0
        octx.beginPath()
        octx.moveTo(lado * 0.03, -0.02)
        octx.quadraticCurveTo(lado * 0.05, -0.08, lado * 0.09, -0.1)
        octx.stroke()

    # nudo con una perla que destella
    def nudo():
        octx.ellipse(0, 0, 0.032, 0.027, 0, 0, TAU)
    forma(nudo, satin(-0.032, -0.027, 0.032, 0.027))
    g = octx.createRadialGradient(-0.005, -0.006, 0, 0, 0, 0.018)
    g.addColorStop(0, "rgb(255,255,255)")
    g.addColorStop(1, css([214, 200, 210]))
    octx.fillStyle = g
    octx.beginPath()
    octx.arc(0, 0, 0.018, 0, TAU)
    octx.fill()
    tw = 1 if RM else 0.5 + 0.5 * math.sin(T * 2.6)
    destello(-0.006, -0.008, 0.03 * (0.5 + 0.5 * tw), 0.5 + 0.5 * tw)
    octx.restore()


# ---------- mensaje ----------
FUENTE_MSG = '"Pacifico", cursive'
# tonos del texto a lo largo de cada línea (posición 0–1, color), en la paleta del ramo
TONO_1 = ((0, [232, 110, 150]), (0.5, [252, 214, 224]), (1, [232, 110, 150]))
TONO_2 = ((0, [230, 66, 128]), (0.5, [246, 140, 176]), (1, [230, 66, 128]))
T_MSG = max(o.bloomStart + o.bloomDur for o in lilies) + 0.3  # el ramo ya terminó de abrir
medidor = document.createElement("canvas").getContext("2d")  # solo para medir texto
document.fonts.load(f"40px {FUENTE_MSG}")  # por si la página todavía no la pidió
fuente_lista = False
msg = None
BARRA = 54  # alto de la barra inferior, en px

# dedicatoria: se escribe a máquina bajo el título cuando este termina, hecha de caracteres
# como todo lo demás; la posición la decide componer()
DEDICATORIA = ("Que siempre encuentres sol para florecer.", "Gracias por ser tú <3")
PASO, PAUSA_FRASE = 0.055, 0.5  # s por letra y pausa entre las dos frases
TONO_DED = tuple(css(c) for _, c in TONO_1)  # los mismos tonos de "Feliz Cumpleaños"
TONO_CURSOR = css(TONO_2[0][1])  # el rosa de "Dani"
# Letra de píxeles: cada "#" es una celda, o sea un carácter del paso ASCII. A un tamaño que
# quepa en un celular, una letra lisa como la del título se vuelve una mancha; esta cae justo
# en las celdas, y es la más baja que se alcanza a leer. Filas de arriba abajo: 0-1 mayúsculas
# y astas, 2-4 el cuerpo de las minúsculas, 5 las descendentes. Un carácter que no esté aquí
# queda como espacio.
PIXELES = {
    " ": ".. ", ".": ". . . . #", ",": ". . . . # #", ":": ". . # . #", "-": "... ... ... ###",
    "<": "..# .#. #.. .#. ..#", ">": "#.. .#. ..# .#. #..",
    "0": ".#. #.# #.# #.# .#.", "1": ".#. ##. .#. .#. ###", "2": "##. ..# .#. #.. ###", "3": "##. ..# .#. ..# ##.",
    "4": "#.# #.# ### ..# ..#", "5": "### #.. ##. ..# ##.", "6": ".## #.. ##. #.# .#.", "7": "### ..# .#. .#. .#.",
    "8": ".#. #.# .#. #.# .#.", "9": ".#. #.# .## ..# ##.",
    "'": "# #", "!": "# # # . #", "¡": "# . # # #", "?": "##. ..# .#. ... .#.", "¿": ".#. ... .#. #.. .##",
    "a": "... ... .## #.# .##", "b": "#.. #.. ##. #.# ##.", "c": "... ... .## #.. .##", "d": "..# ..# .## #.# .##",
    "e": "... ... .#. ### .##", "f": ".## #.. ##. #.. #..", "g": "... ... .## #.# .## ##.", "h": "#.. #.. ##. #.# #.#",
    "i": "# . # # #", "j": ".# .. .# .# .# #.", "k": "#.. #.. #.# ##. #.#", "l": "#. #. #. #. .#",
    "m": "..... ..... ####. #.#.# #.#.#", "n": "... ... ##. #.# #.#", "o": "... ... .#. #.# .#.",
    "p": "... ... ##. #.# ##. #..", "q": "... ... .## #.# .## ..#", "r": "... ... #.# ##. #..",
    "s": "... ... .## .#. ##.", "t": "... .#. ### .#. .##", "u": "... ... #.# #.# .##", "v": "... ... #.# #.# .#.",
    "w": "..... ..... #...# #.#.# .#.#.", "x": "... ... #.# .#. #.#", "y": "... ... #.# #.# .## ##.",
    "z": "... ... ##. .#. .##",
    "á": "..# .#. .## #.# .##", "é": "..# .#. .#. ### .##", "í": ".# .. #. #. #.", "ó": "..# .#. .#. #.# .#.",
    "ú": "..# .#. #.# #.# .##", "ü": "#.# ... #.# #.# .##", "ñ": "### ... ##. #.# #.#",
    "A": ".#. #.# ### #.# #.#", "B": "##. #.# ##. #.# ##.", "C": ".## #.. #.. #.. .##", "D": "##. #.# #.# #.# ##.",
    "E": "### #.. ##. #.. ###", "F": "### #.. ##. #.. #..", "G": ".## #.. #.# #.# .##", "H": "#.# #.# ### #.# #.#",
    "I": "### .#. .#. .#. ###", "J": "..# ..# ..# #.# .#.", "K": "#.# #.# ##. #.# #.#", "L": "#.. #.. #.. #.. ###",
    "M": "#...# ##.## #.#.# #...# #...#", "N": "#..# ##.# #.## #..# #..#", "O": ".#. #.# #.# #.# .#.",
    "P": "##. #.# ##. #.. #..", "Q": ".#. #.# #.# #.# .##", "R": "##. #.# ##. #.# #.#", "S": ".## #.. .#. ..# ##.",
    "T": "### .#. .#. .#. .#.", "U": "#.# #.# #.# #.# ###", "V": "#.# #.# #.# #.# .#.",
    "W": "#...# #...# #.#.# #.#.# .#.#.", "X": "#.# #.# .#. #.# #.#", "Y": "#.# #.# .#. .#. .#.",
    "Z": "### ..# .#. #.. ###",
}
ALTO_LETRA, FILAS_LINEA = 6, 7  # filas de una letra y de una línea a la siguiente
CURSOR = (2, 5)  # ancho y alto del cursor, en celdas


def _letra(dibujo):
    # ancho en celdas y tramos encendidos (columna, fila, largo) de un dibujo de PIXELES
    filas = dibujo.split()
    tramos = []
    for y, fila in enumerate(filas):
        x = 0
        for c, grupo in groupby(fila):
            n = len(list(grupo))
            if c == "#":
                tramos.append((x, y, n))
            x += n
    return len(filas[0]), tramos


LETRAS = {c: _letra(d) for c, d in PIXELES.items()}


def letra(c):
    return LETRAS.get(c, LETRAS[" "])


def celdas_texto(texto):
    # ancho en celdas de un texto en la letra de píxeles, con una celda entre letras
    return sum(letra(c)[0] + 1 for c in texto) - 1


def ancho_texto(texto, px):
    medidor.font = f"{px}px {FUENTE_MSG}"
    return medidor.measureText(texto).width


def bajo_centro(texto):
    # cuánto baja la tinta del texto bajo el centro de su fila (textBaseline "middle"), por px
    medidor.font = f"100px {FUENTE_MSG}"
    medidor.textBaseline = "middle"
    return medidor.measureText(texto).actualBoundingBoxDescent / 100


def envolver(palabras, ancho):
    # palabra por palabra, mientras la línea quepa en `ancho` celdas
    lineas = []
    for p in palabras:
        if lineas and celdas_texto(f"{lineas[-1]} {p}") <= ancho:
            lineas[-1] += " " + p
        else:
            lineas.append(p)
    return lineas


def partir(frase, ancho):
    # en líneas que quepan en `ancho` celdas y, como text-wrap: balance, lo más parejas posible
    palabras = frase.split()
    lineas = envolver(palabras, ancho)
    while len(lineas) > 1:
        otras = envolver(palabras, max(map(celdas_texto, lineas)) - 1)
        if len(otras) > len(lineas):
            break
        lineas = otras
    return lineas


def dedicatoria_en(ancho):
    # el texto en líneas que quepan en `ancho` px (sin contar el cursor), su alto y cuándo
    # aparece cada letra y cada salto de línea (s desde que empieza)
    lineas, tiempos, t = [], [], 0.4  # el cursor aparece un instante antes de la primera letra
    for frase in DEDICATORIA:
        for j, linea in enumerate(partir(frase, int(ancho / cw) - CURSOR[0] - 1)):
            if lineas:  # entre frases, una pausa
                t += PAUSA_FRASE if j == 0 else PASO
                tiempos.append(t)
            for _ in linea:
                t += PASO
                tiempos.append(t)
            lineas.append(linea)
    return Obj(gap=2 * ch, alto=((len(lineas) - 1) * FILAS_LINEA + ALTO_LETRA) * ch,
               lineas=lineas, texto="\n".join(lineas), tiempos=tiempos)


def sobra(linea, ded):
    # cuánto baja la dedicatoria más allá de la fila de la última línea del título
    texto, px, _, f = linea
    return max(0, ded.gap + ded.alto - (f / 2 - bajo_centro(texto)) * px)


def componer():
    # dónde va el mensaje y adónde se desplaza el ramo para dejarle sitio
    global msg
    u = {t: ancho_texto(t, 100) / 100 for t in ("Feliz Cumpleaños", "Feliz", "Cumpleaños", "Dani")}  # ancho por px
    if W >= H * 1.15:
        # horizontal: el ramo se corre a la izquierda y el mensaje queda a su derecha, en dos líneas
        hb, gap = 0.4 * S0, H * 0.04
        s1 = min(H * 0.095, (W * 0.94 - 2 * hb - gap) / u["Feliz Cumpleaños"])
        s2 = min(s1 * 2.4, s1 * u["Feliz Cumpleaños"] * 0.8 / u["Dani"])
        lineas = [("Feliz Cumpleaños", s1, TONO_1, 1.5), ("Dani", s2, TONO_2, 1.25)]  # …, alto de la fila
        tw = max(px * u[t] for t, px, _, _ in lineas)
        x0 = (W - (2 * hb + gap + tw)) / 2
        S1, bx1, by1 = S0, x0 + hb, BY0
        xc = x0 + 2 * hb + gap + tw / 2
        ded = dedicatoria_en(2 * min(gap + tw / 2, W - 16 - xc))  # sin pisar el ramo ni el borde
        alto = sum(px * f for _, px, _, f in lineas)
        y = BY0 - 0.6 * S0 - alto / 2  # centrado con el ramo
        y = min(y, H - BARRA - alto - sobra(lineas[-1], ded))  # sin que la dedicatoria llegue a la barra
    else:
        # vertical: el ramo sube (y se achica lo necesario) y el mensaje queda debajo, en tres
        # líneas para que la letra tenga altura suficiente en las celdas gruesas de un celular
        s1 = W * 0.9 / u["Cumpleaños"]
        s2 = min(s1 * 1.6, W * 0.66 / u["Dani"])
        lineas = [("Feliz", s1 * 1.2, TONO_1, 1.1), ("Cumpleaños", s1, TONO_1, 1.25), ("Dani", s2, TONO_2, 1.2)]
        ded = dedicatoria_en(W - 32)
        alto = sum(px * f for _, px, _, f in lineas) + sobra(lineas[-1], ded)  # con la dedicatoria debajo
        arriba, abajo = H * 0.03, BARRA  # margen superior y alto de la barra inferior
        libre = H - arriba - abajo
        S1 = min(S0, (libre - alto) / 1.21)
        by1 = arriba + (libre - 1.21 * S1 - alto) / 2 + 1.15 * S1
        bx1 = xc = W / 2
        y = by1 + 0.06 * S1
    # cada línea: texto, tamaño, centro vertical, ancho, tonos, cuándo empieza y cuánto tarda en escribirse
    t = T_MSG + 0.9  # empieza mientras el ramo termina de moverse
    filas = []
    for texto, px, tonos, f in lineas:
        alto_fila = px * f
        dur = max(0.9, 0.45 + 0.085 * len(texto))
        filas.append(Obj(texto=texto, px=px, y=y + alto_fila / 2, w=px * u[texto], tonos=tonos, t=t, dur=dur))
        y += alto_fila
        t += dur * 0.9
    msg = Obj(S1=S1, bx1=bx1, by1=by1, xc=xc, filas=filas, fin=t + 0.3, ded=ded)
    # la dedicatoria, justo bajo la tinta de la última línea; empieza ya escrito el título. Cada
    # letra queda como tramos de celdas en su lugar (la línea, centrada como quedará completa,
    # no se corre mientras se escribe), y para cada cantidad de letras, dónde va el cursor
    ultima = filas[-1]
    ded.t = msg.fin + 0.4
    fila0 = js_round((ultima.y + bajo_centro(ultima.texto) * ultima.px + ded.gap) / ch)
    ded.letras, ded.cursor = [], []
    for i, linea in enumerate(ded.lineas):
        x, y = js_round(xc / cw - celdas_texto(linea) / 2), fila0 + i * FILAS_LINEA
        if i:
            ded.letras.append(())  # el salto de línea
        ded.cursor.append((x, y))
        for c in linea:
            ancho, tramos = letra(c)
            ded.letras.append([(x + tx, y + ty, n) for tx, ty, n in tramos])
            x += ancho + 1
            ded.cursor.append((x, y))
    ancho = max(map(celdas_texto, ded.lineas))
    ded.borde = (js_round(xc / cw - ancho / 2), js_round(xc / cw + ancho / 2))  # el degradado va de borde a borde


def vigilar_fuente():
    # el mensaje espera a que la fuente esté cargada (y entonces se mide con la de verdad)
    global fuente_lista
    if not fuente_lista and document.fonts.check(f"40px {FUENTE_MSG}"):
        fuente_lista = True
        componer()


def paso_ramo(T):
    # 0 → ramo centrado como al principio; 1 → ya le hizo sitio al mensaje
    if not fuente_lista:
        return 0
    return 1 if RM else ease((T - T_MSG) / 1.3)


def paradas(tonos, p, brillo):
    # paradas del degradado de una línea: su color, el brillo que la recorre y el borde
    # de la escritura (lo que está a la derecha de p todavía no se ve)
    pos = {o for o, _ in tonos} | {p, max(0, p - 0.06)}
    if brillo is not None:
        pos |= {b for b in (brillo - 0.07, brillo, brillo + 0.07) if 0 < b < 1}
    out = []
    for o in sorted(x for x in pos if 0 <= x <= 1):
        i = next(j for j in range(1, len(tonos)) if o <= tonos[j][0])
        (oa, ca), (ob, cb) = tonos[i - 1], tonos[i]
        c = mix(ca, cb, (o - oa) / (ob - oa))
        if brillo is not None:
            c = mix(c, [255, 250, 252], 0.85 * max(0, 1 - abs(o - brillo) / 0.07))
        alfa = 1 if o <= p - 0.06 else max(0, (p - o) / 0.06)
        out.append((o, c, alfa))
    return out


def draw_text(texto, px, xc, y, w, tonos, p, brillo):
    # una línea en letra caligráfica (en px de pantalla), escrita hasta la fracción p
    x0, x1 = xc - w / 2 - px * 0.3, xc + w / 2 + px * 0.3  # margen para los rasgos de la letra
    g = octx.createLinearGradient(x0, 0, x1, 0)
    for o, c, alfa in paradas(tonos, p, brillo):
        g.addColorStop(o, css(c, alfa))
    octx.font = f"{px:.2f}px {FUENTE_MSG}"
    octx.textAlign = "center"
    octx.textBaseline = "middle"
    octx.fillStyle = g
    octx.fillText(texto, xc, y)
    if 0 < p < 1:  # destello de la "pluma" en la punta de lo escrito
        x, r = x0 + (x1 - x0) * p, px * 0.3
        d = octx.createRadialGradient(x, y, 0, x, y, r)
        d.addColorStop(0, "rgba(255,250,252,0.95)")
        d.addColorStop(1, "rgba(255,250,252,0)")
        octx.fillStyle = d
        octx.beginPath()
        octx.arc(x, y, r, 0, TAU)
        octx.fill()


def draw_message(T):
    # las líneas se escriben una tras otra, de izquierda a derecha; ya completas, cada 4,5 s
    # las recorre un brillo
    if not fuente_lista or (T < msg.filas[0].t and not RM):
        return
    brillo = ((T - msg.fin) / 4.5) % 1 * 1.6 - 0.3 if T > msg.fin and not RM else None
    octx.setTransform(1 / cw, 0, 0, 1 / ch, 0, 0)  # de aquí en adelante, px de pantalla
    for f in msg.filas:
        p = 1 if RM else clamp((T - f.t) / f.dur)
        if p > 0:
            draw_text(f.texto, f.px, msg.xc, f.y, f.w, f.tonos, p, brillo)
    draw_dedicatoria(T)


def draw_dedicatoria(T):
    # a máquina, letra por letra, con un cursor de bloque al final (como el de "preparando tu
    # regalo▌"): fijo mientras escribe y parpadeando mientras espera
    d = msg.ded
    if T < d.t and not RM:
        return
    n = len(d.texto) if RM else bisect_right(d.tiempos, T - d.t)
    octx.setTransform(1, 0, 0, 1, 0, 0)  # en celdas
    octx.fillStyle = gradiente(d.borde[0], 0, d.borde[1], 0, *TONO_DED)
    octx.beginPath()
    for tramos in d.letras[:n]:
        for x, y, largo in tramos:
            octx.rect(x, y, largo, 1)
    octx.fill()
    if RM or 0 < n < len(d.texto) or (T - d.t) % 1 < 0.5:
        octx.fillStyle = TONO_CURSOR
        octx.beginPath()
        octx.rect(*d.cursor[n], *CURSOR)
        octx.fill()


# ---------- portada: el regalo ----------
# Se dibuja con el mismo motor que el ramo (formas → caracteres). Las medidas van en unidades
# de G (el ancho del frente de la caja), con el centro de la caja en el origen. El recuadro
# #escena del HTML marca dónde va todo, así la captura que se ve mientras carga Python
# (public/portada.webp) calza con esta versión en vivo.
D_ABRIR = 0 if RM else 1.6  # cuánto dura la apertura, en s
PAPEL = (css([236, 120, 160]), css([196, 70, 120]))  # frente: arriba, abajo
PAPEL_TAPA = (css([244, 136, 172]), css([222, 96, 142]))
PAPEL_LADO = (css([168, 52, 98]), css([136, 38, 82]))
PAPEL_ARRIBA = css([250, 168, 196])
LUNAR = css([252, 232, 238])
SATIN = (css([176, 168, 184]), css([232, 226, 236]), css([255, 252, 255]))  # cinta blanca: sombra, base, brillo
CARTON = css([246, 236, 214])  # la etiqueta
TONO_PORTADA = ((0, [236, 112, 156]), (0.5, [252, 206, 222]), (1, [236, 112, 156]))
# destellos alrededor: x, y, tamaño, fase (unidades de G)
DESTELLOS = ((-0.72, -0.5, 0.06, 0.0), (0.76, -0.56, 0.05, 1.7), (-0.76, 0.12, 0.045, 3.1), (0.78, 0.2, 0.055, 4.4),
             (-0.26, -0.66, 0.035, 2.3), (0.5, -0.66, 0.04, 5.2), (-0.62, 0.42, 0.03, 0.9), (0.7, -0.14, 0.03, 3.8))
# brillitos que suben flotando por los costados: x, tamaño, fase
BRILLITOS = ((-0.7, 0.05, 0.0), (0.74, 0.045, 0.35), (-0.64, 0.04, 0.62), (0.7, 0.055, 0.8),
             (-0.78, 0.035, 0.18), (0.8, 0.035, 0.52))
escena = document.getElementById("escena")
portada = None


def medir_portada():
    # el recuadro #escena mide 1.7 G de ancho; la caja va centrada arriba y el nombre debajo
    global portada
    r = escena.getBoundingClientRect()
    G = r.width / 1.7
    portada = Obj(G=G, cx=r.left + 0.85 * G, cy=r.top + 0.7 * G, w=ancho_texto("Para Dani", 100) / 100 * 0.36 * G)


def gradiente(x0, y0, x1, y1, *colores):
    g = octx.createLinearGradient(x0, y0, x1, y1)
    for i, c in enumerate(colores):
        g.addColorStop(i / (len(colores) - 1), c)
    return g


def poligono(*puntos):
    def trazar():
        octx.moveTo(*puntos[0])
        for p in puntos[1:]:
            octx.lineTo(*p)
    return trazar


def lunares(x0, y0, x1, y1, desde):
    # lunares blancos sobre el papel, recortados a la cara (x0, y0)–(x1, y1)
    octx.save()
    octx.beginPath()
    octx.rect(x0, y0, x1 - x0, y1 - y0)
    octx.clip()
    octx.fillStyle = LUNAR
    octx.beginPath()
    for fila, y in enumerate(desde):
        x = x0 + 0.04 + (0.075 if fila % 2 else 0)
        while x < x1:
            octx.moveTo(x + 0.028, y)
            octx.arc(x, y, 0.028, 0, TAU)
            x += 0.15
    octx.fill()
    octx.restore()


def corazon(x, y, h, color):
    octx.fillStyle = color
    octx.beginPath()
    octx.moveTo(x, y + 0.45 * h)
    octx.bezierCurveTo(x - 0.75 * h, y - 0.05 * h, x - 0.4 * h, y - 0.7 * h, x, y - 0.25 * h)
    octx.bezierCurveTo(x + 0.4 * h, y - 0.7 * h, x + 0.75 * h, y - 0.05 * h, x, y + 0.45 * h)
    octx.fill()


def destello(x, y, s, alfa):
    # estrella de cuatro puntas con un halo suave
    g = octx.createRadialGradient(x, y, 0, x, y, s * 1.6)
    g.addColorStop(0, f"rgba(255,236,244,{num(0.35 * alfa)})")
    g.addColorStop(1, "rgba(255,236,244,0)")
    octx.fillStyle = g
    octx.beginPath()
    octx.arc(x, y, s * 1.6, 0, TAU)
    octx.fill()
    k = s * 0.18
    octx.fillStyle = f"rgba(255,248,252,{num(alfa)})"
    octx.beginPath()
    octx.moveTo(x, y - s)
    for px, py in ((k, -k), (s, 0), (k, k), (0, s), (-k, k), (-s, 0), (-k, -k)):
        octx.lineTo(x + px, y + py)
    octx.fill()


def caja(Ug):
    # cuerpo: cara lateral (en sombra), frente con lunares y las cintas que lo rodean
    forma(poligono((0.5, -0.12), (0.6, -0.19), (0.6, 0.43), (0.5, 0.5)), gradiente(0, -0.19, 0, 0.5, *PAPEL_LADO), Ug)
    forma(poligono((-0.5, -0.12), (0.5, -0.12), (0.5, 0.5), (-0.5, 0.5)), gradiente(0, -0.12, 0, 0.5, *PAPEL), Ug)
    lunares(-0.5, -0.12, 0.5, 0.5, (-0.06, 0.05, 0.16, 0.27, 0.38, 0.49))
    forma(poligono((-0.07, -0.12), (0.07, -0.12), (0.07, 0.5), (-0.07, 0.5)),
          gradiente(-0.07, 0, 0.07, 0, SATIN[0], SATIN[2], SATIN[1], SATIN[0]), Ug)
    forma(poligono((0.535, -0.145), (0.565, -0.166), (0.565, 0.454), (0.535, 0.475)), SATIN[0], Ug)


def tapa(Ug):
    # tapa: lateral, frente con lunares, cara de arriba y las cintas cruzadas encima
    forma(poligono((0.55, -0.3), (0.65, -0.37), (0.65, -0.19), (0.55, -0.12)), gradiente(0, -0.37, 0, -0.12, *PAPEL_LADO), Ug)
    forma(poligono((-0.55, -0.3), (0.55, -0.3), (0.55, -0.12), (-0.55, -0.12)), gradiente(0, -0.3, 0, -0.12, *PAPEL_TAPA), Ug)
    lunares(-0.55, -0.3, 0.55, -0.12, (-0.21,))
    forma(poligono((-0.55, -0.3), (-0.45, -0.37), (0.65, -0.37), (0.55, -0.3)), PAPEL_ARRIBA, Ug)
    forma(poligono((-0.075, -0.3), (0.075, -0.3), (0.075, -0.12), (-0.075, -0.12)),
          gradiente(-0.075, 0, 0.075, 0, SATIN[0], SATIN[2], SATIN[1], SATIN[0]), Ug)
    forma(poligono((0.585, -0.323), (0.615, -0.344), (0.615, -0.164), (0.585, -0.143)), SATIN[0], Ug)
    forma(poligono((-0.5, -0.325), (0.6, -0.325), (0.62, -0.345), (-0.48, -0.345)), SATIN[1], Ug)
    forma(poligono((-0.075, -0.3), (0.025, -0.37), (0.175, -0.37), (0.075, -0.3)), SATIN[1], Ug)


def mono(tc, Ug):
    # moño de satín: dos puntas que caen sobre la tapa, dos lazadas que se mecen y el nudo
    kx, ky = 0.05, -0.355
    for lado in (-1, 1):
        def punta(lado=lado):
            octx.moveTo(kx + lado * 0.01, ky + 0.01)
            octx.bezierCurveTo(kx + lado * 0.06, ky + 0.12, kx + lado * 0.14, ky + 0.2, kx + lado * 0.2, ky + 0.3)
            octx.lineTo(kx + lado * 0.24, ky + 0.24)
            octx.lineTo(kx + lado * 0.29, ky + 0.29)
            octx.bezierCurveTo(kx + lado * 0.22, ky + 0.16, kx + lado * 0.14, ky + 0.06, kx + lado * 0.06, ky - 0.005)
        forma(punta, gradiente(kx, ky, kx + lado * 0.25, ky + 0.3, SATIN[2], SATIN[1], SATIN[0]), Ug)
    for lado in (-1, 1):
        octx.save()
        octx.translate(kx, ky)
        octx.rotate(0 if RM else lado * 0.05 * math.sin(tc * 2.2))

        def lazada(lado=lado):
            octx.moveTo(lado * 0.02, -0.005)
            octx.bezierCurveTo(lado * 0.08, -0.3, lado * 0.4, -0.3, lado * 0.36, -0.1)
            octx.bezierCurveTo(lado * 0.33, 0.02, lado * 0.15, 0.03, lado * 0.02, 0.015)
        forma(lazada, gradiente(0, 0, lado * 0.36, -0.2, SATIN[0], SATIN[1], SATIN[2], SATIN[1]), Ug)
        octx.fillStyle = SATIN[0]  # el hueco de la lazada
        octx.beginPath()
        octx.ellipse(lado * 0.22, -0.12, 0.07, 0.035, -lado * 0.45, 0, TAU)
        octx.fill()
        octx.restore()

    def nudo():
        octx.ellipse(kx, ky, 0.065, 0.05, 0, 0, TAU)
    forma(nudo, gradiente(kx - 0.065, ky - 0.05, kx + 0.065, ky + 0.05, SATIN[2], SATIN[1], SATIN[0]), Ug)


def etiqueta(tc, Ug):
    # etiqueta colgando del moño, con un corazón
    giro = 0.3 + (0 if RM else 0.06 * math.sin(tc * 1.8))
    ex, ey = 0.44, 0.04
    hx, hy = ex - 0.09 * math.cos(giro), ey - 0.09 * math.sin(giro)  # donde se ata el hilo
    octx.strokeStyle = CARTON
    octx.lineWidth = Ug * 0.9
    octx.beginPath()
    octx.moveTo(0.09, -0.34)
    octx.quadraticCurveTo(0.3, -0.25, hx, hy)
    octx.stroke()
    octx.save()
    octx.translate(ex, ey)
    octx.rotate(giro)
    forma(poligono((-0.09, 0), (-0.06, -0.05), (0.09, -0.05), (0.09, 0.05), (-0.06, 0.05)), CARTON, Ug)
    octx.fillStyle = SATIN[0]
    octx.beginPath()
    octx.arc(-0.058, 0, 0.012, 0, TAU)
    octx.fill()
    corazon(0.02, 0.002, 0.07, css([226, 62, 122]))
    octx.restore()


def luz_interior(tc, A):
    # al abrir: rayos y chispas que salen de la caja
    luz = smooth(0.12, 0.35, A) * (1 - smooth(0.7, 1, A))
    if luz <= 0:
        return
    ox, oy = 0.05, -0.15
    largo = 0.3 + 2.4 * ease((A - 0.1) / 0.7)
    g = octx.createRadialGradient(ox, oy, 0, ox, oy, largo)
    g.addColorStop(0, f"rgba(255,246,250,{num(0.75 * luz)})")
    g.addColorStop(0.45, f"rgba(255,206,226,{num(0.32 * luz)})")
    g.addColorStop(1, "rgba(255,190,220,0)")
    octx.fillStyle = g
    octx.beginPath()
    for i in range(13):  # rayos finos, alternando largos y cortos
        a = -math.pi / 2 + (i / 12 - 0.5) * 2.4 + 0.12 * math.sin(tc * 0.8)
        d = 0.03 + 0.015 * (i % 2)
        L = largo * (1 if i % 2 else 0.72)
        octx.moveTo(ox, oy)
        octx.lineTo(ox + math.cos(a - d) * L, oy + math.sin(a - d) * L)
        octx.lineTo(ox + math.cos(a + d) * L, oy + math.sin(a + d) * L)
    octx.fill()
    r = 0.08 + 0.22 * ease((A - 0.1) / 0.5)  # el núcleo de luz, que se desvanece hacia afuera
    g = octx.createRadialGradient(ox, oy, 0, ox, oy, r)
    g.addColorStop(0, f"rgba(255,250,252,{num(0.9 * luz)})")
    g.addColorStop(0.4, f"rgba(255,222,236,{num(0.45 * luz)})")
    g.addColorStop(1, "rgba(255,210,230,0)")
    octx.fillStyle = g
    octx.beginPath()
    octx.arc(ox, oy, r, 0, TAU)
    octx.fill()
    r = mulberry32(11)
    vuelo = ease((A - 0.15) / 0.7)
    for i in range(16):
        a = -math.pi / 2 + (r() - 0.5) * 2.6
        dist = 0.15 + (0.9 + r() * 0.9) * vuelo
        destello(ox + math.cos(a) * dist, oy + math.sin(a) * dist, 0.025 + r() * 0.03, luz)


def draw_cover(tc, A):
    # la caja de regalo flotando con su nombre; con A > 0 se abre (A = 1: ya no está)
    G, cx, cy = portada.G, portada.cx, portada.cy
    Ug = cw / G  # una celda, en unidades de G
    sube = 0 if RM else math.sin(tc * 1.5) * 0.012  # flota
    desvanece = 1 - smooth(0.55, 0.95, A)
    octx.setTransform(G / cw, 0, 0, G / ch, cx / cw, cy / ch)

    # resplandor rosado detrás, reflejo bajo la caja (más chico cuando sube) y brillitos que suben
    g = octx.createRadialGradient(0.05, 0.05, 0, 0.05, 0.05, 0.95)
    g.addColorStop(0, f"rgba(240,120,170,{num(0.2 * desvanece)})")
    g.addColorStop(1, "rgba(240,120,170,0)")
    octx.fillStyle = g
    octx.beginPath()
    octx.arc(0.05, 0.05, 0.95, 0, TAU)
    octx.fill()
    octx.save()
    octx.translate(0.05, 0.57)
    octx.scale(1 - sube * 6, 0.12)
    g = octx.createRadialGradient(0, 0, 0, 0, 0, 0.55)
    g.addColorStop(0, f"rgba(250,150,190,{num(0.45 * desvanece)})")
    g.addColorStop(1, "rgba(250,150,190,0)")
    octx.fillStyle = g
    octx.beginPath()
    octx.arc(0, 0, 0.55, 0, TAU)
    octx.fill()
    octx.restore()
    for x, s, fase in BRILLITOS:
        vida = 0.5 if RM else (tc * 0.11 + fase) % 1
        tw = 1 if RM else 0.5 + 0.5 * math.sin(tc * 3.1 + fase * 7)  # titilan mientras suben
        destello(x + 0.04 * math.sin(vida * 6 + fase * 9), 0.45 - vida * 1.05, s * (0.5 + 0.5 * tw),
                 desvanece * math.sin(vida * math.pi) * (0.4 + 0.6 * tw))

    # la caja: al abrir se "aprieta" un instante, y la tapa sale volando con el moño y la etiqueta
    octx.globalAlpha = desvanece
    octx.translate(0, sube)
    octx.save()
    octx.translate(0, 0.5)
    octx.scale(1, 1 - 0.07 * math.sin(clamp(A / 0.22) * math.pi))
    octx.translate(0, -0.5)
    caja(Ug)
    octx.restore()
    vuela = ease((A - 0.15) / 0.6)
    octx.save()
    octx.translate(0.05 + 0.25 * vuela, -1.3 * vuela)
    octx.rotate(-0.55 * vuela)
    octx.translate(-0.05, 0)
    octx.globalAlpha = desvanece * (1 - smooth(0.45, 0.8, A))
    tapa(Ug)
    mono(tc, Ug)
    etiqueta(tc, Ug)
    octx.restore()
    octx.globalAlpha = 1
    luz_interior(tc, A)

    # destellos que titilan alrededor
    for x, y, s, fase in DESTELLOS:
        brillo = 1 if RM else 0.5 + 0.5 * math.sin(tc * 1.9 + fase)
        destello(x, y, s * (0.35 + 0.65 * brillo), (0.3 + 0.7 * brillo) * desvanece)

    # el nombre, hecho de caracteres como el mensaje del final
    if fuente_lista:
        octx.setTransform(1 / cw, 0, 0, 1 / ch, 0, 0)
        octx.globalAlpha = desvanece
        brillo = None if RM else (tc / 4.5) % 1 * 1.6 - 0.3
        draw_text("Para Dani", 0.36 * G, cx, cy + 0.85 * G, portada.w, TONO_PORTADA, 1, brillo)
        octx.globalAlpha = 1


def draw_scene(T, tc=0, A=1):
    # T: tiempo del ramo (negativo = todavía no empieza); tc: reloj de la portada; A: apertura del regalo
    octx.setTransform(1, 0, 0, 1, 0, 0)
    octx.clearRect(0, 0, cols, rows)
    if T >= 0:
        draw_bouquet(T)
    if A < 1:
        draw_cover(tc, A)


def draw_bouquet(T):
    global S, bx, by, U
    k = paso_ramo(T)
    S = S0 + (msg.S1 - S0) * k
    bx = BX0 + (msg.bx1 - BX0) * k
    by = BY0 + (msg.by1 - BY0) * k
    U = cw / S
    octx.setTransform(S / cw, 0, 0, S / ch, bx / cw, by / ch)

    # estado de cada tallo (con vaivén leve)
    for o in all_stems:
        o.prog = ease((T - o.stemStart) / o.stemDur)
        sw = 0 if RM else o.prog
        dx = math.sin(T * 0.9 + o.ph) * 0.006 * sw
        dy = math.cos(T * 0.7 + o.ph) * 0.002 * sw
        o.cur = Obj(p0=o.stem.p0, p1=[o.stem.p1[0] + dx * 0.4, o.stem.p1[1]], p2=[o.stem.p2[0] + dx, o.stem.p2[1] + dy])
    for o in all_stems:
        draw_stem(o, o.prog)
    for o in all_stems:
        for lf in o.leaves:
            sc = ease((o.prog - lf.t) / 0.35)
            if sc <= 0:
                continue
            x, y = q_at(o.cur, lf.t)
            ang = q_tan(o.cur, lf.t) + lf.side * lf.off
            draw_leaf(x, y, ang, lf.L, lf.w, (1 if math.cos(ang) >= 0 else -1) * lf.bendK, sc)
    for lf in base_leaves:
        draw_leaf(lf.x, lf.y, lf.ang, lf.L, lf.w, lf.bend, ease((T - lf.start) / lf.dur))
    draw_ribbon(T)
    for o in all_stems:
        g = ease((T - o.budStart) / o.budDur)
        e = clamp((T - o.bloomStart) / o.bloomDur)
        draw_lily(o, o.cur.p2[0], o.cur.p2[1], q_tan(o.cur, 1), g, e)
    draw_message(T)


# ---------- paso ASCII ----------
# los 32.768 colores posibles de un glifo (5 bits por canal), ya escritos como CSS
STYLES = np.array([f"rgb({(k >> 10) << 3},{((k >> 5) & 31) << 3},{(k & 31) << 3})" for k in range(1 << 15)], dtype=object)


def ascii_pass(T):
    octx.getImageData(0, 0, cols, rows).data.assign_to(img)
    alpha = img[3::4]
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0)
    ctx.fillStyle = "#050507"
    ctx.fillRect(0, 0, W, H)
    ctx.font = f"{fs}px {FONT}"
    ctx.textBaseline = "top"

    # halo de color suave bajo los glifos: da cuerpo a las formas
    ctx.globalAlpha = 0.2
    ctx.imageSmoothingEnabled = True
    ctx.drawImage(off, 0, 0, cols * cw, rows * ch)
    ctx.globalAlpha = 1

    # estrellas que titilan; se pintan junto con las celdas en una sola llamada a pintor.js
    # (cada glifo es [x, y, carácter, color], con el carácter como índice en RAMP)
    colores = []
    glifos = []
    for sx, sy, sph, ssp in stars:
        if alpha[sy * cols + sx] > 8:
            continue
        b = 0.5 + 0.5 * math.sin(T * ssp + sph)
        colores.append(f"rgba(232,226,212,{num(0.15 + b * 0.55)})")
        glifos.append((sx, sy, STAR_ON if b > 0.85 else STAR_OFF, len(colores) - 1))

    fseed = 0 if RM else math.floor(T * 8)
    last = len(RAMP) - 1
    # las cuentas por celda van vectorizadas con numpy; el dibujo sigue el mismo orden fila a fila
    cel = np.flatnonzero(alpha >= 10)
    celdas = np.empty((len(glifos) + cel.size, 4), np.int32)
    if glifos:
        celdas[: len(glifos)] = glifos
    if cel.size:
        ascii_cells(cel, fseed, last, celdas[len(glifos):], colores)
    buf = window.Int32Array.new(celdas.size)
    buf.assign(celdas)
    window.pintarGlifos(ctx._ctx, RAMP, "\n".join(colores), buf, cw, ch)


def ascii_cells(cel, fseed, last, out, colores):
    y = cel // cols
    x = cel - y * cols
    px = img.reshape(-1, 4)[cel].astype(np.float64)
    r, g, b, a = px[:, 0], px[:, 1], px[:, 2], px[:, 3]
    h = x * 374761393.0 + y * 668265263.0 + fseed * 1274126177.0
    h = np.fmod(np.trunc(h), 4294967296.0).astype(np.int64) & M32  # el "| 0" de JS
    h = ((h ^ (h >> 13)) * 1274126177) & M32
    n = ((h ^ (h >> 16)) & M32) / 4294967296
    lum = ((0.299 * r + 0.587 * g + 0.114 * b) / 255) * (a / 255)
    idx = np.clip(np.floor((0.32 + np.power(lum, 0.6) * 0.78 + (n - 0.5) * 0.14) * last), 1, last).astype(np.intp)
    # color reforzado (los glifos cubren solo parte de la celda)
    k = 1.45 * np.minimum(1, a / 170 + 0.15)
    R = np.minimum(255, r * k).astype(np.int64) >> 3
    G = np.minimum(255, g * k).astype(np.int64) >> 3
    B = np.minimum(255, b * k).astype(np.int64) >> 3
    key = (R << 10) | (G << 5) | B
    # el color solo se fija cuando cambia respecto a la celda anterior
    new = np.empty(key.size, bool)
    new[0] = True
    new[1:] = key[1:] != key[:-1]

    out[:, 0] = x
    out[:, 1] = y
    out[:, 2] = idx
    out[:, 3] = np.where(new, np.cumsum(new) - 1 + len(colores), -1)
    colores += STYLES[key[new]].tolist()


# ---------- música: "Cumpleaños feliz" en caja de música ----------
# Melodía tradicional (de dominio público), sintetizada aquí mismo con Web Audio: no hay archivos
# de audio. Notas y duraciones en pulsos; "Da-ni" cae en el pulso 16.
MELODIA = (("G4", .75), ("G4", .25), ("A4", 1), ("G4", 1), ("C5", 1), ("B4", 2),
           ("G4", .75), ("G4", .25), ("A4", 1), ("G4", 1), ("D5", 1), ("C5", 2),
           ("G4", .75), ("G4", .25), ("G5", 1), ("E5", 1), ("C5", 1), ("B4", 1), ("A4", 1),
           ("F5", .75), ("F5", .25), ("E5", 1), ("C5", 1), ("D5", 1), ("C5", 3))
PULSO_DANI = 16
# acompañamiento: bajo en el primer pulso de cada compás y el acorde arpegiado, suave
ACORDES = ((1, "C3", "G3", "E4"), (4, "G2", "F3", "B3"), (7, "G2", "F3", "B3"), (10, "C3", "G3", "E4"),
           (13, "C3", "G3", "E4"), (16, "F2", "C3", "A3"), (19, "C3", "G3"), (21, "G2"))
ACOMPANAMIENTO = [(p + i, nota, (0.42, 0.2, 0.2)[i]) for p, *notas in ACORDES for i, nota in enumerate(notas)]
ACOMPANAMIENTO += [(22, "C3", 0.45), (22.08, "G3", 0.3), (22.16, "E4", 0.3), (22.24, "C5", 0.3)]  # acorde final
# timbre de caja de música: múltiplo de la frecuencia, volumen y cuánto dura (fracción del decaimiento)
PARCIALES = ((1, 1.0, 1.0), (2, 0.28, 0.45), (3, 0.1, 0.22), (6.27, 0.05, 0.05))
SEMITONO = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
VOLUMEN = 0.32
boton_musica = document.getElementById("musica")
musica_on = True
ac = master = eco = pista = None


def frecuencia(nota):
    midi = 12 * (int(nota[1:]) + 1) + SEMITONO[nota[0]]
    return 440 * 2 ** ((midi - 69) / 12)


def iniciar_audio():
    # tiene que crearse durante el toque: los navegadores no dejan sonar nada antes
    global ac, master, eco
    Ctx = getattr(window, "AudioContext", None) or getattr(window, "webkitAudioContext", None)
    if Ctx is None:
        return
    sesion = getattr(window.navigator, "audioSession", None)
    if sesion is not None:  # iPhone (Safari 16.4+): que suene aunque el interruptor de silencio esté activado
        sesion.type = "playback"
    ac = Ctx.new()
    comp = ac.createDynamicsCompressor()
    comp.connect(ac.destination)
    master = ac.createGain()
    master.gain.value = VOLUMEN if musica_on else 0
    master.connect(comp)
    # eco suave: un retardo que se realimenta a través de un filtro (cada repetición más opaca)
    eco = ac.createDelay(1.0)
    eco.delayTime.value = 0.27
    filtro = ac.createBiquadFilter()
    filtro.type = "lowpass"
    filtro.frequency.value = 2400
    vuelta = ac.createGain()
    vuelta.gain.value = 0.33
    salida = ac.createGain()
    salida.gain.value = 0.3
    eco.connect(filtro)
    filtro.connect(vuelta)
    vuelta.connect(eco)
    eco.connect(salida)
    salida.connect(master)


def campanita(nota, t, vel, decae):
    f = frecuencia(nota)
    for mult, amp, frac in PARCIALES:
        fin = t + decae * frac
        osc = ac.createOscillator()
        osc.frequency.value = f * mult
        g = ac.createGain()
        g.gain.setValueAtTime(0, t)
        g.gain.linearRampToValueAtTime(vel * amp, t + 0.005)
        g.gain.exponentialRampToValueAtTime(0.0001, fin)
        osc.connect(g)
        g.connect(pista)
        osc.start(t)
        osc.stop(fin + 0.05)


def tocar_cancion(retraso=0):
    # la canción arranca junto con el ramo (`retraso` s desde ahora)
    global pista
    if ac is None:
        return
    ahora = ac.currentTime
    if pista is not None:  # al replantar, la versión anterior se apaga en un instante
        vieja = pista
        vieja.gain.setTargetAtTime(0, ahora, 0.04)
        window.setTimeout(create_once_callable(lambda: vieja.disconnect()), 600)
    pista = ac.createGain()
    pista.connect(master)
    pista.connect(eco)
    # el tempo se ajusta para que "Da-ni" suene justo cuando empieza a escribirse "Dani"
    pulso = msg.filas[-1].t / PULSO_DANI if not RM else 0.54
    inicio = ahora + retraso + 0.05
    p = 0
    for nota, dur in MELODIA:
        campanita(nota, inicio + p * pulso, 0.5, 1.8)
        p += dur
    for p, nota, vel in ACOMPANAMIENTO:
        campanita(nota, inicio + p * pulso, vel, 2.4)


def alternar_musica(*_):
    global musica_on
    musica_on = not musica_on
    boton_musica.setAttribute("aria-pressed", "true" if musica_on else "false")
    if master is not None:
        master.gain.setTargetAtTime(VOLUMEN if musica_on else 0, ac.currentTime, 0.08)


# ---------- ciclo ----------
# Antes de abrir se dibuja la portada (el regalo). Al tocarla se abre durante D_ABRIR s y
# después empieza el ramo (T = 0), junto con la canción.
intro = document.getElementById("intro")
boton_abrir = document.getElementById("abrir")
t0 = t_toque = 0
abierto = lista = False


def restart(*_, retraso=0):
    global t0
    ahora = window.performance.now()
    t0 = ahora - 9000 if RM else ahora + retraso * 1000
    tocar_cancion(retraso)


def frame(now):
    global lista
    vigilar_fuente()
    tc = now / 1000
    if abierto:
        A = 1 if RM else (now - t_toque) / 1000 / D_ABRIR
        T = (now - t0) / 1000
    else:
        A, T = 0, -1
    draw_scene(T, tc, A)
    ascii_pass(T if T >= 0 else tc)
    if not lista and fuente_lista:
        # la portada en vivo ya se ve como la captura: se funden y se puede abrir
        lista = True
        intro.classList.add("lista")
        boton_abrir.disabled = False
        boton_abrir.textContent = "toca para abrir ▸"
        boton_abrir.focus()
    window.requestAnimationFrame(frame_proxy)


def abrir(*_):
    # se abre el regalo: la tapa sale volando, suena una campanita y luego empieza el ramo
    global abierto, t_toque
    if abierto or not lista:
        return
    abierto = True
    t_toque = window.performance.now()
    intro.classList.add("abierto")
    document.body.classList.add("abierto")
    iniciar_audio()
    restart(retraso=D_ABRIR)
    if ac is not None:
        for i, nota in enumerate(("C6", "E6", "G6", "C7", "E7", "G7")):  # campanita al abrir
            campanita(nota, ac.currentTime + 0.12 + i * 0.07, 0.3, 1.6)


frame_proxy = create_proxy(frame)
restart_proxy = create_proxy(restart)
window.addEventListener("resize", create_proxy(resize))
cvs.addEventListener("click", restart_proxy)
document.getElementById("replay").addEventListener("click", restart_proxy)
boton_musica.addEventListener("click", create_proxy(alternar_musica))
intro.addEventListener("click", create_proxy(abrir))
resize()
window.requestAnimationFrame(frame_proxy)
