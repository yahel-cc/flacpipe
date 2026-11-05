#!/usr/bin/env python3
"""Pinta las capturas del banco de pruebas como imágenes, con sus colores.

conductor.py vuelca, junto a cada pantalla, un .json con el carácter y el
atributo de cada celda y los colores de cada par (los de init_pair, así que
salen del tema de verdad). Esto lo convierte en PNG, en una hoja con varios
cuadros seguidos o en un GIF animado:

    python3 pruebas/render.py captura.json                  # → captura.png
    python3 pruebas/render.py captura.json -o otra.png --fondo 300a24
    python3 pruebas/render.py --hoja cuadros/ -n 6 -o hoja.png
    python3 pruebas/render.py --gif cuadros/ -o demo.gif

Las líneas de recuadro, los bloques y el braille se dibujan a mano, como hace
la propia terminal (VTE no los saca de la fuente), para que se vean igual que
en pantalla. Necesita Pillow; no es una dependencia de flacpipe.
"""
import argparse
import glob
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

FUENTES = [
    "/usr/share/fonts/truetype/ubuntu/UbuntuSansMono[wght].ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
FUENTES_NEGRITA = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]
RESERVA = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
]

# Los 16 primeros cambian con cada terminal; éstos son los de xterm.
BASICOS = ["000000", "cd0000", "00cd00", "cdcd00", "0000ee", "cd00cd", "00cdcd", "e5e5e5",
           "7f7f7f", "ff0000", "00ff00", "ffff00", "5c5cff", "ff00ff", "00ffff", "ffffff"]


