# Painel de impressão do Calc

Extensão do LibreOffice Calc com:

- **Ajustar colunas à largura da página**: um botão que faz todas as colunas da aba atual caberem em uma página, sem mudar as outras abas.
- **Painel de impressão**: no estilo do Excel, com as configurações à esquerda e a prévia em tempo real à direita. Inclui impressora, cópias, páginas, aba atual, pasta inteira ou seleção, frente e verso, orientação, papel, margens e escala.

A prévia é gerada de uma cópia oculta da planilha. A impressão envia exatamente esse PDF para a impressora com `lp`.

## Instalar e atualizar

```sh
./install.sh
```

O painel fica em `~/.local/share/painel-impressao-calc/`. O menu e os botões do LibreOffice só são instalados
com o LibreOffice fechado. O link **Verificar atualizações**, no painel, consulta as
[Releases](https://github.com/edufigueiroa/PainelImpressaoCalc/releases/latest) pelo `gh` e reinstala sozinho.

## Nova versão

Aumente `VERSION` em `panel.py` e `version` em `extension/description.xml`, depois rode
`gh release create vX.Y.Z --title "Painel de impressão X.Y.Z" --notes "..."`.
