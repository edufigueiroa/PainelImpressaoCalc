"""Comandos do menu e da barra de ferramentas do Calc."""
import os
import subprocess
import threading

PANEL = os.path.expanduser("~/.local/share/painel-impressao-calc/panel.py")


def _own_page_style(doc, sheet):
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
        except Exception:
            pass
    sheet.PageStyle = new_name
    return style


def ajustar_largura(*_args):
    """Faz todas as colunas da aba atual caberem na largura de uma página."""
    doc = XSCRIPTCONTEXT.getDocument()
    if not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        return
    controller = doc.CurrentController
    style = _own_page_style(doc, controller.ActiveSheet)
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
    except Exception:
        pass


def abrir_painel(*_args):
    """Abre o painel de impressão com prévia em tempo real."""
    doc = XSCRIPTCONTEXT.getDocument()
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "URE_BOOTSTRAP", "UNO_PATH",
                "QT_PLUGIN_PATH", "SAL_USE_VCLPLUGIN"):
        env.pop(key, None)
    subprocess.Popen(["/usr/bin/python3", PANEL, "--doc", str(doc.RuntimeUID)], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


g_exportedScripts = (ajustar_largura, abrir_painel)
