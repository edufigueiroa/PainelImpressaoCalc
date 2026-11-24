#!/bin/bash
# Gera a extensão e instala no LibreOffice deste usuário (Linux e macOS).
# O LibreOffice precisa estar fechado. No Windows, instale o .oxt pelo Gerenciador de extensões.
set -euo pipefail
cd "$(dirname "$0")"

if pgrep -x soffice.bin >/dev/null 2>&1 || pgrep -x soffice >/dev/null 2>&1; then
    echo "Feche o LibreOffice e execute de novo." >&2
    exit 1
fi

UNOPKG=$(command -v unopkg || true)
[ -z "$UNOPKG" ] && [ -x /Applications/LibreOffice.app/Contents/MacOS/unopkg ] && UNOPKG=/Applications/LibreOffice.app/Contents/MacOS/unopkg
[ -z "$UNOPKG" ] && { echo "unopkg não encontrado. Instale o LibreOffice." >&2; exit 1; }

python3 build.py
"$UNOPKG" remove local.painelimpressao.calc >/dev/null 2>&1 || true
"$UNOPKG" add dist/PainelImpressaoCalc.oxt
rm -rf "$HOME/.local/share/painel-impressao-calc"   # painel antigo (até a 1.1.x), não é mais usado
echo "Instalado. Abra o Calc."
