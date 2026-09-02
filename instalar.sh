#!/usr/bin/env bash
# Instala el plugin FLAC Hunter en el snap de Nicotine+ y deja `flacpipe` en el PATH.
# Nicotine+ debe estar CERRADO: al salir reescribe su config y pisaría los cambios.
set -euo pipefail

ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ORIGEN="$ORIGEN/flac_hunter"
DIR_SNAP="/home/yahel/snap/nicotine-plus/current/.local/share/nicotine"
DIR_PLUGINS="$DIR_SNAP/plugins"
CONFIG="/home/yahel/snap/nicotine-plus/current/.config/nicotine/config"
DIR_BIN="$HOME/.local/bin"

if pgrep -af "nicotine" | grep -qv "instalar.sh"; then
  if pgrep -x nicotine-plus >/dev/null 2>&1 || pgrep -f "bin/nicotine" >/dev/null 2>&1; then
    echo "❌ Nicotine+ está abierto. Ciérralo y vuelve a ejecutar este script."
    exit 1
  fi
fi

echo "▸ Creando $DIR_PLUGINS"
mkdir -p "$DIR_PLUGINS" "$DIR_BIN" "/home/yahel/Música/.pipeline" "/home/yahel/Música/.descargas"

echo "▸ Enlazando el plugin (el código sigue viviendo en $PLUGIN_ORIGEN)"
rm -rf "$DIR_PLUGINS/flac_hunter"
if ! ln -s "$PLUGIN_ORIGEN" "$DIR_PLUGINS/flac_hunter" 2>/dev/null; then
  cp -r "$PLUGIN_ORIGEN" "$DIR_PLUGINS/flac_hunter"
  echo "  (no se pudo enlazar; se copió. Reinstala tras cada cambio de código)"
fi
find "$PLUGIN_ORIGEN" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo "▸ Habilitando el plugin en la config de Nicotine+"
python3 - "$CONFIG" <<'PY'
import io, re, sys
ruta = sys.argv[1]
texto = io.open(ruta, encoding="utf-8").read()

m = re.search(r"^enabled = (\[.*\])$", texto, re.MULTILINE)
if not m:
    sys.exit("No encontré la línea 'enabled = [...]' en [plugins]")

import ast
lista = ast.literal_eval(m.group(1))
if "flac_hunter" not in lista:
    lista.append("flac_hunter")
    texto = texto[:m.start()] + f"enabled = {lista!r}" + texto[m.end():]

# Las descargas manuales no deben caer en la raíz de la biblioteca:
# ahí cada carpeta suelta parecería un artista.
texto = re.sub(r"^downloaddir = /home/yahel/Música$",
               "downloaddir = /home/yahel/Música/.descargas",
               texto, flags=re.MULTILINE)

io.open(ruta, "w", encoding="utf-8").write(texto)
print("  plugins habilitados:", lista)
PY

echo "▸ Enlazando flacpipe en $DIR_BIN"
ln -sf "$ORIGEN/flacpipe" "$DIR_BIN/flacpipe"

echo
echo "✅ Instalado."
echo "   1) Abre Nicotine+ (o: flacpipe arrancar)"
echo "   2) flacpipe pedir \"Radiohead - OK Computer\""
echo "   3) flacpipe estado"
case ":$PATH:" in
  *":$DIR_BIN:"*) ;;
  *) echo; echo "   ⚠️  $DIR_BIN no está en tu PATH. Añade a ~/.zshrc:";
     echo "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
