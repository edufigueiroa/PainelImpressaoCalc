#!/bin/bash
# Instala o painel. Feche o LibreOffice antes de executar.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if pgrep -x soffice.bin >/dev/null; then
    echo "Feche o LibreOffice e execute novamente." >&2
    exit 1
fi

python3 build.py
install -Dm755 panel.py "$HOME/.local/share/painel-impressao-calc/panel.py"
unopkg remove local.painelimpressao.calc >/dev/null 2>&1 || true
unopkg add dist/PainelImpressaoCalc.oxt
echo "Instalado. Abra o Calc."
