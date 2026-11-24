#!/usr/bin/python3
"""Painel de impressão com prévia em tempo real para o LibreOffice Calc."""
import argparse
import collections
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import json
import urllib.request

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPalette
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QToolButton, QVBoxLayout,
    QWidget,
)

VERSION = "1.1.3"
REPO = "edufigueiroa/PainelImpressaoCalc"
PIPE = f"painel_impressao_calc_{os.getuid()}"
PAPERS = {  # 1/100 mm
    "A4": ("A4", "21 cm x 29,7 cm", 21000, 29700, "A4"),
    "A3": ("A3", "29,7 cm x 42 cm", 29700, 42000, "A3"),
    "A5": ("A5", "14,8 cm x 21 cm", 14800, 21000, "A5"),
    "Letter": ("Carta", "21,59 cm x 27,94 cm", 21590, 27940, "Letter"),
    "Legal": ("Legal", "21,59 cm x 35,56 cm", 21590, 35560, "Legal"),
    "Oficio": ("Ofício", "21,6 cm x 33 cm", 21600, 33000, "Oficio"),
}
MARGINS = {  # cm: esquerda, direita, superior, inferior
    "normal": ("Margens normais", "Esq. 1,78 · Dir. 1,78 · Sup. 1,91 · Inf. 1,91 cm", 1.78, 1.78, 1.91, 1.91),
    "wide": ("Margens largas", "2,54 cm em todos os lados", 2.54, 2.54, 2.54, 2.54),
    "narrow": ("Margens estreitas", "Esq. 0,64 · Dir. 0,64 · Sup. 1,91 · Inf. 1,91 cm", 0.64, 0.64, 1.91, 1.91),
}


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def clean_env():
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "URE_BOOTSTRAP", "UNO_PATH"):
        env.pop(key, None)
    env["LC_ALL"] = "C"
    return env


