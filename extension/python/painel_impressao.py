"""Comandos do menu e da barra de ferramentas do Calc."""
import os
import subprocess
import threading

import uno

EXTENSION_ID = "local.painelimpressao.calc"
INSTALLED_PANEL = os.path.expanduser("~/.local/share/painel-impressao-calc/panel.py")


def _version(path):
    try:
        with open(path, encoding="utf-8") as source:
            for line in source:
                if line.startswith("VERSION = "):
                    return tuple(int(part) for part in line.split('"')[1].split("."))
    except (OSError, ValueError, IndexError):
        pass
    return ()


def _panel_path():
    """Usa o painel que vem dentro da extensão, ou o instalado pelo install.sh se for mais novo."""
    candidates = []
    try:
        context = XSCRIPTCONTEXT.getComponentContext()
        provider = context.getByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
        location = provider.getPackageLocation(EXTENSION_ID)
        if location:
            candidates.append(os.path.join(uno.fileUrlToSystemPath(location), "bin", "panel.py"))
    except Exception:
        pass
    candidates.append(INSTALLED_PANEL)
    candidates = [path for path in candidates if os.path.exists(path)]
    return max(candidates, key=_version) if candidates else None


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
    panel = _panel_path()
    if panel is None:
        return
    subprocess.Popen(["/usr/bin/python3", panel, "--doc", str(doc.RuntimeUID)], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


g_exportedScripts = (ajustar_largura, abrir_painel)