def rgb_hex(h):
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def xterm(n):
    if n < 16:
        return rgb_hex(BASICOS[n])
    if n < 232:
        n -= 16
        niveles = (0, 95, 135, 175, 215, 255)
        return (niveles[n // 36], niveles[n // 6 % 6], niveles[n % 6])
    g = 8 + (n - 232) * 10
    return (g, g, g)


def _cargar(rutas, tam):
    for ruta in rutas:
        if os.path.exists(ruta):
            try:
                return ImageFont.truetype(ruta, tam)
            except OSError:
                continue
    return ImageFont.load_default()


class Pintor:
    def __init__(self, tam=15, fondo="1b1d23", tinta="d0d0d0"):
        self.f = _cargar(FUENTES, tam)
        self.fb = _cargar(FUENTES_NEGRITA, tam)
        try:
            self.f.set_variation_by_name("Regular")
            self.fb = _cargar(FUENTES[:1], tam)
            self.fb.set_variation_by_name("Bold")
        except Exception:
            pass
        self.reservas = [_cargar([r], tam) for r in RESERVA if os.path.exists(r)]
        asc, desc = self.f.getmetrics()
        self.cw = round(self.f.getlength("M"))
        self.ch = asc + desc + 2
        self.asc = asc
        self.fondo = rgb_hex(fondo)
        self.tinta = rgb_hex(tinta)
        self._tiene = {}
        self._nada = self._mascara(self.f, "\U0010fffd")

    @staticmethod
    def _mascara(fuente, c):
        try:
            m = fuente.getmask(c)
            return (m.size, bytes(m))
        except Exception:
            return None

    def fuente_para(self, c, negrita):
        base = self.fb if negrita else self.f
        if ord(c) < 0x2000:
            return base
        clave = c
        if clave not in self._tiene:
            elegida = None
            for fuente in [self.f] + self.reservas:
                m = self._mascara(fuente, c)
                if m is not None and m != self._mascara(fuente, "\U0010fffd") and m[0][0] > 0:
                    elegida = fuente
                    break
            self._tiene[clave] = elegida or self.f
        elegida = self._tiene[clave]
        return base if elegida is self.f else elegida

    # ── dibujo a mano de lo que la terminal pinta sola ──
    def _bloque(self, d, x, y, c, color):
        w, h = self.cw, self.ch
        o = ord(c)
        if 0x2581 <= o <= 0x2588:                     # ▁ … █
            alto = max(1, h * (o - 0x2580) / 8)
            d.rectangle([x, y + h - alto, x + w - 1, y + h - 1], fill=color)
            return True
        if 0x2589 <= o <= 0x258F:                     # ▉ … ▏
            ancho = max(1, w * (0x2590 - o) / 8)
            d.rectangle([x, y, x + ancho - 1, y + h - 1], fill=color)
            return True
        if c == "▀":
            d.rectangle([x, y, x + w - 1, y + h / 2 - 1], fill=color)
            return True
        if c == "▐":
            d.rectangle([x + w / 2, y, x + w - 1, y + h - 1], fill=color)
            return True
        if c == "▔":
            d.rectangle([x, y, x + w - 1, y + h / 8 - 1], fill=color)
            return True
        if c == "▕":
            d.rectangle([x + w * 7 / 8, y, x + w - 1, y + h - 1], fill=color)
            return True
        if c in "░▒▓":
            densidad = {"░": 0.25, "▒": 0.5, "▓": 0.75}[c]
            mezcla = tuple(int(a * densidad + b * (1 - densidad))
                           for a, b in zip(color, self.fondo_celda))
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=mezcla)
            return True
        cuadrantes = {"▖": "3", "▗": "4", "▘": "1", "▝": "2", "▙": "134", "▚": "14",
                      "▛": "123", "▜": "124", "▞": "23", "▟": "234"}
        if c in cuadrantes:
            for q in cuadrantes[c]:
                qx = x + (0 if q in "13" else w / 2)
                qy = y + (0 if q in "12" else h / 2)
                d.rectangle([qx, qy, qx + w / 2 - 1, qy + h / 2 - 1], fill=color)
            return True
        return False

    # brazos: (izquierda, derecha, arriba, abajo); 1 fino, 2 grueso
    CAJAS = {
        "─": (1, 1, 0, 0), "━": (2, 2, 0, 0), "│": (0, 0, 1, 1), "┃": (0, 0, 2, 2),
        "┌": (0, 1, 0, 1), "┐": (1, 0, 0, 1), "└": (0, 1, 1, 0), "┘": (1, 0, 1, 0),
        "├": (0, 1, 1, 1), "┤": (1, 0, 1, 1), "┬": (1, 1, 0, 1), "┴": (1, 1, 1, 0),
        "┼": (1, 1, 1, 1), "╴": (1, 0, 0, 0), "╶": (0, 1, 0, 0), "╵": (0, 0, 1, 0),
        "╷": (0, 0, 0, 1), "╸": (2, 0, 0, 0), "╺": (0, 2, 0, 0), "╹": (0, 0, 2, 0),
        "╻": (0, 0, 0, 2), "╼": (1, 2, 0, 0), "╾": (2, 1, 0, 0),
        "┏": (0, 2, 0, 2), "┓": (2, 0, 0, 2), "┗": (0, 2, 2, 0), "┛": (2, 0, 2, 0),
        "┣": (0, 2, 2, 2), "┫": (2, 0, 2, 2), "┳": (2, 2, 0, 2), "┻": (2, 2, 2, 0),
        "╋": (2, 2, 2, 2), "┠": (0, 1, 2, 2), "┨": (1, 0, 2, 2),
    }
    DISCONTINUAS = {"╌": (1, 2), "╍": (2, 2), "┄": (1, 3), "┅": (2, 3), "┈": (1, 4),
                    "┉": (2, 4), "╎": (1, -2), "┆": (1, -3), "┊": (1, -4)}

    def _caja(self, d, x, y, c, color):
        w, h = self.cw, self.ch
        cx, cy = x + w // 2, y + h // 2
        fino, grueso = max(1, round(w / 9)), max(2, round(w / 4.5))
        if c in self.CAJAS:
            izq, der, arr, aba = self.CAJAS[c]
            for peso, (x0, y0, x1, y1) in (
                    (izq, (x, cy, cx, cy)), (der, (cx, cy, x + w, cy)),
                    (arr, (cx, y, cx, cy)), (aba, (cx, cy, cx, y + h))):
                if not peso:
                    continue
                g = fino if peso == 1 else grueso
                if y0 == y1:
                    d.rectangle([min(x0, x1) - (g // 2 if x0 == cx or x1 == cx else 0),
                                 y0 - g // 2, max(x0, x1) - 1 + (g - g // 2 if x0 == cx or x1 == cx else 0),
                                 y0 - g // 2 + g - 1], fill=color)
                else:
                    d.rectangle([x0 - g // 2, min(y0, y1), x0 - g // 2 + g - 1, max(y0, y1) - 1],
                                fill=color)
            return True
        if c in self.DISCONTINUAS:
            peso, n = self.DISCONTINUAS[c]
            g = fino if peso == 1 else grueso
            if n > 0:
                trozo = w / n
                for i in range(n):
                    d.rectangle([x + i * trozo, cy - g // 2, x + i * trozo + trozo * 0.6,
                                 cy - g // 2 + g - 1], fill=color)
            else:
                n = -n
                trozo = h / n
                for i in range(n):
                    d.rectangle([cx - g // 2, y + i * trozo, cx - g // 2 + g - 1,
                                 y + i * trozo + trozo * 0.6], fill=color)
            return True
        if c in "╭╮╰╯":
            r = max(2, min(w, h) // 2 - 1)
            g = fino
            derecha = c in "╭╰"                 # el brazo horizontal sale a la derecha
            abajo = c in "╭╮"                   # el vertical baja
            ax = cx + r if derecha else cx - r  # centro del arco
            ay = cy + r if abajo else cy - r
            inicio = {"╭": 180, "╮": 270, "╰": 90, "╯": 0}[c]
            d.arc([ax - r, ay - r, ax + r, ay + r], inicio, inicio + 90, fill=color, width=g)
            if derecha:
                self._rect(d, ax, cy - g // 2, x + w - 1, cy - g // 2 + g - 1, color)
            else:
                self._rect(d, x, cy - g // 2, ax, cy - g // 2 + g - 1, color)
            if abajo:
                self._rect(d, cx - g // 2, ay, cx - g // 2 + g - 1, y + h - 1, color)
            else:
                self._rect(d, cx - g // 2, y, cx - g // 2 + g - 1, ay, color)
            return True
        return False

    @staticmethod
    def _rect(d, x0, y0, x1, y1, color):
        if x1 >= x0 and y1 >= y0:
            d.rectangle([x0, y0, x1, y1], fill=color)

    def _braille(self, d, x, y, c, color):
        o = ord(c)
        if not 0x2800 <= o <= 0x28FF:
            return False
        bits = o - 0x2800
        w, h = self.cw, self.ch
        r = max(1.2, w / 7)
        posiciones = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3)]
        for i, (col, fila) in enumerate(posiciones):
            if bits & (1 << i):
                px = x + w * (0.3 + 0.4 * col)
                py = y + h * (0.16 + 0.225 * fila)
                d.ellipse([px - r, py - r, px + r, py + r], fill=color)
        return True

    @staticmethod
    def _celda(datos, fy, fx):
        """(frente, fondo, negrita, inverso, tenue) de una celda, en el formato
        nuevo (el lienzo) o en el viejo (atributos de curses + pares)."""
        if "frente" in datos:
            e = datos["estilo"][fy][fx]
            fondo = datos["fondo"][fy][fx]
            inverso = bool(e & 4) or fondo == -2
            return (datos["frente"][fy][fx], -1 if fondo == -2 else fondo,
                    bool(e & 1), inverso, bool(e & 2))
        a = datos["atributos"][fy][fx]
        return tuple(datos["estilos"].get(str(a), [-1, -1, False, False, False]))

    def pintar(self, datos, margen=10):
        cols, filas = datos["cols"], datos["filas"]
        img = Image.new("RGB", (cols * self.cw + 2 * margen, filas * self.ch + 2 * margen),
                        self.fondo)
        d = ImageDraw.Draw(img)
        for fy, texto in enumerate(datos["texto"]):
            for fx, c in enumerate(texto):
                frente, fondo, negrita, inverso, tenue = self._celda(datos, fy, fx)
                tinta = self.tinta if frente in (-1, None) else xterm(frente)
                papel = self.fondo if fondo in (-1, None) else xterm(fondo)
                if inverso:
                    tinta, papel = papel, tinta
                if tenue:
                    tinta = tuple((t + p) // 2 for t, p in zip(tinta, papel))
                x = margen + fx * self.cw
                y = margen + fy * self.ch
                if papel != self.fondo:
                    d.rectangle([x, y, x + self.cw - 1, y + self.ch - 1], fill=papel)
                if c == " ":
                    continue
                self.fondo_celda = papel
                if self._bloque(d, x, y, c, tinta) or self._caja(d, x, y, c, tinta) \
                        or self._braille(d, x, y, c, tinta):
                    continue
                fuente = self.fuente_para(c, negrita)
                d.text((x, y + 1), c, font=fuente, fill=tinta)
        return img


def _cuadros(carpeta):
    return sorted(glob.glob(os.path.join(carpeta, "*.json")))


def _instante(ruta):
    """El «t» de un cuadro sin leer el JSON entero: va al principio."""
    with open(ruta, encoding="utf-8") as f:
        cabeza = f.read(200)
    return float(cabeza.split('"t":', 1)[1].split(",", 1)[0])


def _leer(ruta):
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("entrada", help="un .json de captura, o una carpeta de cuadros")
    ap.add_argument("-o", "--salida")
    ap.add_argument("--fondo", default="1b1d23", help="fondo de la terminal, en hex")
    ap.add_argument("--tinta", default="d0d0d0", help="color de texto por defecto, en hex")
    ap.add_argument("--tam", type=int, default=15, help="tamaño de letra")
    ap.add_argument("--hoja", action="store_true", help="varios cuadros apilados en un PNG")
    ap.add_argument("--gif", action="store_true", help="GIF animado con todos los cuadros")
    ap.add_argument("-n", type=int, default=6, help="cuántos cuadros en la hoja")
    ap.add_argument("--desde", type=int, default=0, help="saltar los N primeros cuadros")
    ap.add_argument("--hasta", type=int, default=0, help="quedarse con N cuadros (0 = todos)")
    ap.add_argument("--recorte", help="y0:y1 filas a pintar, para la hoja o el GIF")
    ap.add_argument("--entre", help="a:b segundos desde el primer cuadro (hoja o GIF)")
    ap.add_argument("--paso", type=int, default=1, help="en el GIF, uno de cada N cuadros")
    args = ap.parse_args()

    pintor = Pintor(args.tam, args.fondo, args.tinta)

    def recortar(datos):
        if not args.recorte:
            return datos
        y0, y1 = (int(v) for v in args.recorte.split(":"))
        datos = dict(datos)
        for clave in ("texto", "atributos", "frente", "fondo", "estilo"):
            if clave in datos:
                datos[clave] = datos[clave][y0:y1]
        datos["filas"] = len(datos["texto"])
        return datos

    if os.path.isdir(args.entrada):
        rutas = _cuadros(args.entrada)[args.desde:]
        if args.entre:
            a, b = (float(v) for v in args.entre.split(":"))
            tiempos = [_instante(r) for r in rutas]
            cero = tiempos[0] if tiempos else 0
            rutas = [r for r, t in zip(rutas, tiempos) if a <= t - cero <= b]
        if args.hasta:
            rutas = rutas[:args.hasta]
        if not rutas:
            sys.exit("no hay cuadros en " + args.entrada)
        if args.gif:
            rutas = rutas[::max(1, args.paso)]
            imagenes, tiempos = [], []
            previo = None
            for ruta in rutas:
                datos = _leer(ruta)
                imagenes.append(pintor.pintar(recortar(datos)))
                tiempos.append(datos["t"])
            duraciones = [max(20, int((b - a) * 1000)) for a, b in zip(tiempos, tiempos[1:])]
            duraciones.append(duraciones[-1] if duraciones else 100)
            salida = args.salida or os.path.join(args.entrada, "animacion.gif")
            imagenes[0].save(salida, save_all=True, append_images=imagenes[1:],
                             duration=duraciones, loop=0, optimize=False)
            print(salida, f"({len(imagenes)} cuadros)")
            del previo
            return
        paso = max(1, len(rutas) // args.n)
        elegidas = rutas[::paso][:args.n]
        imagenes = []
        t0 = None
        for ruta in elegidas:
            datos = _leer(ruta)
            t0 = datos["t"] if t0 is None else t0
            img = pintor.pintar(recortar(datos), margen=6)
            etiqueta = Image.new("RGB", (img.width, 18), (40, 40, 48))
            ImageDraw.Draw(etiqueta).text((6, 2), f"t = {datos['t'] - t0:5.2f} s",
                                          font=pintor.f, fill=(200, 200, 210))
            imagenes.append(etiqueta)
            imagenes.append(img)
        alto = sum(i.height for i in imagenes)
        hoja = Image.new("RGB", (max(i.width for i in imagenes), alto), (40, 40, 48))
        y = 0
        for i in imagenes:
            hoja.paste(i, (0, y))
            y += i.height
        salida = args.salida or os.path.join(args.entrada, "hoja.png")
        hoja.save(salida)
        print(salida)
        return

    datos = recortar(_leer(args.entrada))
    salida = args.salida or os.path.splitext(args.entrada)[0] + ".png"
    pintor.pintar(datos).save(salida)
    print(salida)


if __name__ == "__main__":
    main()