def command(*args):
    try:
        return subprocess.run(args, env=clean_env(), capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def list_printers():
    default = ""
    line = command("lpstat", "-d")
    if ":" in line:
        default = line.split(":", 1)[1].strip()
    states = {}
    for row in command("lpstat", "-p").splitlines():
        parts = row.split()
        if len(parts) >= 3 and parts[0] == "printer":
            if "disabled" in row:
                states[parts[1]] = "Indisponível"
            elif "printing" in row:
                states[parts[1]] = "Imprimindo"
            else:
                states[parts[1]] = "Pronta"
    names = [n.strip() for n in command("lpstat", "-e").splitlines() if n.strip()]
    names.sort(key=lambda n: (n != default, n.lower()))
    return [(n, states.get(n, "Estado desconhecido")) for n in names], default


def printer_supports_duplex(name):
    for row in command("lpoptions", "-p", name, "-l").splitlines():
        if row.startswith("Duplex/"):
            return "DuplexNoTumble" in row
    return False


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
        except Exception:
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
    mode = s["scaling"]
    style.ScaleToPages = 0
    style.ScaleToPagesX = 0
    style.ScaleToPagesY = 0
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
    margin = next((k for k, v in MARGINS.items()
                   if all(abs(a - b) < 0.03 for a, b in zip(v[2:], (left, right, top, bottom)))),
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


class Engine(QObject):
    """Mantém uma cópia oculta da planilha e gera a prévia sem alterar o original."""
    loaded = Signal(dict, dict)
    ready = Signal(str)
    busy = Signal(bool)
    failed = Signal(str)
    message = Signal(str)
    closed = Signal()

    def __init__(self, doc_uid, work_dir):
        super().__init__()
        self.doc_uid = doc_uid
        self.work = work_dir
        self.jobs = collections.deque()
        self.cv = threading.Condition()
        self.stopping = False
        self.desktop = self.doc = self.copy = None
        self.active_sheet = ""
        self.selection = []
        self.counter = 0
        self.pdfs = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def post(self, kind, settings=None, job=None):
        with self.cv:
            if kind == "render":
                self.jobs = collections.deque(j for j in self.jobs if j[0] != "render")
            self.jobs.append((kind, settings, job))
            self.cv.notify()

    def stop(self):
        with self.cv:
            self.stopping = True
            self.cv.notify()
        self.thread.join(timeout=6)

    def connect(self):
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local)
        url = f"uno:pipe,name={PIPE};urp;StarOffice.ComponentContext"
        try:
            context = resolver.resolve(url)
        except NoConnectException:
            extra = os.environ.get("PAINEL_SOFFICE_ARGS", "").split()
            subprocess.Popen(["soffice", *extra, "--nologo", "--norestore",
                              f"--accept=pipe,name={PIPE};urp;StarOffice.ComponentContext"],
                             env={k: v for k, v in clean_env().items() if k != "LC_ALL"},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = None
            for _ in range(60):
                time.sleep(0.25)
                try:
                    context = resolver.resolve(url)
                    break
                except NoConnectException:
                    pass
            if context is None:
                raise RuntimeError("Não foi possível conectar ao LibreOffice.")
        self.desktop = context.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", context)

    def find_document(self):
        components = self.desktop.Components.createEnumeration()
        while components.hasMoreElements():
            doc = components.nextElement()
            try:
                if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument") \
                        and str(doc.RuntimeUID) == str(self.doc_uid):
                    return doc
            except Exception:
                continue
        current = self.desktop.CurrentComponent
        if current is not None and current.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
            return current
        raise RuntimeError("Abra uma planilha no Calc antes de usar o painel.")

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
            self.close_copy()
        source = os.path.join(self.work, "copia.ods")
        self.doc.storeToURL(uno.systemPathToFileUrl(source),
                            (prop("FilterName", "calc8"),))
        self.copy = self.desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(source), "_blank", 0,
            (prop("Hidden", True), prop("ReadOnly", False), prop("PickListEntry", False),
             prop("MacroExecutionMode", 0), prop("UpdateDocMode", 0)))

    def close_copy(self):
        if self.copy is not None:
            try:
                self.copy.close(True)
            except Exception:
                pass
            self.copy = None

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

    def render(self, s):
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
        self.counter += 1
        path = os.path.join(self.work, f"previa_{self.counter}.pdf")
        data = uno.Any("[]com.sun.star.beans.PropertyValue", tuple(options))
        self.copy.storeToURL(uno.systemPathToFileUrl(path),
                             (prop("FilterName", "calc_pdf_Export"), prop("FilterData", data)))
        self.pdfs.append(path)
        while len(self.pdfs) > 2:
            try:
                os.unlink(self.pdfs.pop(0))
            except OSError:
                pass
        return path

    def apply_to_document(self, s):
        sheets = self.doc.Sheets
        family = self.doc.StyleFamilies.getByName("PageStyles")
        if s["scope"] == "all":
            for name in {sheets.getByIndex(i).PageStyle for i in range(sheets.Count)}:
                apply_settings(family.getByName(name), s)
        else:
            apply_settings(own_page_style(self.doc, sheets.getByName(self.active_sheet)), s)

    def print_pdf(self, path, job):
        args = ["lp", "-d", job["printer"], "-n", str(job["copies"]), "-t", job["title"],
                "-o", f"sides={job['sides']}", "-o", f"collate={'true' if job['collate'] else 'false'}"]
        if job["media"]:
            args += ["-o", f"media={job['media']}"]
        if job["pages"]:
            args += ["-P", job["pages"]]
        args += ["--", path]
        result = subprocess.run(args, env=clean_env(), capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "A impressora recusou o trabalho.")

    def run(self):
        try:
            self.busy.emit(True)
            self.connect()
            self.doc = self.find_document()
            controller = self.doc.CurrentController
            sheet = controller.ActiveSheet
            self.active_sheet = sheet.Name
            self.capture_selection()
            family = self.doc.StyleFamilies.getByName("PageStyles")
            settings = read_settings(family.getByName(sheet.PageStyle))
            self.open_copy()
            title = self.doc.Title or "Planilha"
            self.loaded.emit(settings, {"title": title, "sheet": sheet.Name,
                                        "has_selection": bool(self.selection)})
        except Exception as error:  # noqa: BLE001
            self.busy.emit(False)
            self.failed.emit(f"Não foi possível abrir o painel: {error}")
            return

        while True:
            with self.cv:
                if not self.jobs and not self.stopping:
                    self.cv.wait(2)
                if self.stopping:
                    break
                current = self.jobs.popleft() if self.jobs else None
            if current is None:
                try:
                    self.doc.getURL()
                    self.doc.CurrentController.getFrame()
                except Exception:  # noqa: BLE001
                    self.closed.emit()
                    break
                continue
            kind, settings, job = current
            self.busy.emit(True)
            try:
                if kind == "reload":
                    self.capture_selection()
                    self.open_copy()
                    self.ready.emit(self.render(settings))
                elif kind == "render":
                    self.ready.emit(self.render(settings))
                elif kind == "apply":
                    self.apply_to_document(settings)
                    self.message.emit("Ajustes salvos na planilha. Salve o arquivo para mantê-los.")
                elif kind == "print":
                    path = self.render(settings)
                    self.ready.emit(path)
                    self.apply_to_document(settings)
                    self.print_pdf(path, job)
                    self.message.emit(f"Enviado para {job['printer']}.")
            except Exception as error:  # noqa: BLE001
                self.failed.emit(str(error))
            finally:
                self.busy.emit(False)
        self.close_copy()
        try:
            if self.desktop is not None and self.desktop.Frames.Count == 0:
                self.desktop.terminate()  # não deixa o LibreOffice preso sem janelas
        except Exception:  # noqa: BLE001
            pass


class Updater(QObject):
    """Procura versão nova na página de versões do GitHub e reinstala pelo install.sh do projeto."""
    finished = Signal(str, str)  # tipo (newer, current, error, updated, failed), detalhe

    def __init__(self, repo, folder, version):
        super().__init__()
        self.repo = repo
        self.folder = folder
        self.version = version

    @staticmethod
    def parse(tag):
        return tuple(int(p) for p in tag.lstrip("v").split(".") if p.isdigit())

    def check(self):
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "painel-impressao-calc"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                tag = json.load(response)["tag_name"]
        except (OSError, ValueError, KeyError):
            self.finished.emit("error", "Não foi possível consultar o GitHub. Confira a internet.")
            return
        if self.parse(tag) > self.parse(self.version):
            self.finished.emit("newer", tag)
        else:
            self.finished.emit("current", tag)

    def install(self, tag):
        threading.Thread(target=self._install, args=(tag,), daemon=True).start()

    def _install(self, tag):
        temp = tempfile.mkdtemp(prefix="atualizacao-")
        try:
            source = os.path.join(temp, "src")
            result = subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", tag,
                                     f"https://github.com/{self.repo}.git", source],
                                    capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                self.finished.emit("failed", result.stderr.strip()[-400:] or "Falha ao baixar.")
                return
            result = subprocess.run(["bash", os.path.join(source, "install.sh")], capture_output=True,
                                    text=True, timeout=300, cwd=source)
            if result.returncode != 0:
                self.finished.emit("failed", (result.stderr or result.stdout).strip()[-400:])
                return
            self.finished.emit("updated", tag)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.finished.emit("failed", str(error))
        finally:
            shutil.rmtree(temp, ignore_errors=True)


