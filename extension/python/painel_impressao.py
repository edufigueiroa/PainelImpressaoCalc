"""Painel de impressão do LibreOffice Calc, no estilo do Excel.

Roda dentro do próprio LibreOffice, com as janelas dele, e funciona em Windows, macOS e Linux.
A prévia vem de uma cópia oculta da planilha exportada para PDF; a página é convertida em imagem
pelo Draw. A impressão usa o CUPS (lp) quando existe (Linux e macOS) e o LibreOffice no Windows.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading

import uno
import unohelper
from com.sun.star.awt import (XActionListener, XCallback, XItemListener, XTextListener,
                              XTopWindowListener, XWindowListener)
from com.sun.star.beans import PropertyValue
from com.sun.star.document import XDocumentEventListener

VERSION = "1.2.0"
EXTENSION_ID = "local.painelimpressao.calc"
UPDATE_FEED = "https://raw.githubusercontent.com/edufigueiroa/PainelImpressaoCalc/main/update.xml"

PAPERS = {  # 1/100 mm; o último item é o nome do papel no CUPS
    "A4": ("A4", "21 x 29,7 cm", 21000, 29700, "A4"),
    "A3": ("A3", "29,7 x 42 cm", 29700, 42000, "A3"),
    "A5": ("A5", "14,8 x 21 cm", 14800, 21000, "A5"),
    "Letter": ("Carta", "21,59 x 27,94 cm", 21590, 27940, "Letter"),
    "Legal": ("Legal", "21,59 x 35,56 cm", 21590, 35560, "Legal"),
    "Oficio": ("Ofício", "21,6 x 33 cm", 21600, 33000, "Oficio"),
}
MARGINS = {  # cm: esquerda, direita, superior, inferior
    "normal": ("Margens normais", "Esq. e dir. 1,78 cm · sup. e inf. 1,91 cm", 1.78, 1.78, 1.91, 1.91),
    "wide": ("Margens largas", "2,54 cm em todos os lados", 2.54, 2.54, 2.54, 2.54),
    "narrow": ("Margens estreitas", "Esq. e dir. 0,64 cm · sup. e inf. 1,91 cm", 0.64, 0.64, 1.91, 1.91),
    "custom": ("Margens personalizadas", "Definidas por você"),
}
CHOICES = {
    "scope": [("active", "Imprimir planilha ativa", "Somente a aba atual"),
              ("all", "Imprimir toda a pasta de trabalho", "Todas as abas"),
              ("selection", "Imprimir seleção", "Somente as células selecionadas")],
    "sides": [("one-sided", "Imprimir em um lado", "Apenas um lado da folha"),
              ("two-sided-long-edge", "Frente e verso (borda longa)", "Vira as páginas na borda longa"),
              ("two-sided-short-edge", "Frente e verso (borda curta)", "Vira as páginas na borda curta")],
    "collate": [(True, "Agrupado", "1;2;3   1;2;3   1;2;3"),
                (False, "Desagrupado", "1;1;1   2;2;2   3;3;3")],
    "orientation": [(False, "Orientação retrato", "Página em pé"),
                    (True, "Orientação paisagem", "Página deitada")],
    "paper": [(k, f"{v[0]}  ({v[1]})", v[1]) for k, v in PAPERS.items()],
    "margins": [(k, v[0], v[1]) for k, v in MARGINS.items()],
    "scaling": [("none", "Sem dimensionamento", "Imprime no tamanho real"),
                ("one", "Ajustar planilha em uma página", "Tudo em uma única página"),
                ("width", "Ajustar todas as colunas em uma página", "Largura de 1 página"),
                ("height", "Ajustar todas as linhas em uma página", "Altura de 1 página"),
                ("percent", "Escala personalizada", "Porcentagem do tamanho real"),
                ("pages", "Ajustar a páginas", "Número de páginas de largura e altura")],
}

WIDTH, HEIGHT = 560, 372      # tamanho inicial da janela, em unidades do LibreOffice (APPFONT)
LEFT_X, LEFT_W = 8, 176       # coluna das configurações
PREVIEW_X = LEFT_X + LEFT_W + 10
BAR_H = 16                    # barra de páginas abaixo da prévia
GAP = 3

_panels = {}                  # um painel aberto por documento


# ---------------------------------------------------------------- utilidades

def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def clean_env(keep_locale=False):
    """Ambiente para programas externos, sem as variáveis internas do LibreOffice."""
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "URE_BOOTSTRAP", "UNO_PATH",
                "QT_PLUGIN_PATH", "SAL_USE_VCLPLUGIN", "DYLD_LIBRARY_PATH"):
        env.pop(key, None)
    if not keep_locale:
        env["LC_ALL"] = "C"
    return env


def tool(name):
    """Caminho de um comando do CUPS (lp, lpstat, lpoptions). No Windows não existe."""
    if sys.platform.startswith("win"):
        return None
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/usr/bin", "/usr/local/bin", "/usr/sbin"):
        path = os.path.join(folder, name)
        if os.access(path, os.X_OK):
            return path
    return None


def run(*args):
    try:
        return subprocess.run(args, env=clean_env(), capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def list_printers(ctx):
    """Lista (nome, estado) das impressoras e a padrão. Usa o CUPS se houver, senão o LibreOffice."""
    lpstat = tool("lpstat")
    if lpstat:
        line = run(lpstat, "-d")
        default = line.split(":", 1)[1].strip() if ":" in line else ""
        states = {}
        for row in run(lpstat, "-p").splitlines():
            parts = row.split()
            if len(parts) >= 3 and parts[0] == "printer":
                states[parts[1]] = ("Indisponível" if "disabled" in row else
                                    "Imprimindo" if "printing" in row else "Pronta")
        names = [n.strip() for n in run(lpstat, "-e").splitlines() if n.strip()]
        if names:
            names.sort(key=lambda n: (n != default, n.lower()))
            return [(n, states.get(n, "")) for n in names], default
    server = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.PrinterServer", ctx)
    names = list(server.getPrinterNames())
    try:
        default = server.getDefaultPrinterName()
    except Exception:  # noqa: BLE001 - versões antigas não têm este método
        default = names[0] if names else ""
    names.sort(key=lambda n: (n != default, n.lower()))
    return [(n, "") for n in names], default


def printer_supports_duplex(name):
    lpoptions = tool("lpoptions")
    if not lpoptions or not tool("lp"):
        return False
    for row in run(lpoptions, "-p", name, "-l").splitlines():
        if row.startswith("Duplex/"):
            return "DuplexNoTumble" in row
    return False


def open_printer_settings():
    """Abre as configurações de impressoras do sistema."""
    try:
        if sys.platform.startswith("win"):
            os.startfile("ms-settings:printers")  # noqa: S606
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.Print-Scan-Settings.extension"])
            return
        for args in (["kcmshell6", "kcm_printer_manager"], ["system-config-printer"],
                     ["gnome-control-center", "printers"], ["xdg-open", "http://localhost:631/printers"]):
            if shutil.which(args[0]):
                subprocess.Popen(args, env=clean_env(keep_locale=True), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return
    except OSError:
        pass


def version_tuple(text):
    return tuple(int(p) for p in str(text).lstrip("v").split(".") if p.isdigit())


# ---------------------------------------------------------------- estilos de página

def own_page_style(doc, sheet):
    """Devolve um estilo exclusivo da aba, para não alterar as outras abas."""
    family = doc.StyleFamilies.getByName("PageStyles")
    name = sheet.PageStyle
    sheets = doc.Sheets
    shared = any(sheets.getByIndex(i).Name != sheet.Name and sheets.getByIndex(i).PageStyle == name
                 for i in range(sheets.Count))
    source = family.getByName(name)
    if not shared:
        return source
    base = f"{name} - {sheet.Name}"
    new_name, n = base, 2
    while family.hasByName(new_name):
        new_name, n = f"{base} {n}", n + 1
    style = doc.createInstance("com.sun.star.style.PageStyle")
    family.insertByName(new_name, style)
    for info in source.PropertySetInfo.Properties:
        if info.Attributes & 16 or info.Name in ("DisplayName", "FollowStyle", "Hidden", "StyleName"):
            continue
        try:
            style.setPropertyValue(info.Name, source.getPropertyValue(info.Name))
        except Exception:  # noqa: BLE001
            pass
    sheet.PageStyle = new_name
    return style


def apply_settings(style, s):
    width, height = s["width"], s["height"]
    if s["paper"] in PAPERS:
        width, height = PAPERS[s["paper"]][2:4]
    short, long_side = sorted((width, height))
    width, height = (long_side, short) if s["landscape"] else (short, long_side)
    style.IsLandscape = s["landscape"]
    style.Width = width
    style.Height = height
    style.LeftMargin = round(s["left"] * 1000)
    style.RightMargin = round(s["right"] * 1000)
    style.TopMargin = round(s["top"] * 1000)
    style.BottomMargin = round(s["bottom"] * 1000)
    style.CenterHorizontally = s["center_h"]
    style.CenterVertically = s["center_v"]
    style.PrintGrid = s["grid"]
    style.PrintHeaders = s["headings"]
    style.HeaderIsOn = s["header_footer"]
    style.FooterIsOn = s["header_footer"]
    style.ScaleToPages = 0
    style.ScaleToPagesX = 0
    style.ScaleToPagesY = 0
    mode = s["scaling"]
    if mode == "none":
        style.PageScale = 100
    elif mode == "percent":
        style.PageScale = s["percent"]
    else:
        x, y = {"one": (1, 1), "width": (1, 0), "height": (0, 1),
                "pages": (s["fit_x"], s["fit_y"])}[mode]
        style.ScaleToPagesX = x
        style.ScaleToPagesY = y


def read_settings(style):
    width, height = style.Width, style.Height
    short, long_side = sorted((width, height))
    paper = next((k for k, v in PAPERS.items()
                  if abs(v[2] - short) < 60 and abs(v[3] - long_side) < 60), "custom")
    left, right = style.LeftMargin / 1000, style.RightMargin / 1000
    top, bottom = style.TopMargin / 1000, style.BottomMargin / 1000
    margin = next((k for k, v in MARGINS.items() if len(v) > 2
                   and all(abs(a - b) < 0.03 for a, b in zip(v[2:], (left, right, top, bottom)))),
                  "custom")
    x, y = style.ScaleToPagesX, style.ScaleToPagesY
    percent = style.PageScale or 100
    if (x, y) == (1, 1):
        scaling = "one"
    elif (x, y) == (1, 0):
        scaling = "width"
    elif (x, y) == (0, 1):
        scaling = "height"
    elif x or y:
        scaling = "pages"
    elif style.ScaleToPages:
        scaling, x, y = "pages", 1, style.ScaleToPages
    else:
        scaling = "none" if percent == 100 else "percent"
    return {
        "scope": "active", "paper": paper, "width": width, "height": height,
        "landscape": bool(style.IsLandscape), "margin": margin, "left": left, "right": right,
        "top": top, "bottom": bottom, "scaling": scaling, "percent": percent,
        "fit_x": x or 1, "fit_y": y, "center_h": bool(style.CenterHorizontally),
        "center_v": bool(style.CenterVertically), "grid": bool(style.PrintGrid),
        "headings": bool(style.PrintHeaders),
        "header_footer": bool(style.HeaderIsOn or style.FooterIsOn),
    }


# ---------------------------------------------------------------- ouvintes UNO

class Callback(unohelper.Base, XCallback):
    """Executa uma função na linha principal do LibreOffice."""
    def __init__(self, fn):
        self.fn = fn

    def notify(self, _data):
        self.fn()


class DocumentWatcher(unohelper.Base, XDocumentEventListener):
    """Fecha o painel quando a planilha é fechada."""
    def __init__(self, panel):
        self.panel = panel

    def documentEventOccured(self, event):
        if event.EventName in ("OnPrepareUnload", "OnUnload"):
            self.panel.close()

    def disposing(self, _event):
        self.panel.later(self.panel.close)


class Listener(unohelper.Base, XActionListener, XItemListener, XTextListener,
               XWindowListener, XTopWindowListener):
    def __init__(self, panel):
        self.panel = panel

    def _name(self, event):
        try:
            return event.Source.Model.Name
        except Exception:  # noqa: BLE001
            return ""

    def actionPerformed(self, event):
        self.panel.on_action(event.ActionCommand)

    def itemStateChanged(self, event):
        self.panel.on_change(self._name(event))

    def textChanged(self, event):
        self.panel.on_change(self._name(event))

    def windowResized(self, _event):
        self.panel.relayout()

    def windowClosing(self, _event):
        self.panel.later(self.panel.close)

    def windowMoved(self, _event): pass
    def windowShown(self, _event): pass
    def windowHidden(self, _event): pass
    def windowOpened(self, _event): pass
    def windowClosed(self, _event): pass
    def windowMinimized(self, _event): pass
    def windowNormalized(self, _event): pass
    def windowActivated(self, _event): pass
    def windowDeactivated(self, _event): pass

    def disposing(self, _event): pass


# ---------------------------------------------------------------- painel

class Panel:
    def __init__(self, ctx, doc):
        self.ctx = ctx
        self.smgr = ctx.ServiceManager
        self.doc = doc
        self.key = str(doc.RuntimeUID)
        self.desktop = self.smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        self.toolkit = self.smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        self.async_call = self.smgr.createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
        self.work = tempfile.mkdtemp(prefix="painel-impressao-")
        self.listener = Listener(self)
        self.watcher = DocumentWatcher(self)
        self.copy = self.pdf_doc = None
        self.settings = {}
        self.active_sheet = ""
        self.selection = []
        self.options = {k: list(v) for k, v in CHOICES.items()}
        self.values = {}
        self.printers = []
        self.page = 0
        self.page_count = 0
        self.counter = 0
        self.files = []
        self.loading = True
        self.closed = False
        self.timer = None
        self.dialog = None
        self.cups = bool(tool("lp"))

    # ---- linha principal

    def later(self, fn):
        """Agenda fn na linha principal (seguro a partir de outras threads)."""
        if not self.closed:
            self.async_call.addCallback(Callback(fn), None)

    def message(self, text, kind="infobox", buttons=1):
        peer = self.dialog.getPeer() if self.dialog else None
        box = self.toolkit.createMessageBox(peer, uno.Enum("com.sun.star.awt.MessageBoxType",
                                                            {"infobox": "INFOBOX", "warningbox": "WARNINGBOX",
                                                             "querybox": "QUERYBOX"}[kind]),
                                            buttons, "Painel de impressão", text)
        return box.execute()

    # ---- montagem da janela

    def add(self, kind, name, **props):
        model = self.model.createInstance(f"com.sun.star.awt.UnoControl{kind}Model")
        model.Name = name
        for key, value in props.items():
            setattr(model, key, value)
        self.model.insertByName(name, model)
        return model

    def build(self):
        self.model = self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", self.ctx)
        self.model.Title = "Imprimir"
        self.model.Width, self.model.Height = WIDTH, HEIGHT
        self.model.Sizeable = True
        self.model.Closeable = True
        self.model.Moveable = True

        def label(name, text, bold=False, size=0, align=0):
            props = {"Label": text, "Align": align, "VerticalAlign": uno.Enum("com.sun.star.style.VerticalAlignment", "MIDDLE")}
            if bold:
                props["FontWeight"] = 150.0
            if size:
                props["FontHeight"] = float(size)
            self.add("FixedText", name, **props)

        def number(name, low, high, step=1.0, decimals=0, value=0.0):
            self.add("NumericField", name, ValueMin=float(low), ValueMax=float(high), ValueStep=float(step),
                     DecimalAccuracy=decimals, Spin=True, StrictFormat=True, Value=float(value))

        def listbox(name):
            self.add("ListBox", name, Dropdown=True, LineCount=12,
                     StringItemList=tuple(t for _, t, _ in self.options[name]))

        def button(name, text, command, **extra):
            self.add("Button", name, Label=text, **extra)
            self.commands[name] = command

        self.commands = {}
        label("title", "Imprimir", bold=True, size=16)
        button("print", "Imprimir", "print", DefaultButton=True, MultiLine=True, FontWeight=150.0)
        label("copies_label", "Cópias:", align=2)
        number("copies", 1, 999, value=1)
        label("printer_label", "Impressora", bold=True, size=11)
        self.add("ListBox", "printer", Dropdown=True, LineCount=12)
        button("manage", "Gerenciar impressoras…", "manage")
        label("settings_label", "Configurações", bold=True, size=11)
        listbox("scope")
        label("pages_label", "Páginas:")
        self.add("Edit", "page_from", HelpText="Primeira página (vazio = desde a primeira)")
        label("pages_to", "até", align=1)
        self.add("Edit", "page_to", HelpText="Última página (vazio = até a última)")
        listbox("sides")
        listbox("collate")
        listbox("orientation")
        listbox("paper")
        listbox("margins")
        for key, text in (("left", "Esquerda"), ("right", "Direita"), ("top", "Superior"), ("bottom", "Inferior")):
            label(f"{key}_label", text)
            number(key, 0, 10, 0.1, 2)
            label(f"{key}_unit", "cm")
        listbox("scaling")
        label("percent_label", "Escala:")
        number("percent", 10, 400, 5, value=100)
        label("percent_unit", "%")
        label("fit_x_label", "Largura:")
        number("fit_x", 0, 99, value=1)
        label("fit_y_label", "Altura:")
        number("fit_y", 0, 99, value=0)
        for name in ("fit_x", "fit_x_label", "fit_y", "fit_y_label"):
            self.model.getByName(name).HelpText = "Número de páginas (0 = automático)"
        for name, text in (("center_h", "Centralizar na horizontal"), ("center_v", "Centralizar na vertical"),
                           ("grid", "Imprimir linhas de grade"), ("headings", "Imprimir títulos de linha e coluna"),
                           ("header_footer", "Cabeçalho e rodapé")):
            self.add("CheckBox", name, Label=text, State=0)
        button("apply", "Aplicar na planilha", "apply", HelpText="Guarda estes ajustes na planilha sem imprimir")
        button("version", f"Versão {VERSION} · Atualizações", "version",
               HelpText="Procura uma versão nova do painel")

        self.add("ImageControl", "preview", ScaleImage=True, ScaleMode=1, Border=0,
                 BackgroundColor=0x7F7F7F)
        button("prev", "<", "prev", HelpText="Página anterior")
        label("page_label", "Página 0 de 0", align=1)
        button("next", ">", "next", HelpText="Próxima página")
        label("status", "Abrindo a planilha…", align=1)
        button("reload", "Atualizar dados", "reload", HelpText="Recarrega a prévia depois de editar a planilha")
        button("close", "Fechar", "close")

        self.dialog = self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", self.ctx)
        self.dialog.setModel(self.model)
        for name, command in self.commands.items():
            control = self.dialog.getControl(name)
            control.setActionCommand(command)
            control.addActionListener(self.listener)
        for name in ("printer", *CHOICES):
            self.dialog.getControl(name).addItemListener(self.listener)
        for name in ("center_h", "center_v", "grid", "headings", "header_footer"):
            self.dialog.getControl(name).addItemListener(self.listener)
        for name in ("left", "right", "top", "bottom", "percent", "fit_x", "fit_y"):
            self.dialog.getControl(name).addTextListener(self.listener)
        self.layout_left()
        self.relayout()
        parent = self.doc.CurrentController.Frame.ContainerWindow
        self.dialog.createPeer(self.toolkit, parent)
        self.dialog.addWindowListener(self.listener)
        self.dialog.addTopWindowListener(self.listener)

    def place(self, name, x, y, w, h):
        model = self.model.getByName(name)
        model.PositionX, model.PositionY, model.Width, model.Height = int(x), int(y), int(w), int(h)

    def layout_left(self):
        """Empilha as linhas da coluna esquerda, pulando as que estão ocultas."""
        W = LEFT_W
        margin_custom = self.values.get("margins") == "custom"
        scaling = self.values.get("scaling")
        rows = [
            (True, 14, [("title", 0, 70, 0), ("version", 72, W - 72, 1)]),
            (True, 26, [("print", 0, 74, 0), ("copies_label", 80, 36, 7), ("copies", 120, W - 120, 7)]),
            (True, 12, [("printer_label", 0, 60, 0), ("manage", W - 96, 96, 0)]),
            (True, 13, [("printer", 0, W, 0)]),
            (True, 11, [("settings_label", 0, W, 0)]),
            (True, 13, [("scope", 0, W, 0)]),
            (True, 12, [("pages_label", 0, 36, 0), ("page_from", 40, 50, 0), ("pages_to", 92, 22, 0),
                        ("page_to", 116, 50, 0)]),
            (True, 13, [("sides", 0, W, 0)]),
            (True, 13, [("collate", 0, W, 0)]),
            (True, 13, [("orientation", 0, W, 0)]),
            (True, 13, [("paper", 0, W, 0)]),
            (True, 13, [("margins", 0, W, 0)]),
            (margin_custom, 12, [("left_label", 0, 36, 0), ("left", 38, 40, 0), ("left_unit", 80, 8, 0),
                                 ("right_label", 92, 36, 0), ("right", 128, 38, 0), ("right_unit", 168, 8, 0)]),
            (margin_custom, 12, [("top_label", 0, 36, 0), ("top", 38, 40, 0), ("top_unit", 80, 8, 0),
                                 ("bottom_label", 92, 36, 0), ("bottom", 128, 38, 0), ("bottom_unit", 168, 8, 0)]),
            (True, 13, [("scaling", 0, W, 0)]),
            (scaling == "percent", 12, [("percent_label", 0, 36, 0), ("percent", 38, 50, 0),
                                        ("percent_unit", 90, 10, 0)]),
            (scaling == "pages", 12, [("fit_x_label", 0, 36, 0), ("fit_x", 38, 36, 0),
                                      ("fit_y_label", 82, 30, 0), ("fit_y", 114, 36, 0)]),
            (True, 9, [("center_h", 0, W, 0)]),
            (True, 9, [("center_v", 0, W, 0)]),
            (True, 9, [("grid", 0, W, 0)]),
            (True, 9, [("headings", 0, W, 0)]),
            (True, 9, [("header_footer", 0, W, 0)]),
        ]
        y = 6
        for visible, height, items in rows:
            for name, x, w, dy in items:
                if visible:
                    self.place(name, LEFT_X + x, y + dy, w, height - dy * 2)
                self.model.getByName(name).EnableVisible = visible
                if self.dialog is not None and self.dialog.getPeer():
                    self.dialog.getControl(name).setVisible(visible)
            if visible:
                y += height + GAP

    def relayout(self):
        """Ajusta a prévia e a barra inferior ao tamanho atual da janela."""
        width, height = self.model.Width, self.model.Height
        if self.dialog is not None and self.dialog.getPeer():
            size = self.dialog.getPosSize()
            logic = self.dialog.convertSizeToLogic(uno.createUnoStruct("com.sun.star.awt.Size", size.Width, size.Height),
                                                   uno.getConstantByName("com.sun.star.util.MeasureUnit.APPFONT"))
            width, height = max(logic.Width, 400), max(logic.Height, 300)
        pw = width - PREVIEW_X - 6
        self.place("preview", PREVIEW_X, 6, pw, height - BAR_H - 14)
        y = height - BAR_H - 4
        self.place("prev", PREVIEW_X, y, 16, BAR_H - 2)
        self.place("page_label", PREVIEW_X + 18, y, 58, BAR_H - 2)
        self.place("next", PREVIEW_X + 78, y, 16, BAR_H - 2)
        self.place("close", width - 6 - 44, y, 44, BAR_H - 2)
        self.place("reload", width - 6 - 44 - 4 - 62, y, 62, BAR_H - 2)
        self.place("apply", width - 6 - 44 - 4 - 62 - 4 - 74, y, 74, BAR_H - 2)
        self.place("status", PREVIEW_X + 98, y, max(10, pw - 98 - 192), BAR_H - 2)
        if self.page_count and self.dialog is not None and self.dialog.getPeer():
            self.schedule_page()

    # ---- valores dos controles

    def ctl(self, name):
        return self.dialog.getControl(name)

    def set_choice(self, name, value):
        values = [v for v, _, _ in self.options[name]]
        if value not in values:
            value = values[0]
        self.values[name] = value
        index = values.index(value)
        self.ctl(name).selectItemPos(index, True)
        self.model.getByName(name).HelpText = self.options[name][index][2]

    def read_choice(self, name):
        pos = self.ctl(name).getSelectedItemPos()
        if 0 <= pos < len(self.options[name]):
            self.values[name] = self.options[name][pos][0]
            self.model.getByName(name).HelpText = self.options[name][pos][2]
        return self.values.get(name)

    def set_options(self, name, options):
        self.options[name] = options
        self.model.getByName(name).StringItemList = tuple(t for _, t, _ in options)

    def num(self, name):
        return self.model.getByName(name).Value or 0.0

    def set_num(self, name, value):
        self.model.getByName(name).Value = float(value)

    def checked(self, name):
        return self.model.getByName(name).State == 1

    def set_checked(self, name, on):
        self.model.getByName(name).State = 1 if on else 0

    def set_status(self, text):
        self.model.getByName("status").Label = text

    def enable(self, on):
        for name in ("printer", "scope", "page_from", "page_to", "sides", "collate", "orientation", "paper",
                     "margins", "scaling", "copies", "apply", "reload", "center_h", "center_v", "grid",
                     "headings", "header_footer", "left", "right", "top", "bottom", "percent", "fit_x",
                     "fit_y", "print"):
            self.model.getByName(name).Enabled = on
        if on:
            self.model.getByName("print").Enabled = bool(self.values.get("printer"))

    # ---- ciclo de vida

    def open(self):
        self.build()
        self.loading = True
        self.enable(False)
        self.load_printers()
        self.dialog.setVisible(True)
        self.doc.addDocumentEventListener(self.watcher)
        self.later(self.load)

    def load(self):
        try:
            controller = self.doc.CurrentController
            sheet = controller.ActiveSheet
            self.active_sheet = sheet.Name
            self.capture_selection()
            family = self.doc.StyleFamilies.getByName("PageStyles")
            s = read_settings(family.getByName(sheet.PageStyle))
            self.settings = dict(s)
            self.open_copy()
        except Exception as error:  # noqa: BLE001
            self.message(f"Não foi possível abrir o painel: {error}", "warningbox")
            self.close()
            return
        title = self.doc.Title or "Planilha"
        self.model.Title = f"Imprimir - {title} ({self.active_sheet})"
        self.title_text = title
        scope = [o for o in CHOICES["scope"] if o[0] != "selection" or self.selection]
        self.set_options("scope", scope)
        if s["paper"] == "custom":
            short, long_side = sorted((s["width"], s["height"]))
            size = f"{short / 1000:.1f} x {long_side / 1000:.1f} cm".replace(".", ",")
            self.set_options("paper", CHOICES["paper"] + [("custom", f"Tamanho atual  ({size})", size)])
        self.set_choice("scope", "active")
        self.set_choice("collate", True)
        self.set_choice("orientation", s["landscape"])
        self.set_choice("paper", s["paper"])
        self.set_choice("margins", s["margin"])
        self.set_choice("scaling", s["scaling"])
        for key in ("left", "right", "top", "bottom", "percent", "fit_x", "fit_y"):
            self.set_num(key, s[key])
        for key in ("center_h", "center_v", "grid", "headings", "header_footer"):
            self.set_checked(key, s[key])
        self.layout_left()
        self.loading = False
        self.enable(True)
        self.render_now()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer:
            self.timer.cancel()
        _panels.pop(self.key, None)
        for document in (self.pdf_doc, self.copy):
            try:
                if document is not None:
                    document.close(True)
            except Exception:  # noqa: BLE001
                pass
        self.pdf_doc = self.copy = None
        try:
            self.doc.removeDocumentEventListener(self.watcher)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.dialog.setVisible(False)
            self.dialog.dispose()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.work, ignore_errors=True)

    def to_front(self):
        try:
            self.dialog.setVisible(True)
            self.dialog.toFront()
        except Exception:  # noqa: BLE001
            self.dialog.setFocus()

    # ---- impressoras

    def load_printers(self):
        printers, default = list_printers(self.ctx)
        self.printers = printers
        options = [(n, n.replace("_", " ") + (f"  —  {state}" if state else ""), state) for n, state in printers]
        if not options:
            options = [("", "Nenhuma impressora instalada", "Instale uma impressora no sistema")]
        self.options["printer"] = options
        self.model.getByName("printer").StringItemList = tuple(t for _, t, _ in options)
        names = [n for n, _ in printers]
        self.set_choice("printer", default if default in names else options[0][0])
        self.on_printer_changed()

    def on_printer_changed(self):
        name = self.values.get("printer")
        duplex = bool(name) and printer_supports_duplex(name)
        current = self.values.get("sides", "one-sided")
        sides = CHOICES["sides"] if duplex else CHOICES["sides"][:1]
        self.set_options("sides", sides)
        self.set_choice("sides", current if duplex else "one-sided")
        if not self.cups:
            self.model.getByName("sides").HelpText = "Frente e verso: use as preferências da impressora no sistema"

    # ---- eventos

    def on_action(self, command):
        if command == "close":
            self.later(self.close)
        elif command == "manage":
            open_printer_settings()
        elif command == "version":
            self.check_updates()
        elif command == "prev" and self.page > 0:
            self.page -= 1
            self.show_page()
        elif command == "next" and self.page < self.page_count - 1:
            self.page += 1
            self.show_page()
        elif self.loading:
            return
        elif command == "print":
            self.print_document()
        elif command == "apply":
            self.guard(lambda: (self.apply_to_document(self.collect()),
                                self.set_status("Ajustes salvos na planilha. Salve o arquivo para mantê-los.")))
        elif command == "reload":
            self.guard(lambda: (self.capture_selection(), self.open_copy(), self.render_now()))

    def on_change(self, name):
        if self.loading or self.closed:
            return
        if name == "printer":
            self.read_choice("printer")
            self.on_printer_changed()
            self.enable(True)
            return
        if name in CHOICES:
            self.read_choice(name)
            if name == "margins" and self.values["margins"] in MARGINS and self.values["margins"] != "custom":
                for key, value in zip(("left", "right", "top", "bottom"), MARGINS[self.values["margins"]][2:]):
                    self.set_num(key, value)
            if name in ("margins", "scaling"):
                self.layout_left()
            if name in ("sides", "collate"):
                return  # não muda a prévia
        self.schedule()

    def schedule(self):
        self.set_status("Atualizando…")
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(0.35, lambda: self.later(self.render_now))
        self.timer.daemon = True
        self.timer.start()

    def schedule_page(self):
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(0.25, lambda: self.later(self.show_page))
        self.timer.daemon = True
        self.timer.start()

    def guard(self, fn):
        try:
            fn()
        except Exception as error:  # noqa: BLE001
            self.set_status("Falhou")
            self.message(str(error), "warningbox")

    # ---- planilha e prévia

    def capture_selection(self):
        self.selection = []
        selected = self.doc.CurrentController.getSelection()
        if selected.supportsService("com.sun.star.sheet.SheetCellRanges"):
            self.selection = list(selected.getRangeAddresses())
        elif selected.supportsService("com.sun.star.sheet.SheetCellRange") \
                and not selected.supportsService("com.sun.star.sheet.SheetCell"):
            self.selection = [selected.getRangeAddress()]

    def open_copy(self):
        if self.copy is not None:
            try:
                self.copy.close(True)
            except Exception:  # noqa: BLE001
                pass
        source = os.path.join(self.work, "copia.ods")
        self.doc.storeToURL(uno.systemPathToFileUrl(source), (prop("FilterName", "calc8"),))
        self.copy = self.desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(source), "_blank", 0,
            (prop("Hidden", True), prop("ReadOnly", False), prop("PickListEntry", False),
             prop("MacroExecutionMode", 0), prop("UpdateDocMode", 0)))

    def collect(self):
        s = dict(self.settings)
        for name in ("scope", "paper", "margins", "scaling"):
            self.read_choice(name)
        s.update({
            "scope": self.values["scope"], "paper": self.values["paper"],
            "landscape": self.read_choice("orientation"), "margin": self.values["margins"],
            "scaling": self.values["scaling"], "percent": int(self.num("percent")) or 100,
            "fit_x": int(self.num("fit_x")), "fit_y": int(self.num("fit_y")),
        })
        for key in ("left", "right", "top", "bottom"):
            s[key] = self.num(key)
        for key in ("center_h", "center_v", "grid", "headings", "header_footer"):
            s[key] = self.checked(key)
        if s["scaling"] == "pages" and not (s["fit_x"] or s["fit_y"]):
            s["fit_x"] = 1
        return s

    def export_target(self, s):
        if s["scope"] == "all":
            return None
        sheet = self.copy.Sheets.getByName(self.active_sheet)
        addresses = None
        if s["scope"] == "selection" and self.selection:
            addresses = self.selection
        else:
            areas = sheet.getPrintAreas()
            if areas:
                addresses = list(areas)
        if addresses:
            ranges = self.copy.createInstance("com.sun.star.sheet.SheetCellRanges")
            ranges.addRangeAddresses(tuple(addresses), False)
            return ranges
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        end = cursor.RangeAddress
        return sheet.getCellRangeByPosition(0, 0, end.EndColumn, end.EndRow)

    def new_file(self, extension):
        self.counter += 1
        path = os.path.join(self.work, f"previa_{self.counter}.{extension}")
        self.files.append(path)
        while len(self.files) > 6:
            try:
                os.unlink(self.files.pop(0))
            except OSError:
                pass
        return path

    def render(self, s):
        """Gera o PDF da cópia com os ajustes e abre no Draw para virar imagem."""
        sheets = self.copy.Sheets
        if s["scope"] == "all":
            names = {sheets.getByIndex(i).PageStyle for i in range(sheets.Count)}
        else:
            names = {sheets.getByName(self.active_sheet).PageStyle}
        family = self.copy.StyleFamilies.getByName("PageStyles")
        for name in names:
            apply_settings(family.getByName(name), s)
        options = [prop("UseLosslessCompression", True), prop("ReduceImageResolution", False),
                   prop("ExportBookmarks", False), prop("ExportNotes", False)]
        target = self.export_target(s)
        if target is not None:
            options.append(prop("Selection", target))
        path = self.new_file("pdf")
        data = uno.Any("[]com.sun.star.beans.PropertyValue", tuple(options))
        self.copy.storeToURL(uno.systemPathToFileUrl(path),
                             (prop("FilterName", "calc_pdf_Export"), prop("FilterData", data)))
        if self.pdf_doc is not None:
            try:
                self.pdf_doc.close(True)
            except Exception:  # noqa: BLE001
                pass
        self.pdf_doc = self.desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(path), "_blank", 0,
            (prop("Hidden", True), prop("ReadOnly", True), prop("FilterName", "draw_pdf_import"),
             prop("PickListEntry", False)))
        return path

    def render_now(self):
        if self.closed or self.loading:
            return
        try:
            self.render(self.collect())
        except Exception as error:  # noqa: BLE001
            self.set_status("Falhou")
            self.message(f"Não foi possível gerar a prévia: {error}", "warningbox")
            return
        self.page_count = self.pdf_doc.DrawPages.Count
        self.page = min(self.page, max(0, self.page_count - 1))
        pages = f"{self.page_count} página{'s' if self.page_count != 1 else ''}"
        self.set_status(pages)
        self.model.getByName("print").Label = f"Imprimir\n{pages}"
        self.show_page()

    def show_page(self):
        if self.closed or self.pdf_doc is None:
            return
        count = self.page_count
        self.model.getByName("page_label").Label = f"Página {self.page + 1 if count else 0} de {count}"
        self.model.getByName("prev").Enabled = self.page > 0
        self.model.getByName("next").Enabled = self.page < count - 1
        if not count:
            self.model.getByName("preview").ImageURL = ""
            return
        page = self.pdf_doc.DrawPages.getByIndex(self.page)
        box = self.ctl("preview").getPosSize()
        scale = min(box.Width / max(page.Width, 1), box.Height / max(page.Height, 1)) * 1.5
        width = max(50, min(2400, int(page.Width * scale)))
        height = max(50, min(2400, int(page.Height * scale)))
        path = self.new_file("png")
        exporter = self.smgr.createInstanceWithContext("com.sun.star.drawing.GraphicExportFilter", self.ctx)
        exporter.setSourceDocument(page)
        data = uno.Any("[]com.sun.star.beans.PropertyValue", (prop("PixelWidth", width), prop("PixelHeight", height)))
        exporter.filter((prop("URL", uno.systemPathToFileUrl(path)), prop("MediaType", "image/png"),
                         prop("FilterData", data)))
        self.model.getByName("preview").ImageURL = uno.systemPathToFileUrl(path)

    def apply_to_document(self, s):
        sheets = self.doc.Sheets
        family = self.doc.StyleFamilies.getByName("PageStyles")
        if s["scope"] == "all":
            for name in {sheets.getByIndex(i).PageStyle for i in range(sheets.Count)}:
                apply_settings(family.getByName(name), s)
        else:
            apply_settings(own_page_style(self.doc, sheets.getByName(self.active_sheet)), s)
        self.doc.setModified(True)

    # ---- impressão

    def page_range(self):
        def parse(name):
            text = self.model.getByName(name).Text.strip()
            return int(text) if text.isdigit() else 0
        first, last = parse("page_from"), parse("page_to")
        total = self.page_count
        if first and last and first > last:
            raise ValueError("A página inicial é maior que a final.")
        if first > total or last > total:
            raise ValueError(f"O documento tem {total} página(s).")
        return f"{first or 1}-{last or total}" if (first or last) else ""

    def print_document(self):
        printer = self.values.get("printer")
        if not printer:
            return
        try:
            pages = self.page_range()
        except ValueError as error:
            self.message(str(error), "warningbox")
            return
        s = self.collect()
        self.set_status("Enviando…")
        try:
            path = self.render(s)
            self.apply_to_document(s)
            copies = int(self.num("copies")) or 1
            collate = self.read_choice("collate")
            if self.cups:
                self.print_cups(path, printer, copies, collate, pages, s)
            else:
                self.print_office(printer, copies, collate, pages)
        except Exception as error:  # noqa: BLE001
            self.set_status("Falhou")
            self.message(f"Não foi possível imprimir: {error}", "warningbox")
            return
        self.set_status(f"Enviado para {printer.replace('_', ' ')}.")

    def print_cups(self, path, printer, copies, collate, pages, s):
        args = [tool("lp"), "-d", printer, "-n", str(copies), "-t", getattr(self, "title_text", "Planilha"),
                "-o", f"sides={self.read_choice('sides')}",
                "-o", f"collate={'true' if collate else 'false'}"]
        if s["paper"] in PAPERS:
            args += ["-o", f"media={PAPERS[s['paper']][4]}"]
        if pages:
            args += ["-P", pages]
        args += ["--", path]
        result = subprocess.run(args, env=clean_env(), capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "A impressora recusou o trabalho.")

    def print_office(self, printer, copies, collate, pages):
        """Imprime o PDF da prévia pelo próprio LibreOffice (Windows ou sem CUPS)."""
        self.pdf_doc.setPrinter((prop("Name", printer),))
        options = [prop("CopyCount", copies), prop("Collate", collate), prop("Wait", True)]
        if pages:
            options.append(prop("Pages", pages))
        self.pdf_doc.print(tuple(options))

    # ---- atualizações

    def check_updates(self):
        self.model.getByName("version").Enabled = False
        self.model.getByName("version").Label = "Procurando…"

        def work():
            try:
                provider = self.smgr.createInstanceWithContext(
                    "com.sun.star.deployment.UpdateInformationProvider", self.ctx)
                entries = provider.getUpdateInformation((UPDATE_FEED,), EXTENSION_ID)
                latest = ""
                for entry in entries:
                    nodes = entry.getElementsByTagName("version")
                    if nodes.getLength():
                        latest = nodes.item(0).getAttribute("value")
                result = ("newer" if version_tuple(latest) > version_tuple(VERSION) else "current", latest)
            except Exception:  # noqa: BLE001
                result = ("error", "")
            self.later(lambda: self.update_result(*result))

        threading.Thread(target=work, daemon=True).start()

    def update_result(self, kind, latest):
        button = self.model.getByName("version")
        button.Enabled = True
        button.Label = f"Versão {VERSION} · Atualizações"
        if kind == "current":
            button.Label = f"Versão {VERSION} · Atualizado"
        elif kind == "error":
            self.message("Não foi possível consultar as atualizações. Confira a internet.", "warningbox")
        else:
            answer = self.message(
                f"Há uma versão nova: {latest}.\nVersão instalada: {VERSION}.\n\n"
                "Abrir o Gerenciador de extensões? Lá, clique em \"Verificar atualizações\" e depois em "
                "\"Instalar\".", "querybox", 3)  # 3 = Sim/Não
            if answer == 2:  # Sim
                frame = self.doc.CurrentController.Frame
                helper = self.smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.ctx)
                helper.executeDispatch(frame, ".uno:ExtensionManager", "", 0, ())


# ---------------------------------------------------------------- comandos do menu e da barra

def ajustar_largura(*_args):
    """Faz todas as colunas da aba atual caberem na largura de uma página."""
    doc = XSCRIPTCONTEXT.getDocument()  # noqa: F821 - fornecido pelo LibreOffice
    if not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        return
    controller = doc.CurrentController
    style = own_page_style(doc, controller.ActiveSheet)
    style.ScaleToPages = 0
    style.ScaleToPagesX = 1
    style.ScaleToPagesY = 0
    doc.setModified(True)
    try:
        indicator = controller.Frame.createStatusIndicator()
        indicator.start("Colunas ajustadas à largura de 1 página", 1)
        timer = threading.Timer(4, indicator.end)
        timer.daemon = True
        timer.start()
    except Exception:  # noqa: BLE001
        pass


def abrir_painel(*_args):
    """Abre o painel de impressão com prévia em tempo real."""
    ctx = XSCRIPTCONTEXT.getComponentContext()  # noqa: F821
    doc = XSCRIPTCONTEXT.getDocument()  # noqa: F821
    if not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        return
    key = str(doc.RuntimeUID)
    if key in _panels:
        _panels[key].to_front()
        return
    panel = Panel(ctx, doc)
    _panels[key] = panel
    try:
        panel.open()
    except Exception:
        _panels.pop(key, None)
        raise


g_exportedScripts = (ajustar_largura, abrir_painel)
