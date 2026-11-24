#!/bin/bash
# Instala o painel de impressão. O painel é atualizado sempre; a extensão (menu e botões)
# só é reinstalada com o LibreOffice fechado.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

install -Dm755 panel.py "$HOME/.local/share/painel-impressao-calc/panel.py"
echo "Painel instalado."

if pgrep -x soffice.bin >/dev/null; then
    if unopkg list 2>/dev/null | grep -q "Identifier: local.painelimpressao.calc"; then
        echo "O menu e os botões do LibreOffice já estão instalados."
        exit 0
    fi
    echo "Feche o LibreOffice e execute de novo para instalar o menu e os botões." >&2
    exit 1
fi

python3 build.py
unopkg remove local.painelimpressao.calc >/dev/null 2>&1 || true
unopkg add dist/PainelImpressaoCalc.oxt
echo "Instalado. Abra o Calc."