def update_dialog_handler(parent, updater, label):
    """Liga as respostas do Updater às mensagens da janela."""
    def on_finished(kind, detail):
        label.setEnabled(True)
        if kind == "newer":
            answer = QMessageBox.question(parent, "Atualizações",
                                          f"Há uma versão nova: {detail}.\nVersão instalada: {updater.version}.\n\nAtualizar agora?")
            if answer == QMessageBox.StandardButton.Yes:
                label.setEnabled(False)
                label.setText("Atualizando…")
                updater.install(detail)
            else:
                label.setText(f"Versão {updater.version} · Verificar atualizações")
        elif kind == "current":
            label.setText(f"Versão {updater.version} · Você já tem a versão mais nova")
        elif kind == "updated":
            label.setText(f"Atualizado para {detail}")
            QMessageBox.information(parent, "Atualizações", f"Atualizado para {detail}. Feche e abra de novo para usar a versão nova.")
        else:
            label.setText(f"Versão {updater.version} · Verificar atualizações")
            QMessageBox.warning(parent, "Atualizações", detail)
    updater.finished.connect(on_finished)


class Choice(QPushButton):
    """Botão de duas linhas com menu, no estilo do painel de impressão do Excel."""
    changed = Signal(object)

    def __init__(self, icon_names, options):
        super().__init__()
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 28, 4)
        self.icon_label = QLabel()
        self.icon_label.setPixmap(self.find_icon(icon_names).pixmap(28, 28))
        row.addWidget(self.icon_label)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title = QLabel()
        self.subtitle = QLabel()
        font = self.subtitle.font()
        font.setPointSizeF(font.pointSizeF() * 0.86)
        self.subtitle.setFont(font)
        self.subtitle.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        row.addLayout(text, 1)
        for child in (self.icon_label, self.title, self.subtitle):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            child.setMinimumWidth(0)
        self.popup = QMenu(self)
        self.setMenu(self.popup)
        self.group = QActionGroup(self)
        self.value = None
        self.options = {}
        self.set_options(options)

    @staticmethod
    def find_icon(names):
        for name in names:
            if QIcon.hasThemeIcon(name):
                return QIcon.fromTheme(name)
        return QIcon()

    def set_options(self, options):
        self.popup.clear()
        for action in self.group.actions():
            self.group.removeAction(action)
        self.options = {}
        for value, title, subtitle in options:
            action = QAction(f"{title}  —  {subtitle}" if subtitle else title, self, checkable=True)
            action.setData(value)
            action.triggered.connect(lambda _=False, v=value: self.pick(v))
            self.group.addAction(action)
            self.popup.addAction(action)
            self.options[value] = (title, subtitle, action)

    def set_value(self, value):
        if value not in self.options:
            value = next(iter(self.options))
        self.value = value
        title, subtitle, action = self.options[value]
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.subtitle.setVisible(bool(subtitle))
        action.setChecked(True)

    def pick(self, value):
        if value != self.value:
            self.set_value(value)
            self.changed.emit(value)


