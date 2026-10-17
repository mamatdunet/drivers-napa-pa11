#!/bin/bash
# Installation de l'outil PA11 (Napa / A-Max PA11) pour macOS.
# Double-cliquez (clic droit > Ouvrir la première fois) ou lancez : bash install.command
set -e
cd "$(dirname "$0")"
DEST="$HOME/PA11"
mkdir -p "$DEST"

echo "== Installation de l'outil PA11 dans $DEST =="

if ! /usr/bin/xcrun --find python3 >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 est nécessaire. macOS va proposer d'installer les « outils de ligne de"
  echo "commande » d'Apple : acceptez, puis relancez ce script."
  xcode-select --install || true
  exit 1
fi
PY=$(command -v python3)
echo "Python : $($PY --version 2>&1)"

cp pa11.py "$DEST/pa11.py"
if [ ! -x "$DEST/venv/bin/python" ]; then
  "$PY" -m venv "$DEST/venv"
fi
"$DEST/venv/bin/python" -m pip install --quiet --upgrade pip
"$DEST/venv/bin/python" -m pip install --quiet --upgrade pyusb libusb-package

# Vérification que libusb est bien chargeable
if ! "$DEST/venv/bin/python" -c "import usb.core, libusb_package; usb.core.find(backend=libusb_package.get_libusb1_backend())" 2>/dev/null; then
  echo "La bibliothèque libusb n'a pas pu être chargée."
  echo "Si vous avez Homebrew :  brew install libusb   puis relancez ce script."
  exit 1
fi

# Commande en ligne : ~/PA11/pa11
cat > "$DEST/pa11" <<'EOF'
#!/bin/bash
exec "$HOME/PA11/venv/bin/python" "$HOME/PA11/pa11.py" "$@"
EOF
chmod +x "$DEST/pa11"

mk() {  # mk "Nom.command" "sous-commande"
  cat > "$DEST/$1" <<EOF
#!/bin/bash
clear
"\$HOME/PA11/pa11" $2
echo
read -n 1 -s -r -p "Appuyez sur une touche pour fermer cette fenêtre."
EOF
  chmod +x "$DEST/$1"
}
mk "1 - Diagnostic PA11.command" "info"
mk "2 - Ouvrir PA11.command" "open"
mk "3 - Fermer PA11.command" "close"
mk "Sauvegarde complète PA11.command" "backup"

echo
echo "Installation terminée."
echo "Dans le dossier $DEST (qui va s'ouvrir) :"
echo "  1 - Diagnostic PA11   : vérifie que le lecteur est reconnu (lecture seule)"
echo "  2 - Ouvrir PA11       : copie le contenu du lecteur et l'ouvre dans le Finder"
echo "  3 - Fermer PA11       : réécrit vos changements sur le lecteur"
open "$DEST"
