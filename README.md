# Painel de impressão do Calc

Extensão para o **LibreOffice Calc no Linux** que traz duas coisas que faltam em relação ao Excel:

- **Ajustar colunas à largura da página**: um botão que faz todas as colunas da aba atual caberem na largura de uma página, sem mudar as outras abas.
- **Painel de impressão**: no estilo do Excel, com as configurações à esquerda e a prévia em tempo real à direita. Permite escolher impressora, cópias, páginas, aba atual, pasta inteira ou seleção, frente e verso, orientação, papel, margens e escala, que pode ser uma página, todas as colunas, todas as linhas, uma porcentagem ou um número de páginas.

A prévia é gerada de uma cópia oculta da planilha, sem alterar o arquivo, e o papel sai igual ao que aparece na tela.

## Baixar e instalar

1. Baixe **PainelImpressaoCalc.oxt** na [última versão](https://github.com/edufigueiroa/PainelImpressaoCalc/releases/latest).
2. No LibreOffice, abra **Ferramentas > Gerenciador de extensões > Adicionar** e escolha o arquivo.
3. Reinicie o LibreOffice. Os botões aparecem na barra padrão do Calc, e o painel fica em **Arquivo > Painel de impressão**.

Requisitos: Linux com CUPS (`lp`), Python 3 com **PySide6**, e o suporte a Python do LibreOffice.

```sh
sudo dnf install python3-pyside6 libreoffice-pyuno   # Fedora
```

Em outras distribuições, instale o PySide6 e o pacote de Python do LibreOffice (geralmente `python3-uno`) pelo gerenciador de pacotes.

O painel tem o link **Verificar atualizações**, que avisa quando sai uma versão nova.

## Desenvolvimento

`./install.sh` gera a extensão (`build.py`) e instala com `unopkg`. O menu e os botões só podem ser reinstalados
com o LibreOffice fechado. Já o painel (`panel.py`) é atualizado mesmo com ele aberto.

Para lançar uma versão nova, aumente `VERSION` em `panel.py` e `version` em `extension/description.xml`, depois rode:

```sh
python3 build.py
gh release create vX.Y.Z dist/PainelImpressaoCalc.oxt --title "Painel de impressão X.Y.Z" --notes "..."
```
