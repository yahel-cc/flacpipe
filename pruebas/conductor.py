#!/usr/bin/env python3
"""Corre la TUI dentro de un pty y vuelca la rejilla lógica tras cada repintado.

No se ejecuta a mano: lo lanza probar.py como hijo del pty. Recibe por
variables de entorno dónde volcar (SALIDA) y un escenario en JSON (ESCENARIO)
con lo que hay que sobreescribir de fh_comun/flacpipe_tui para simular una
cola, una descarga en marcha o una tarjeta montada.

Tras cada cuadro se vuelca el lienzo de la interfaz (tui.LIENZO), que ya
tiene el carácter, el color de frente y de fondo y el estilo de cada celda.
Nada de emular ANSI. Con GRABAR=1 se guarda además cada cuadro por separado,
para hacer GIFs con render.py, y en tiempos.json cuánto tarda en pintarse.

demo.py reusa preparar() para montar el mismo escenario en tu terminal.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import flacpipe_tui as tui  # noqa: E402


def preparar(escenario):
    """Sobreescribe lo que diga el escenario: rutas de la cola y el log,
    Nicotine+ vivo, una tarjeta montada y una copia que finge escribir."""
    comun = tui.comun
    for clave, valor in escenario.get("comun", {}).items():
        setattr(comun, clave, valor)
    for clave, valor in escenario.get("tui", {}).items():
        setattr(tui, clave, valor)
    for clave, valor in escenario.get("listas", {}).items():
        setattr(tui.listas, clave, valor)
    if escenario.get("nicotine_vivo"):
        comun.pid_nicotine = lambda: 424242
    if escenario.get("fiio_falso"):
        _fiio_falso(escenario.get("fiio_mb_s", 66), escenario.get("fiio_tarjeta"))


def _fiio_falso(mb_s, tarjeta=None):
    """`tarjeta` es una carpeta real que hace de montaje, para que clasificar
    vea álbumes de verdad; sin ella la tarjeta falsa está vacía."""
    fiio = tui.fiio
    fiio.detectar_destinos = lambda: [{
        "montaje": tarjeta or "/run/media/yahel/SNOWSKY", "dispositivo": "/dev/loop9", "tipo": "vfat",
        "libre": 27_400_000_000, "total": 32_000_000_000, "etiqueta": "SNOWSKY"}]
    bloque = 2_000_000
    pausa = bloque / (mb_s * 1_000_000)

    def copiar(destino_raiz, albumes, simular=False, rehacer=False, informar=print,
               evento=None, cancelado=None):
        resumen = {"copiados": 0, "omitidos": 0, "archivos": 0, "bytes": 0,
                   "desordenados": [], "errores": [], "abortado": False}
        hay = fiio.clasificar(destino_raiz, albumes)
        motivos = {"al_dia": "ya está y en orden", "otro_orden": "el orden físico no coincide",
                   "distintos": "copia incompleta o distinta"}
        omitir = {a["rel"]: motivos[k] for k in motivos for a in hay[k]} if not rehacer else {}
        for a in albumes:
            if a["rel"] in omitir:
                evento({"t": "omitido", "rel": a["rel"], "motivo": omitir[a["rel"]]})
                if omitir[a["rel"]] == motivos["al_dia"]:
                    resumen["omitidos"] += 1
                else:
                    resumen["desordenados"].append(a["rel"])
                continue
            informar(f"▸ {a['rel']}")
            evento({"t": "album", "rel": a["rel"], "pistas": len(a["pistas"]),
                    "bytes": a["bytes"]})
            por_pista = a["bytes"] // max(1, len(a["pistas"]))
            for i, p in enumerate(a["pistas"], 1):
                nombre = p if isinstance(p, str) else p.get("archivo", str(p))
                evento({"t": "pista", "rel": a["rel"], "archivo": nombre, "indice": i,
                        "total": len(a["pistas"]), "bytes": por_pista})
                hecho = 0
                while hecho < por_pista:
                    if cancelado and cancelado():
                        return resumen
                    n = min(bloque, por_pista - hecho)
                    hecho += n
                    evento({"t": "bytes", "n": n})
                    time.sleep(pausa)
                evento({"t": "pista_ok", "archivo": nombre})
                informar(f"  {tui.OK} {nombre}")
                resumen["archivos"] += 1
                resumen["bytes"] += por_pista
            evento({"t": "orden", "rel": a["rel"], "ok": True})
            evento({"t": "album_ok", "rel": a["rel"]})
            resumen["copiados"] += 1
        return resumen

    fiio.copiar = copiar


def _volcar_json(numero):
    """El cuadro tal cual quedó en el lienzo: carácter, frente, fondo y estilo
    de cada celda, con los colores ya resueltos (256, o los básicos)."""
    lienzo = tui.LIENZO
    return {"frame": numero, "t": time.monotonic(),
            "cols": lienzo.ancho, "filas": lienzo.alto,
            "colores": tui._PALETA["colores"],
            "texto": ["".join(c[0] for c in fila) for fila in lienzo.filas],
            "frente": [[c[1] for c in fila] for fila in lienzo.filas],
            "fondo": [[c[2] for c in fila] for fila in lienzo.filas],
            "estilo": [[c[3] for c in fila] for fila in lienzo.filas]}


def volcar_cuadros(salida, grabar=False):
    """Envuelve App.dibujar para dejar cada cuadro en `salida`."""
    if grabar:
        os.makedirs(os.path.join(salida, "cuadros"), exist_ok=True)
    original = tui.App.dibujar
    contador = {"n": 0}
    tiempos = []

    def dibujar(self):
        inicio = time.perf_counter()
        original(self)
        tiempos.append(time.perf_counter() - inicio)
        contador["n"] += 1
        datos = _volcar_json(contador["n"])
        texto = "\n".join(fila.rstrip() for fila in datos["texto"])
        tmp = os.path.join(salida, "pantalla.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"# frame {contador['n']}  {datos['cols']}x{datos['filas']}\n")
            f.write(texto + "\n")
        os.replace(tmp, os.path.join(salida, "pantalla.txt"))
        tmp = os.path.join(salida, "pantalla.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(salida, "pantalla.json"))
        if grabar:
            with open(os.path.join(salida, "cuadros", f"{contador['n']:05d}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(datos, f, ensure_ascii=False)
        if contador["n"] % 30 == 0:
            recientes = sorted(tiempos[-300:])
            with open(os.path.join(salida, "tiempos.json"), "w") as f:
                json.dump({"cuadros": contador["n"], "pares": len(tui._PARES),
                           "mediana_ms": recientes[len(recientes) // 2] * 1000,
                           "p95_ms": recientes[int(len(recientes) * 0.95)] * 1000,
                           "max_ms": recientes[-1] * 1000}, f)

    tui.App.dibujar = dibujar


if __name__ == "__main__":
    preparar(json.loads(os.environ.get("ESCENARIO", "{}")))
    volcar_cuadros(os.environ["SALIDA"], grabar=os.environ.get("GRABAR") == "1")
    sys.exit(tui.ejecutar())
