#!/usr/bin/env bash
# Instalador para Linux y macOS: busca un Python 3.8+ y lanza instalar.py.
# Nicotine+ debe estar CERRADO: al salir reescribe su config y pisaría los cambios.
#
#   bash instalar.sh                  # instala
#   bash instalar.sh --simular        # enseña qué haría
#   bash instalar.sh --quitar         # desinstala
#   bash instalar.sh --help           # todas las opciones
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for candidato in python3 python; do
  if command -v "$candidato" >/dev/null 2>&1 \
     && "$candidato" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    exec "$candidato" "$AQUI/instalar.py" "$@"
  fi
done

echo "Hace falta Python 3.8 o más nuevo y no lo encuentro en el PATH." >&2
case "$(uname -s)" in
  Darwin) echo "  macOS:  brew install python   (o https://www.python.org/downloads/)" >&2 ;;
  *)      echo "  Linux:  sudo apt install python3   (o el gestor de tu distribución)" >&2 ;;
esac
exit 1