class Preview(QWidget):
    def __init__(self):
        super().__init__()
        self.pdf = None
        self.page = 0
        self.cache_key = None
        self.cache = None
        self.setMinimumSize(420, 420)

    def show_page(self, pdf, page):
        self.pdf = pdf
        self.page = page
        self.cache_key = None
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Mid))
        if not self.pdf or self.pdf.pageCount() == 0:
            return
        point = self.pdf.pagePointSize(self.page)
        margin = 18
        scale = min((self.width() - margin * 2) / point.width(),
                    (self.height() - margin * 2) / point.height())
        width, height = point.width() * scale, point.height() * scale
        target = QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)
        ratio = self.devicePixelRatioF()
        pixels = QSize(max(1, round(width * ratio)), max(1, round(height * ratio)))
        key = (id(self.pdf), self.page, pixels.width(), pixels.height())
        if key != self.cache_key:
            self.cache = self.pdf.render(self.page, pixels)
            self.cache_key = key
        painter.fillRect(target.translated(QPointF(3, 3)), QColor(0, 0, 0, 80))
        painter.fillRect(target, QColor("white"))
        painter.drawImage(target, self.cache)
        painter.setPen(QColor(0, 0, 0, 120))
        painter.drawRect(target)


class Window(QWidget):
    def __init__(self, doc_uid, work_dir):
        super().__init__()
        self.work = work_dir
        self.ready_to_render = False
        self.settings = {}
        self.title_text = "Planilha"
        self.pdf = None
        self.old_pdf = None
        self.printers = []
        self.setWindowTitle("Imprimir")
        self.resize(1120, 780)
        self.debounce = QTimer(self, singleShot=True, interval=180, timeout=self.request_render)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.build_settings())
        outer.addWidget(self.build_preview(), 1)

        self.engine = Engine(doc_uid, work_dir)
        self.engine.loaded.connect(self.on_loaded)
        self.engine.ready.connect(self.on_ready)
        self.engine.busy.connect(self.on_busy)
        self.engine.failed.connect(self.on_failed)
        self.engine.message.connect(self.status.setText)
        self.engine.closed.connect(self.close)
        self.set_controls_enabled(False)
        self.status.setText("Abrindo a planilha…")
        self.load_printers()
        self.engine.start()

    def heading(self, text):
        label = QLabel(text)
        font = label.font()
        font.setPointSizeF(font.pointSizeF() * 1.35)
        label.setFont(font)
        return label

    def build_settings(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(370)
        body = QWidget()
        self.left = QVBoxLayout(body)
        self.left.setContentsMargins(16, 16, 16, 16)
        self.left.setSpacing(7)
        scroll.setWidget(body)

        big = self.heading("Imprimir")
        font = big.font()
        font.setPointSizeF(font.pointSizeF() * 1.45)
        big.setFont(font)
        self.left.addWidget(big)

        top = QHBoxLayout()
        self.print_button = QPushButton(QIcon.fromTheme("document-print"), "Imprimir")
        self.print_button.setIconSize(QSize(26, 26))
        self.print_button.setMinimumSize(120, 58)
        self.print_button.setDefault(True)
        self.print_button.clicked.connect(self.print_document)
        top.addWidget(self.print_button)
        top.addStretch()
        top.addWidget(QLabel("Cópias:"))
        self.copies = QSpinBox(minimum=1, maximum=999)
        self.copies.setMinimumWidth(72)
        top.addWidget(self.copies)
        self.left.addLayout(top)

        self.left.addWidget(self.heading("Impressora"))
        self.printer = Choice(["printer", "document-print"], [("", "Nenhuma impressora", "")])
        self.printer.changed.connect(self.on_printer_changed)
        self.left.addWidget(self.printer)
        props = QToolButton(text="Gerenciar impressoras")
        props.setAutoRaise(True)
        props.clicked.connect(lambda: subprocess.Popen(
            ["kcmshell6", "kcm_printer_manager"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))
        self.left.addWidget(props, alignment=Qt.AlignmentFlag.AlignRight)

        self.left.addWidget(self.heading("Configurações"))
        self.scope = Choice(["table", "view-grid", "document-multiple"], [
            ("active", "Imprimir planilha ativa", "Somente a aba atual"),
            ("all", "Imprimir toda a pasta de trabalho", "Todas as abas"),
            ("selection", "Imprimir seleção", "Somente as células selecionadas"),
        ])
        self.left.addWidget(self.scope)

        pages = QHBoxLayout()
        pages.addWidget(QLabel("Páginas:"))
        self.page_from = QSpinBox(minimum=0, maximum=99999, specialValueText=" ")
        self.page_to = QSpinBox(minimum=0, maximum=99999, specialValueText=" ")
        pages.addWidget(self.page_from)
        pages.addWidget(QLabel("para"))
        pages.addWidget(self.page_to)
        self.left.addLayout(pages)

        self.sides = Choice(["document-duplex", "document-multiple"], [
            ("one-sided", "Imprimir em um lado", "Apenas um lado da folha"),
            ("two-sided-long-edge", "Frente e verso (borda longa)", "Vira as páginas na borda longa"),
            ("two-sided-short-edge", "Frente e verso (borda curta)", "Vira as páginas na borda curta"),
        ])
        self.left.addWidget(self.sides)
        self.collate = Choice(["view-list-details", "document-multiple"], [
            (True, "Agrupado", "1;2;3   1;2;3   1;2;3"),
            (False, "Desagrupado", "1;1;1   2;2;2   3;3;3"),
        ])
        self.left.addWidget(self.collate)
        self.orientation = Choice(["object-rotate-right", "document-page-setup"], [
            (False, "Orientação retrato", "Página em pé"),
            (True, "Orientação paisagem", "Página deitada"),
        ])
        self.left.addWidget(self.orientation)
        self.paper = Choice(["document-new", "document-page-setup"],
                            [(k, v[0], v[1]) for k, v in PAPERS.items()])
        self.left.addWidget(self.paper)

        self.margins = Choice(["distribute-horizontal-margin", "transform-crop", "page-simple"],
                              [(k, v[0], v[1]) for k, v in MARGINS.items()]
                              + [("custom", "Margens personalizadas", "Definidas por você")])
        self.left.addWidget(self.margins)
        self.margin_box = QWidget()
        grid = QGridLayout(self.margin_box)
        grid.setContentsMargins(0, 0, 0, 0)
        self.margin_spins = {}
        for index, (key, label) in enumerate((("left", "Esquerda"), ("right", "Direita"),
                                              ("top", "Superior"), ("bottom", "Inferior"))):
            spin = QDoubleSpinBox(minimum=0, maximum=10, singleStep=0.1, decimals=2, suffix=" cm")
            spin.valueChanged.connect(self.schedule)
            self.margin_spins[key] = spin
            grid.addWidget(QLabel(label), index // 2, (index % 2) * 2)
            grid.addWidget(spin, index // 2, (index % 2) * 2 + 1)
        self.left.addWidget(self.margin_box)

        self.scaling = Choice(["zoom-fit-width", "zoom-fit-best", "zoom-original"], [
            ("none", "Sem dimensionamento", "Imprime no tamanho real"),
            ("one", "Ajustar planilha em uma página", "Tudo em uma única página"),
            ("width", "Ajustar todas as colunas em uma página", "Largura de 1 página"),
            ("height", "Ajustar todas as linhas em uma página", "Altura de 1 página"),
            ("percent", "Escala personalizada", "Porcentagem do tamanho real"),
            ("pages", "Ajustar a páginas", "Número de páginas de largura e altura"),
        ])
        self.left.addWidget(self.scaling)
        self.percent_box = QWidget()
        row = QHBoxLayout(self.percent_box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Escala:"))
        self.percent = QSpinBox(minimum=10, maximum=400, singleStep=5, suffix=" %")
        self.percent.valueChanged.connect(self.schedule)
        row.addWidget(self.percent)
        self.left.addWidget(self.percent_box)
        self.fit_box = QWidget()
        row = QHBoxLayout(self.fit_box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Largura:"))
        self.fit_x = QSpinBox(minimum=0, maximum=99, specialValueText="Auto")
        self.fit_x.valueChanged.connect(self.schedule)
        row.addWidget(self.fit_x)
        row.addWidget(QLabel("Altura:"))
        self.fit_y = QSpinBox(minimum=0, maximum=99, specialValueText="Auto")
        self.fit_y.valueChanged.connect(self.schedule)
        row.addWidget(self.fit_y)
        self.left.addWidget(self.fit_box)

        self.center_h = QCheckBox("Centralizar na horizontal")
        self.center_v = QCheckBox("Centralizar na vertical")
        self.grid = QCheckBox("Imprimir linhas de grade")
        self.headings = QCheckBox("Imprimir títulos de linha e coluna")
        self.header_footer = QCheckBox("Cabeçalho e rodapé")
        for box in (self.center_h, self.center_v, self.grid, self.headings, self.header_footer):
            box.toggled.connect(self.schedule)
            self.left.addWidget(box)

        for choice in (self.scope, self.orientation, self.paper, self.margins, self.scaling):
            choice.changed.connect(self.on_layout_choice)

        self.save_button = QPushButton(QIcon.fromTheme("document-save"), "Aplicar na planilha")
        self.save_button.setToolTip("Guarda estes ajustes na planilha sem imprimir")
        self.save_button.clicked.connect(lambda: self.engine.post("apply", self.collect()))
        self.left.addWidget(self.save_button)
        self.update_link = QToolButton(text=f"Versão {VERSION} · Verificar atualizações")
        self.update_link.setAutoRaise(True)
        self.updater = Updater(REPO, "PainelImpressaoCalc", VERSION)
        update_dialog_handler(self, self.updater, self.update_link)
        self.update_link.clicked.connect(self.check_updates)
        self.left.addWidget(self.update_link, alignment=Qt.AlignmentFlag.AlignLeft)
        self.left.addStretch()
        return scroll

    def build_preview(self):
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.preview = Preview()
        column.addWidget(self.preview, 1)
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 6)
        self.prev_page = QToolButton(icon=QIcon.fromTheme("go-previous"), text="<")
        self.prev_page.clicked.connect(lambda: self.page_box.setValue(self.page_box.value() - 1))
        self.page_box = QSpinBox(minimum=1, maximum=1)
        self.page_box.valueChanged.connect(self.on_page)
        self.page_total = QLabel("de 0")
        self.next_page = QToolButton(icon=QIcon.fromTheme("go-next"), text=">")
        self.next_page.clicked.connect(lambda: self.page_box.setValue(self.page_box.value() + 1))
        for widget in (self.prev_page, self.page_box, self.page_total, self.next_page):
            bar.addWidget(widget)
        self.status = QLabel()
        bar.addWidget(self.status, 1, Qt.AlignmentFlag.AlignCenter)
        self.reload_button = QPushButton(QIcon.fromTheme("view-refresh"), "Atualizar dados")
        self.reload_button.setToolTip("Recarrega a prévia depois de editar a planilha")
        self.reload_button.clicked.connect(lambda: self.engine.post("reload", self.collect()))
        bar.addWidget(self.reload_button)
        close = QPushButton("Fechar")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        column.addLayout(bar)
        return panel

    def check_updates(self):
        self.update_link.setEnabled(False)
        self.update_link.setText("Procurando atualizações…")
        self.updater.check()

    def load_printers(self):
        self.printers, default = list_printers()
        options = [(name, name.replace("_", " "), state) for name, state in self.printers]
        if not options:
            options = [("", "Nenhuma impressora", "Instale uma impressora")]
        self.printer.set_options(options)
        self.printer.set_value(default if default in dict(self.printers) else options[0][0])
        self.on_printer_changed(self.printer.value)

    def on_printer_changed(self, name):
        duplex = bool(name) and printer_supports_duplex(name)
        for value in ("two-sided-long-edge", "two-sided-short-edge"):
            self.sides.options[value][2].setEnabled(duplex)
        if not duplex and self.sides.value != "one-sided":
            self.sides.set_value("one-sided")
        elif self.sides.value is None:
            self.sides.set_value("one-sided")

    def on_loaded(self, s, info):
        self.settings = dict(s)
        self.title_text = info["title"]
        self.setWindowTitle(f"Imprimir - {info['title']} ({info['sheet']})")
        self.scope.options["selection"][2].setEnabled(info["has_selection"])
        if s["paper"] == "custom":
            width, height = sorted((s["width"], s["height"]))
            options = [(k, v[0], v[1]) for k, v in PAPERS.items()]
            options.append(("custom", "Tamanho atual",
                            f"{width / 1000:.1f} cm x {height / 1000:.1f} cm".replace(".", ",")))
            self.paper.set_options(options)
        self.scope.set_value("active")
        self.collate.set_value(True)
        self.orientation.set_value(s["landscape"])
        self.paper.set_value(s["paper"])
        self.margins.set_value(s["margin"])
        self.scaling.set_value(s["scaling"])
        for key, spin in self.margin_spins.items():
            spin.setValue(s[key])
        self.percent.setValue(s["percent"])
        self.fit_x.setValue(s["fit_x"])
        self.fit_y.setValue(s["fit_y"])
        self.center_h.setChecked(s["center_h"])
        self.center_v.setChecked(s["center_v"])
        self.grid.setChecked(s["grid"])
        self.headings.setChecked(s["headings"])
        self.header_footer.setChecked(s["header_footer"])
        self.update_extra_boxes()
        self.ready_to_render = True
        self.set_controls_enabled(True)
        self.request_render()

    def update_extra_boxes(self):
        self.margin_box.setVisible(self.margins.value == "custom")
        self.percent_box.setVisible(self.scaling.value == "percent")
        self.fit_box.setVisible(self.scaling.value == "pages")

    def on_layout_choice(self, _value):
        if self.margins.value in MARGINS:
            values = MARGINS[self.margins.value][2:]
            for key, value in zip(("left", "right", "top", "bottom"), values):
                spin = self.margin_spins[key]
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self.update_extra_boxes()
        self.schedule()

    def schedule(self, *_):
        if self.ready_to_render:
            self.status.setText("Atualizando…")
            self.debounce.start()

    def collect(self):
        s = dict(self.settings)
        s.update({
            "scope": self.scope.value, "paper": self.paper.value,
            "landscape": self.orientation.value, "margin": self.margins.value,
            "scaling": self.scaling.value, "percent": self.percent.value(),
            "fit_x": self.fit_x.value(), "fit_y": self.fit_y.value(),
            "center_h": self.center_h.isChecked(), "center_v": self.center_v.isChecked(),
            "grid": self.grid.isChecked(), "headings": self.headings.isChecked(),
            "header_footer": self.header_footer.isChecked(),
        })
        for key, spin in self.margin_spins.items():
            s[key] = spin.value()
        if s["scaling"] == "pages" and not (s["fit_x"] or s["fit_y"]):
            s["fit_x"] = 1
        return s

    def request_render(self):
        if self.ready_to_render:
            self.engine.post("render", self.collect())

    def on_ready(self, path):
        pdf = QPdfDocument(self)
        pdf.load(path)
        previous, self.old_pdf, self.pdf = self.old_pdf, self.pdf, pdf
        if previous is not None:
            previous.deleteLater()
        count = pdf.pageCount()
        page = min(self.page_box.value(), max(1, count))
        self.page_box.blockSignals(True)
        self.page_box.setMaximum(max(1, count))
        self.page_box.setValue(page)
        self.page_box.blockSignals(False)
        self.page_total.setText(f"de {count}")
        self.preview.show_page(pdf, page - 1)
        self.update_page_buttons()
        pages = f"{count} página{'s' if count != 1 else ''}"
        self.status.setText(pages)
        self.print_button.setText(f"Imprimir\n{pages}")

    def on_page(self, value):
        if self.pdf:
            self.preview.show_page(self.pdf, value - 1)
        self.update_page_buttons()

    def update_page_buttons(self):
        self.prev_page.setEnabled(self.page_box.value() > 1)
        self.next_page.setEnabled(self.page_box.value() < self.page_box.maximum())

    def on_busy(self, busy):
        if busy and self.ready_to_render:
            self.status.setText("Atualizando…")
        self.print_button.setEnabled(not busy and self.ready_to_render and bool(self.printer.value))

    def on_failed(self, message):
        self.status.setText("Falhou")
        QMessageBox.warning(self, "Painel de impressão", message)
        if not self.ready_to_render:
            self.close()

    def set_controls_enabled(self, enabled):
        for widget in (self.printer, self.scope, self.page_from, self.page_to, self.sides,
                       self.collate, self.orientation, self.paper, self.margins, self.scaling,
                       self.copies, self.save_button, self.reload_button, self.center_h,
                       self.center_v, self.grid, self.headings, self.header_footer):
            widget.setEnabled(enabled)
        self.print_button.setEnabled(enabled and bool(self.printer.value))

    def print_document(self):
        printer = self.printer.value
        if not printer:
            return
        first, last = self.page_from.value(), self.page_to.value()
        if first and last and first > last:
            QMessageBox.warning(self, "Painel de impressão", "A página inicial é maior que a final.")
            return
        total = self.pdf.pageCount() if self.pdf else 0
        if first > total or last > total:
            QMessageBox.warning(self, "Painel de impressão", f"O documento tem {total} página(s).")
            return
        pages = ""
        if first or last:
            pages = f"{first or 1}-{last or total}"
        s = self.collect()
        media = PAPERS[s["paper"]][4] if s["paper"] in PAPERS else ""
        job = {"printer": printer, "copies": self.copies.value(), "sides": self.sides.value,
               "collate": self.collate.value, "pages": pages, "media": media,
               "title": self.title_text}
        self.engine.post("print", s, job)

    def closeEvent(self, event):
        self.debounce.stop()
        self.engine.stop()
        event.accept()
        QTimer.singleShot(0, QApplication.quit)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default="")
    args = parser.parse_args()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    lock = open(os.path.join(runtime, f"painel-impressao-calc-{args.doc or 'atual'}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # o painel deste documento já está aberto
    work = tempfile.mkdtemp(prefix="painel-impressao-", dir=runtime)
    try:
        app = QApplication(sys.argv[:1])
        app.setApplicationName("painel-impressao-calc")
        window = Window(args.doc, work)
        window.show()
        return app.exec()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
