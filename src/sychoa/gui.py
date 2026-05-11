from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import fitz  # PyMuPDF
except ImportError as exc:  # pragma: no cover - exercised by users without deps
    raise SystemExit(
        "Missing dependency: PyMuPDF. Install with `python3 -m pip install .`."
    ) from exc

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QImage, QKeySequence, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QSplitter,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


from .models import ADDRESS_TYPES, AddressRecord, BoxRecord, normalize_address_type
from .pdf_export import HIGHLIGHT_WIDTH, MIN_BOX_SIZE, export_address_pdfs
from .project_json import (
    addresses_from_json,
    extra_pages_from_json,
    default_type_extra_pages,
    now_iso,
    pdf_records_from_json,
    project_to_json,
    read_project_json,
    resolve_json_path,
    type_extra_pages_from_json,
    write_project_json,
)


RENDER_SCALE = 2.0
HIGHLIGHT_COLOR = "#ff0000"
HANDLE_SIZE = 14.0
THUMBNAIL_WIDTH = 150
SETTINGS_ORG = "sychoa"
SETTINGS_APP = "PDF Address Box Builder"
LAST_PROJECT_KEY = "last_project_path"


@dataclass
class LoadedPdf:
    id: str
    path: Path
    doc: fitz.Document


class EditableBoxItem(QGraphicsRectItem):
    def __init__(
        self,
        box: BoxRecord,
        page_rect: QRectF,
        pen: QPen,
        on_changed: Callable[[], None],
    ) -> None:
        super().__init__(box.rect)
        self.box = box
        self.page_rect = page_rect
        self.on_changed = on_changed
        self.drag_mode: str | None = None
        self.drag_start = QPointF()
        self.original_rect = QRectF()

        self.setPen(pen)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt override
        pad = HANDLE_SIZE / 2.0
        return super().boundingRect().adjusted(-pad, -pad, pad, pad)

    def paint(self, painter, option, widget=None) -> None:  # noqa: ANN001, N802 - Qt override
        super().paint(painter, option, widget)
        painter.setPen(QPen(QColor("#ffffff"), 1.0))
        painter.setBrush(QBrush(QColor(HIGHLIGHT_COLOR)))
        for handle in self.handle_rects().values():
            painter.drawRect(handle)

    def handle_rects(self) -> dict[str, QRectF]:
        rect = self.rect()
        half = HANDLE_SIZE / 2.0
        corners = {
            "top_left": rect.topLeft(),
            "top_right": rect.topRight(),
            "bottom_left": rect.bottomLeft(),
            "bottom_right": rect.bottomRight(),
        }
        return {
            name: QRectF(point.x() - half, point.y() - half, HANDLE_SIZE, HANDLE_SIZE)
            for name, point in corners.items()
        }

    def handle_at(self, pos: QPointF) -> str | None:
        for name, rect in self.handle_rects().items():
            if rect.contains(pos):
                return name
        return None

    def hoverMoveEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        handle = self.handle_at(event.pos())
        if handle in {"top_left", "bottom_right"}:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif handle in {"top_right", "bottom_left"}:
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        self.unsetCursor()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.drag_mode = self.handle_at(event.pos()) or "move"
        self.drag_start = event.scenePos()
        self.original_rect = QRectF(self.rect())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if not self.drag_mode:
            super().mouseMoveEvent(event)
            return

        if self.drag_mode == "move":
            delta = event.scenePos() - self.drag_start
            new_rect = self.clamped_move(self.original_rect.translated(delta))
        else:
            new_rect = self.resized_rect(self.drag_mode, event.scenePos())

        if new_rect.width() >= MIN_BOX_SIZE and new_rect.height() >= MIN_BOX_SIZE:
            self.prepareGeometryChange()
            self.setRect(new_rect)
            self.sync_box_record()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        self.drag_mode = None
        self.sync_box_record()
        self.on_changed()
        event.accept()

    def clamped_move(self, rect: QRectF) -> QRectF:
        dx = 0.0
        dy = 0.0
        if rect.left() < self.page_rect.left():
            dx = self.page_rect.left() - rect.left()
        elif rect.right() > self.page_rect.right():
            dx = self.page_rect.right() - rect.right()
        if rect.top() < self.page_rect.top():
            dy = self.page_rect.top() - rect.top()
        elif rect.bottom() > self.page_rect.bottom():
            dy = self.page_rect.bottom() - rect.bottom()
        return rect.translated(dx, dy)

    def resized_rect(self, handle: str, scene_pos: QPointF) -> QRectF:
        point = bounded_point(scene_pos, self.page_rect)
        rect = QRectF(self.original_rect)
        if handle == "top_left":
            rect.setTopLeft(point)
        elif handle == "top_right":
            rect.setTopRight(point)
        elif handle == "bottom_left":
            rect.setBottomLeft(point)
        elif handle == "bottom_right":
            rect.setBottomRight(point)
        return rect.normalized()

    def sync_box_record(self) -> None:
        rect = self.rect().normalized()
        self.box.x0 = rect.left()
        self.box.y0 = rect.top()
        self.box.x1 = rect.right()
        self.box.y1 = rect.bottom()


class PdfCanvas(QGraphicsView):
    def __init__(self, on_box_created: Callable[[QRectF], None], on_box_changed: Callable[[], None]) -> None:
        super().__init__()
        self.on_box_created = on_box_created
        self.on_box_changed = on_box_changed
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHints(self.renderHints())
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setMouseTracking(True)

        self.page_rect = QRectF()
        self.zoom = 1.0
        self.start_pos: QPointF | None = None
        self.draft_item: QGraphicsRectItem | None = None
        self.pixmap_item: QGraphicsPixmapItem | None = None

    def show_page(
        self,
        image: QImage,
        page_width: float,
        page_height: float,
        addresses: list[AddressRecord],
        current_pdf_id: str,
        selected_address_id: str | None,
        show_all: bool,
    ) -> None:
        self.scene.clear()
        self.start_pos = None
        self.draft_item = None
        self.page_rect = QRectF(0, 0, page_width, page_height)
        self.scene.setSceneRect(self.page_rect)

        self.pixmap_item = self.scene.addPixmap(QPixmap.fromImage(image))
        self.pixmap_item.setScale(1.0 / RENDER_SCALE)
        self.pixmap_item.setZValue(-10)

        for address in addresses:
            is_selected = address.id == selected_address_id
            if not is_selected and not show_all:
                continue
            color = QColor(HIGHLIGHT_COLOR)
            alpha = 230 if is_selected else 90
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha), HIGHLIGHT_WIDTH)
            for box in address.boxes:
                if box.pdf_id != current_pdf_id or box.page != self.current_page:
                    continue
                if is_selected:
                    rect_item = EditableBoxItem(box, self.page_rect, pen, self.on_box_changed)
                    self.scene.addItem(rect_item)
                else:
                    rect_item = self.scene.addRect(box.rect, pen)
                rect_item.setZValue(5 if is_selected else 1)
        self.apply_zoom()

    @property
    def current_page(self) -> int:
        return int(getattr(self, "_current_page", 0))

    @current_page.setter
    def current_page(self, value: int) -> None:
        self._current_page = value

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.25, min(4.0, zoom))
        self.apply_zoom()

    def apply_zoom(self) -> None:
        self.resetTransform()
        self.scale(self.zoom, self.zoom)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        if event.button() != Qt.MouseButton.LeftButton or self.page_rect.isNull():
            super().mousePressEvent(event)
            return
        item = self.itemAt(event.pos())
        if item is not None and item is not self.pixmap_item:
            super().mousePressEvent(event)
            return
        pos = self.mapToScene(event.pos())
        if not self.page_rect.contains(pos):
            return
        self.start_pos = pos
        self.draft_item = self.scene.addRect(QRectF(pos, pos), QPen(QColor("#111827"), 1.25, Qt.PenStyle.DashLine))
        self.draft_item.setZValue(20)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        if self.start_pos and self.draft_item:
            pos = bounded_point(self.mapToScene(event.pos()), self.page_rect)
            self.draft_item.setRect(QRectF(self.start_pos, pos).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        if event.button() != Qt.MouseButton.LeftButton or not self.start_pos or not self.draft_item:
            super().mouseReleaseEvent(event)
            return
        pos = bounded_point(self.mapToScene(event.pos()), self.page_rect)
        rect = QRectF(self.start_pos, pos).normalized()
        self.scene.removeItem(self.draft_item)
        self.start_pos = None
        self.draft_item = None
        if rect.width() >= MIN_BOX_SIZE and rect.height() >= MIN_BOX_SIZE:
            self.on_box_created(rect)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Address Box Builder")
        self.resize(1280, 850)

        self.pdfs: list[LoadedPdf] = []
        self.current_pdf_index = 0
        self.project_path: Path | None = None
        self.working_dir = Path.cwd()
        self.created_at = now_iso()
        self.addresses: list[AddressRecord] = []
        self.extra_pages: dict[str, list[int]] = {}
        self.type_extra_pages = default_type_extra_pages()
        self.current_page = 0
        self.current_zoom = 1.0
        self.show_all_addresses = False
        self.page_image_cache: dict[tuple[str, int, float], QImage] = {}
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        self.canvas = PdfCanvas(self.add_box_from_rect, self.on_box_changed)
        self.build_ui()
        self.update_actions()
        QTimer.singleShot(0, self.load_last_project)

    @property
    def current_pdf(self) -> LoadedPdf | None:
        if 0 <= self.current_pdf_index < len(self.pdfs):
            return self.pdfs[self.current_pdf_index]
        return None

    @property
    def current_pdf_id(self) -> str:
        return self.current_pdf.id if self.current_pdf else ""

    @property
    def pdf_doc(self) -> fitz.Document | None:
        return self.current_pdf.doc if self.current_pdf else None

    @property
    def pdf_path(self) -> Path | None:
        return self.current_pdf.path if self.current_pdf else None

    def build_ui(self) -> None:
        toolbar = QToolBar("Project")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        open_pdf_action = QAction("Open PDFs", self)
        open_pdf_action.triggered.connect(self.open_pdf_dialog)
        toolbar.addAction(open_pdf_action)

        add_pdf_action = QAction("Add PDF", self)
        add_pdf_action.triggered.connect(self.add_pdf_dialog)
        toolbar.addAction(add_pdf_action)

        load_action = QAction("Load JSON", self)
        load_action.triggered.connect(self.load_project_dialog)
        toolbar.addAction(load_action)

        save_action = QAction("Save JSON", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_project_dialog)
        toolbar.addAction(save_action)

        export_action = QAction("Export PDFs", self)
        export_action.triggered.connect(self.export_pdfs_dialog)
        toolbar.addAction(export_action)

        self.save_action = save_action
        self.export_action = export_action

        side_panel = QWidget()
        side_layout = QVBoxLayout(side_panel)

        self.pdf_label = QLabel("No PDF loaded")
        self.pdf_label.setWordWrap(True)
        side_layout.addWidget(self.pdf_label)

        side_layout.addWidget(QLabel("PDFs"))
        self.pdf_list = QListWidget()
        self.pdf_list.setMaximumHeight(120)
        self.pdf_list.currentRowChanged.connect(self.on_pdf_changed)
        side_layout.addWidget(self.pdf_list, 1)

        side_layout.addWidget(QLabel("Addresses"))
        self.address_list = QListWidget()
        self.address_list.currentRowChanged.connect(self.on_address_changed)
        side_layout.addWidget(self.address_list, 3)

        address_buttons = QHBoxLayout()
        add_address = QPushButton("Add")
        add_address.clicked.connect(self.add_address_dialog)
        duplicate_address = QPushButton("Duplicate")
        duplicate_address.clicked.connect(self.duplicate_address_dialog)
        rename_address = QPushButton("Rename")
        rename_address.clicked.connect(self.rename_address_dialog)
        delete_address = QPushButton("Delete")
        delete_address.clicked.connect(self.delete_selected_address)
        address_buttons.addWidget(add_address)
        address_buttons.addWidget(duplicate_address)
        address_buttons.addWidget(rename_address)
        address_buttons.addWidget(delete_address)
        side_layout.addLayout(address_buttons)

        address_type_controls = QHBoxLayout()
        address_type_controls.addWidget(QLabel("Type"))
        self.address_type_combo = QComboBox()
        self.address_type_combo.addItems(ADDRESS_TYPES)
        self.address_type_combo.currentTextChanged.connect(self.on_address_type_changed)
        address_type_controls.addWidget(self.address_type_combo)
        side_layout.addLayout(address_type_controls)

        side_layout.addWidget(QLabel("Boxes"))
        self.box_list = QTreeWidget()
        self.box_list.setHeaderHidden(True)
        self.box_list.itemClicked.connect(self.on_box_clicked)
        side_layout.addWidget(self.box_list, 2)
        delete_box = QPushButton("Delete Box")
        delete_box.clicked.connect(self.delete_selected_box)
        side_layout.addWidget(delete_box)

        page_tabs = QTabWidget()

        address_page_tab = QWidget()
        address_page_layout = QVBoxLayout(address_page_tab)
        self.extra_page_list = QListWidget()
        self.extra_page_list.itemClicked.connect(self.on_extra_page_clicked)
        address_page_layout.addWidget(self.extra_page_list)

        extra_page_buttons = QHBoxLayout()
        add_extra_page = QPushButton("Add Current")
        add_extra_page.clicked.connect(self.add_current_page_to_extra_pages)
        remove_extra_page = QPushButton("Remove")
        remove_extra_page.clicked.connect(self.remove_selected_extra_page)
        extra_page_buttons.addWidget(add_extra_page)
        extra_page_buttons.addWidget(remove_extra_page)
        address_page_layout.addLayout(extra_page_buttons)
        page_tabs.addTab(address_page_tab, "Global Pages")

        type_page_tab = QWidget()
        type_page_layout = QVBoxLayout(type_page_tab)
        type_page_controls = QHBoxLayout()
        type_page_controls.addWidget(QLabel("Type"))
        self.type_page_combo = QComboBox()
        self.type_page_combo.addItems(ADDRESS_TYPES)
        self.type_page_combo.currentTextChanged.connect(self.refresh_type_page_list)
        type_page_controls.addWidget(self.type_page_combo)
        type_page_layout.addLayout(type_page_controls)

        self.type_page_list = QListWidget()
        self.type_page_list.itemClicked.connect(self.on_type_page_clicked)
        type_page_layout.addWidget(self.type_page_list)

        type_page_buttons = QHBoxLayout()
        add_type_page = QPushButton("Add Current")
        add_type_page.clicked.connect(self.add_current_page_to_type_pages)
        remove_type_page = QPushButton("Remove")
        remove_type_page.clicked.connect(self.remove_selected_type_page)
        type_page_buttons.addWidget(add_type_page)
        type_page_buttons.addWidget(remove_type_page)
        type_page_layout.addLayout(type_page_buttons)
        page_tabs.addTab(type_page_tab, "Type Pages")

        side_layout.addWidget(page_tabs, 2)

        self.show_all_checkbox = QCheckBox("Show all addresses")
        self.show_all_checkbox.setChecked(False)
        self.show_all_checkbox.toggled.connect(self.set_show_all_addresses)
        side_layout.addWidget(self.show_all_checkbox)

        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setMinimumWidth(80)
        self.page_spin.valueChanged.connect(self.go_to_spin_page)
        self.page_spin.setEnabled(False)

        self.page_total_label = QLabel("of 0")

        page_number_controls = QHBoxLayout()
        page_number_controls.addWidget(QLabel("Page"))
        page_number_controls.addWidget(self.page_spin)
        page_number_controls.addWidget(self.page_total_label)
        page_number_controls.addStretch(1)
        side_layout.addLayout(page_number_controls)

        zoom_out = QPushButton("-")
        zoom_out.clicked.connect(lambda: self.set_zoom(self.current_zoom / 1.2))
        zoom_in = QPushButton("+")
        zoom_in.clicked.connect(lambda: self.set_zoom(self.current_zoom * 1.2))
        fit_page = QPushButton("Fit")
        fit_page.clicked.connect(self.fit_page)
        self.zoom_label = QLabel("100%")

        zoom_controls = QHBoxLayout()
        zoom_controls.addWidget(zoom_out)
        zoom_controls.addWidget(self.zoom_label)
        zoom_controls.addWidget(zoom_in)
        zoom_controls.addWidget(fit_page)
        side_layout.addLayout(zoom_controls)

        thumbnail_panel = QWidget()
        thumbnail_layout = QVBoxLayout(thumbnail_panel)
        thumbnail_layout.addWidget(QLabel("Pages"))
        self.thumbnail_list = QListWidget()
        self.thumbnail_list.setViewMode(QListView.ViewMode.IconMode)
        self.thumbnail_list.setFlow(QListView.Flow.TopToBottom)
        self.thumbnail_list.setWrapping(False)
        self.thumbnail_list.setMovement(QListView.Movement.Static)
        self.thumbnail_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.thumbnail_list.setIconSize(QSize(THUMBNAIL_WIDTH, int(THUMBNAIL_WIDTH * 1.4)))
        self.thumbnail_list.setSpacing(8)
        self.thumbnail_list.setUniformItemSizes(False)
        self.thumbnail_list.itemClicked.connect(self.on_thumbnail_clicked)
        thumbnail_layout.addWidget(self.thumbnail_list)

        splitter = QSplitter()
        splitter.addWidget(side_panel)
        splitter.addWidget(self.canvas)
        splitter.addWidget(thumbnail_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([320, 800, 220])
        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())

        delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self)
        delete_shortcut.activated.connect(self.delete_selected_box)
        backspace_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self)
        backspace_shortcut.activated.connect(self.delete_selected_box)
        previous_page_shortcut = QShortcut(QKeySequence(Qt.Key.Key_PageUp), self)
        previous_page_shortcut.activated.connect(self.previous_page)
        next_page_shortcut = QShortcut(QKeySequence(Qt.Key.Key_PageDown), self)
        next_page_shortcut.activated.connect(self.next_page)

    def open_pdf_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Open main PDFs", str(self.working_dir), "PDF files (*.pdf)")
        if paths:
            if self.addresses and not confirm(self, "Open PDF", "Opening a new PDF clears the current addresses and boxes."):
                return
            self.load_pdfs([{"path": Path(path)} for path in paths], clear_project=True, create_default_project=True)

    def add_pdf_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDFs", str(self.working_dir), "PDF files (*.pdf)")
        if paths:
            self.load_pdfs([{"path": Path(path)} for path in paths], clear_project=False)
            self.autosave_project()

    def load_pdfs(
        self,
        pdf_records: list[dict],
        clear_project: bool,
        create_default_project: bool = False,
    ) -> None:
        loaded: list[LoadedPdf] = []
        for record in pdf_records:
            path = Path(record["path"]).expanduser().resolve()
            try:
                doc = fitz.open(path)
            except Exception as exc:
                QMessageBox.critical(self, "Could not open PDF", f"{path}\n\n{exc}")
                for pdf_source in loaded:
                    pdf_source.doc.close()
                return
            loaded.append(LoadedPdf(id=str(record.get("id") or self.new_pdf_id()), path=path, doc=doc))

        if clear_project:
            self.close_pdfs()
            self.pdfs = loaded
            self.current_pdf_index = 0
            self.addresses = []
            self.extra_pages = {}
            self.type_extra_pages = default_type_extra_pages()
            self.project_path = self.default_project_path(loaded[0].path) if create_default_project and loaded else None
            self.created_at = now_iso()
        else:
            existing_paths = {pdf_source.path for pdf_source in self.pdfs}
            for pdf_source in loaded:
                if pdf_source.path in existing_paths:
                    pdf_source.doc.close()
                    continue
                self.pdfs.append(pdf_source)
            if self.pdfs and self.current_pdf_index >= len(self.pdfs):
                self.current_pdf_index = 0

        if self.pdfs:
            self.working_dir = self.pdfs[self.current_pdf_index].path.parent
        self.page_image_cache.clear()
        self.current_page = 0
        self.refresh_pdf_list()
        self.refresh_pdf_page_state()
        if clear_project and self.project_path:
            self.autosave_project()
        if self.pdfs:
            self.statusBar().showMessage(f"Loaded {len(self.pdfs)} PDF(s)", 4000)

    def new_pdf_id(self) -> str:
        existing_ids = {pdf_source.id for pdf_source in self.pdfs}
        while True:
            pdf_id = f"pdf-{uuid.uuid4().hex[:8]}"
            if pdf_id not in existing_ids:
                return pdf_id

    def close_pdfs(self) -> None:
        for pdf_source in self.pdfs:
            pdf_source.doc.close()

    def refresh_pdf_list(self) -> None:
        current_id = self.current_pdf_id
        self.pdf_list.blockSignals(True)
        self.pdf_list.clear()
        selected_index = 0
        for index, pdf_source in enumerate(self.pdfs):
            item = QListWidgetItem(f"{pdf_source.path.name}\n{pdf_source.doc.page_count} pages")
            item.setData(Qt.ItemDataRole.UserRole, pdf_source.id)
            self.pdf_list.addItem(item)
            if pdf_source.id == current_id:
                selected_index = index
        if self.pdfs:
            self.current_pdf_index = min(selected_index, len(self.pdfs) - 1)
            self.pdf_list.setCurrentRow(self.current_pdf_index)
        self.pdf_list.blockSignals(False)

    def refresh_pdf_page_state(self) -> None:
        doc = self.pdf_doc
        self.page_image_cache.clear()
        self.current_page = 0
        if not doc:
            self.page_spin.setEnabled(False)
            self.page_total_label.setText("of 0")
            self.thumbnail_list.clear()
            self.render_current_page()
            self.update_actions()
            return
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, doc.page_count))
        self.page_spin.setValue(1)
        self.page_spin.blockSignals(False)
        self.page_total_label.setText(f"of {doc.page_count}")
        self.refresh_thumbnails()
        self.refresh_type_page_list()
        self.refresh_address_list()
        self.render_current_page()
        self.update_actions()

    def on_pdf_changed(self, index: int) -> None:
        if index < 0 or index >= len(self.pdfs) or index == self.current_pdf_index:
            return
        self.current_pdf_index = index
        self.working_dir = self.pdfs[index].path.parent
        self.refresh_pdf_page_state()

    def pdf_by_id(self, pdf_id: str) -> LoadedPdf | None:
        for pdf_source in self.pdfs:
            if pdf_source.id == pdf_id:
                return pdf_source
        return None

    def go_to_pdf_page(self, pdf_id: str, page: int) -> None:
        for index, pdf_source in enumerate(self.pdfs):
            if pdf_source.id == pdf_id:
                self.current_page = max(0, min(page, pdf_source.doc.page_count - 1))
                if index != self.current_pdf_index:
                    self.current_pdf_index = index
                    self.working_dir = pdf_source.path.parent
                    self.page_image_cache.clear()
                    self.pdf_list.blockSignals(True)
                    self.pdf_list.setCurrentRow(index)
                    self.pdf_list.blockSignals(False)
                    self.page_spin.blockSignals(True)
                    self.page_spin.setMaximum(max(1, pdf_source.doc.page_count))
                    self.page_spin.setValue(self.current_page + 1)
                    self.page_spin.blockSignals(False)
                    self.page_total_label.setText(f"of {pdf_source.doc.page_count}")
                    self.refresh_thumbnails()
                    self.refresh_extra_page_list()
                    self.refresh_type_page_list()
                self.render_current_page()
                return

    def load_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load JSON project", str(self.working_dir), "JSON files (*.json)")
        if path:
            self.load_project(Path(path))

    def load_project(self, path: Path) -> None:
        try:
            path = path.expanduser().resolve()
            self.working_dir = path.parent
            data = read_project_json(path)
            pdf_records = []
            for record in pdf_records_from_json(data):
                pdf_path = resolve_json_path(record["path"], path)
                if not pdf_path.exists():
                    located, _ = QFileDialog.getOpenFileName(
                        self,
                        f"Locate PDF {record['path']}",
                        str(self.working_dir),
                        "PDF files (*.pdf)",
                    )
                    if not located:
                        return
                    pdf_path = Path(located)
                pdf_records.append({"id": record["id"], "path": pdf_path})
            if not pdf_records:
                QMessageBox.warning(self, "No PDFs", "The JSON project does not reference any PDFs.")
                return
            self.load_pdfs(pdf_records, clear_project=True)
            self.project_path = path
            self.working_dir = path.parent
            self.created_at = str(data.get("created_at") or now_iso())
            self.addresses = addresses_from_json(data)
            self.extra_pages = extra_pages_from_json(data)
            self.type_extra_pages = type_extra_pages_from_json(data)
            self.refresh_type_page_list()
            self.refresh_address_list()
            if self.addresses:
                self.address_list.setCurrentRow(0)
            self.render_current_page()
            self.remember_project_path(path)
            self.statusBar().showMessage(f"Loaded {path.name}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Could not load JSON", str(exc))

    def save_project_dialog(self) -> None:
        if not self.pdf_doc or not self.pdf_path:
            QMessageBox.warning(self, "No PDF", "Open a PDF before saving.")
            return
        suggested = self.project_path or self.default_project_path(self.pdf_path)
        path, _ = QFileDialog.getSaveFileName(self, "Save JSON project", str(suggested), "JSON files (*.json)")
        if not path:
            return
        self.project_path = Path(path).expanduser().resolve()
        self.working_dir = self.project_path.parent
        self.save_project()
        self.statusBar().showMessage(f"Saved {self.project_path.name}", 4000)

    def save_project(self) -> bool:
        if not self.pdf_doc or not self.pdf_path or not self.project_path:
            return False
        data = self.project_json()
        write_project_json(self.project_path, data)
        self.remember_project_path(self.project_path)
        return True

    def autosave_project(self) -> None:
        if not self.project_path and self.pdf_path:
            self.project_path = self.default_project_path(self.pdf_path)
        if self.save_project():
            self.statusBar().showMessage(f"Autosaved {self.project_path.name}", 2500)

    def default_project_path(self, pdf_path: Path) -> Path:
        base = pdf_path.with_name(f"{pdf_path.stem}-boxes.json")
        if self.project_path == base or not base.exists():
            return base
        counter = 2
        while True:
            candidate = pdf_path.with_name(f"{pdf_path.stem}-boxes-{counter}.json")
            if not candidate.exists():
                return candidate
            counter += 1

    def remember_project_path(self, path: Path) -> None:
        self.settings.setValue(LAST_PROJECT_KEY, str(path.expanduser().resolve()))

    def load_last_project(self) -> None:
        raw_path = self.settings.value(LAST_PROJECT_KEY, "", str)
        if not raw_path:
            return
        path = Path(raw_path).expanduser()
        if path.exists():
            self.load_project(path)
        else:
            self.settings.remove(LAST_PROJECT_KEY)

    def project_json(self) -> dict:
        return project_to_json(
            self.pdfs,
            self.addresses,
            self.created_at,
            self.project_path,
            self.extra_pages,
            self.type_extra_pages,
        )

    def export_pdfs_dialog(self) -> None:
        if not self.pdf_doc or not self.pdf_path:
            QMessageBox.warning(self, "No PDF", "Open a PDF before exporting.")
            return
        exportable_addresses = [
            address
            for address in self.addresses
            if address.boxes or self.extra_pages or self.type_extra_pages.get(normalize_address_type(address.address_type))
        ]
        if not exportable_addresses:
            QMessageBox.warning(self, "No output pages", "Add at least one box or extra output page before exporting.")
            return
        out_dir = QFileDialog.getExistingDirectory(self, "Choose export folder", str(self.working_dir))
        if not out_dir:
            return
        self.working_dir = Path(out_dir)
        progress_dialog = QProgressDialog("Preparing export...", None, 0, 0, self)
        progress_dialog.setWindowTitle("Exporting PDFs")
        progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setAutoClose(False)
        progress_dialog.setAutoReset(False)
        progress_dialog.show()

        def update_progress(current: int, total: int, message: str) -> None:
            maximum = max(total, 1)
            if progress_dialog.maximum() != maximum:
                progress_dialog.setRange(0, maximum)
            progress_dialog.setLabelText(message)
            progress_dialog.setValue(min(current, maximum))
            self.statusBar().showMessage(message)
            QApplication.processEvents()

        try:
            exported = export_address_pdfs(
                self.pdfs,
                exportable_addresses,
                Path(out_dir),
                self.extra_pages,
                self.type_extra_pages,
                progress_callback=update_progress,
            )
        except Exception as exc:
            progress_dialog.close()
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        progress_dialog.setValue(progress_dialog.maximum())
        progress_dialog.close()
        self.statusBar().showMessage(f"Exported {len(exported)} PDFs to {out_dir}", 6000)
        QMessageBox.information(self, "Export complete", f"Exported {len(exported)} PDFs.")

    def add_address_dialog(self) -> None:
        label, ok = QInputDialog.getText(self, "Add address", "Address:")
        label = label.strip()
        if not ok or not label:
            return
        address = AddressRecord(label=label, address_type=self.address_type_combo.currentText())
        self.addresses.append(address)
        self.refresh_address_list()
        self.set_selected_address(address.id)
        self.render_current_page()
        self.autosave_project()

    def duplicate_address_dialog(self) -> None:
        source = self.selected_address()
        if not source:
            QMessageBox.warning(self, "No address", "Select an address before duplicating.")
            return
        label, ok = QInputDialog.getText(self, "Duplicate address", "New address:", text=f"{source.label} copy")
        label = label.strip()
        if not ok or not label:
            return
        duplicated = AddressRecord(
            label=label,
            address_type=source.address_type,
            boxes=[
                BoxRecord(
                    pdf_id=box.pdf_id,
                    page=box.page,
                    x0=box.rect.left(),
                    y0=box.rect.top(),
                    x1=box.rect.right(),
                    y1=box.rect.bottom(),
                )
                for box in source.boxes
            ],
        )
        self.addresses.append(duplicated)
        self.refresh_address_list()
        self.set_selected_address(duplicated.id)
        self.render_current_page()
        self.autosave_project()

    def rename_address_dialog(self) -> None:
        address = self.selected_address()
        if not address:
            return
        label, ok = QInputDialog.getText(self, "Rename address", "Address:", text=address.label)
        label = label.strip()
        if ok and label:
            address.label = label
            self.refresh_address_list()
            self.render_current_page()
            self.autosave_project()

    def delete_selected_address(self) -> None:
        address = self.selected_address()
        if not address:
            return
        if not confirm(self, "Delete address", f"Delete {address.label} and its boxes?"):
            return
        self.addresses = [item for item in self.addresses if item.id != address.id]
        self.refresh_address_list()
        self.render_current_page()
        self.autosave_project()

    def add_box_from_rect(self, rect: QRectF) -> None:
        address = self.selected_address()
        if not address:
            QMessageBox.warning(self, "No address", "Add or select an address before drawing boxes.")
            return
        address.boxes.append(
            BoxRecord(
                pdf_id=self.current_pdf_id,
                page=self.current_page,
                x0=rect.left(),
                y0=rect.top(),
                x1=rect.right(),
                y1=rect.bottom(),
            )
        )
        self.refresh_box_list()
        self.render_current_page()
        self.autosave_project()

    def delete_selected_box(self) -> None:
        address = self.selected_address()
        item = self.box_list.currentItem()
        if not address or not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        box_id = data.get("box_id")
        if not box_id:
            return
        box_index = next((index for index, box in enumerate(address.boxes) if box.id == box_id), -1)
        if box_index < 0:
            return
        del address.boxes[box_index]
        self.refresh_box_list()
        self.render_current_page()
        self.autosave_project()

    def on_box_changed(self) -> None:
        self.refresh_box_list()
        self.autosave_project()

    def on_address_type_changed(self, address_type: str) -> None:
        address = self.selected_address()
        if not address:
            return
        normalized = normalize_address_type(address_type)
        if address.address_type == normalized:
            return
        address.address_type = normalized
        self.refresh_address_list()
        self.autosave_project()

    def add_current_page_to_extra_pages(self) -> None:
        if not self.pdf_doc:
            return
        pages = self.extra_pages.setdefault(self.current_pdf_id, [])
        if self.current_page not in pages:
            pages.append(self.current_page)
            pages.sort()
            self.refresh_extra_page_list()
            self.autosave_project()

    def remove_selected_extra_page(self) -> None:
        row = self.extra_page_list.currentRow()
        if row < 0 or row >= self.extra_page_list.count():
            return
        item = self.extra_page_list.item(row)
        data = item.data(Qt.ItemDataRole.UserRole)
        pdf_id = data["pdf_id"]
        page = int(data["page"])
        self.extra_pages[pdf_id] = [extra_page for extra_page in self.extra_pages.get(pdf_id, []) if extra_page != page]
        if not self.extra_pages[pdf_id]:
            del self.extra_pages[pdf_id]
        self.refresh_extra_page_list()
        self.autosave_project()

    def on_extra_page_clicked(self, item: QListWidgetItem) -> None:
        if not self.pdfs:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        self.go_to_pdf_page(str(data["pdf_id"]), int(data["page"]))

    def selected_type_for_pages(self) -> str:
        return normalize_address_type(self.type_page_combo.currentText())

    def add_current_page_to_type_pages(self) -> None:
        if not self.pdf_doc:
            return
        address_type = self.selected_type_for_pages()
        pages = self.type_extra_pages.setdefault(address_type, {}).setdefault(self.current_pdf_id, [])
        if self.current_page not in pages:
            pages.append(self.current_page)
            pages.sort()
            self.refresh_type_page_list()
            self.refresh_address_list()
            self.autosave_project()

    def remove_selected_type_page(self) -> None:
        row = self.type_page_list.currentRow()
        if row < 0 or row >= self.type_page_list.count():
            return
        address_type = self.selected_type_for_pages()
        item = self.type_page_list.item(row)
        data = item.data(Qt.ItemDataRole.UserRole)
        pdf_id = data["pdf_id"]
        page = int(data["page"])
        self.type_extra_pages.setdefault(address_type, {})[pdf_id] = [
            type_page for type_page in self.type_extra_pages.get(address_type, {}).get(pdf_id, []) if type_page != page
        ]
        if not self.type_extra_pages[address_type][pdf_id]:
            del self.type_extra_pages[address_type][pdf_id]
        self.refresh_type_page_list()
        self.refresh_address_list()
        self.autosave_project()

    def on_type_page_clicked(self, item: QListWidgetItem) -> None:
        if not self.pdfs:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        self.go_to_pdf_page(str(data["pdf_id"]), int(data["page"]))

    def selected_address(self) -> AddressRecord | None:
        item = self.address_list.currentItem()
        if item is None:
            return None
        address_id = item.data(Qt.ItemDataRole.UserRole)
        if not address_id:
            return None
        return next((address for address in self.addresses if address.id == address_id), None)

    def set_selected_address(self, address_id: str) -> None:
        for row in range(self.address_list.count()):
            item = self.address_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == address_id:
                self.address_list.setCurrentRow(row)
                return

    def on_address_changed(self) -> None:
        self.refresh_address_type_combo()
        self.refresh_box_list()
        self.refresh_extra_page_list()
        self.render_current_page()

    def refresh_address_list(self) -> None:
        previous_id = self.selected_address().id if self.selected_address() else None
        self.address_list.blockSignals(True)
        self.address_list.clear()
        selected_row = -1
        sorted_addresses = sorted(
            self.addresses,
            key=lambda address: (address.label.casefold(), normalize_address_type(address.address_type), address.id),
        )
        for index, address in enumerate(sorted_addresses):
            type_page_count = sum(len(pages) for pages in self.type_extra_pages.get(normalize_address_type(address.address_type), {}).values())
            item = QListWidgetItem(
                f"{address.label} [{normalize_address_type(address.address_type)}] "
                f"({len(address.boxes)} boxes, {type_page_count} type)"
            )
            item.setData(Qt.ItemDataRole.UserRole, address.id)
            self.address_list.addItem(item)
            if address.id == previous_id:
                selected_row = index
        self.address_list.blockSignals(False)
        if selected_row >= 0:
            self.address_list.setCurrentRow(selected_row)
        elif sorted_addresses:
            self.address_list.setCurrentRow(0)
        self.refresh_box_list()
        self.refresh_extra_page_list()
        self.refresh_address_type_combo()

    def refresh_address_type_combo(self) -> None:
        address = self.selected_address()
        self.address_type_combo.blockSignals(True)
        self.address_type_combo.setEnabled(address is not None)
        if address:
            self.address_type_combo.setCurrentText(normalize_address_type(address.address_type))
        else:
            self.address_type_combo.setCurrentText("A")
        self.address_type_combo.blockSignals(False)

    def refresh_box_list(self) -> None:
        selected_item = self.box_list.currentItem()
        selected_data = selected_item.data(0, Qt.ItemDataRole.UserRole) if selected_item else {}
        selected_box_id = selected_data.get("box_id") if isinstance(selected_data, dict) else None
        self.box_list.clear()
        address = self.selected_address()
        if not address:
            return
        boxes_by_pdf: dict[str, list[BoxRecord]] = {}
        for box in address.boxes:
            boxes_by_pdf.setdefault(box.pdf_id, []).append(box)

        pdf_groups: list[tuple[str, str]] = [(pdf_source.id, pdf_source.path.name) for pdf_source in self.pdfs]
        missing_ids = sorted(pdf_id for pdf_id in boxes_by_pdf if pdf_id and not self.pdf_by_id(pdf_id))
        pdf_groups.extend((pdf_id, f"Missing PDF ({pdf_id})") for pdf_id in missing_ids)
        if "" in boxes_by_pdf:
            pdf_groups.append(("", "Missing PDF"))

        selected_tree_item: QTreeWidgetItem | None = None
        for pdf_id, pdf_name in pdf_groups:
            pdf_boxes = sorted(
                boxes_by_pdf.get(pdf_id, []),
                key=lambda box: (box.page, box.rect.top(), box.rect.left(), box.id),
            )
            pdf_item = QTreeWidgetItem([f"{pdf_name} ({len(pdf_boxes)})"])
            pdf_item.setData(0, Qt.ItemDataRole.UserRole, {"pdf_id": pdf_id})
            self.box_list.addTopLevelItem(pdf_item)
            pdf_item.setExpanded(True)
            for box in pdf_boxes:
                rect = box.rect
                child = QTreeWidgetItem(
                    [
                        f"Page {box.page + 1}: x {rect.left():.1f}, y {rect.top():.1f}, "
                        f"{rect.width():.1f} x {rect.height():.1f}"
                    ]
                )
                child.setData(0, Qt.ItemDataRole.UserRole, {"box_id": box.id, "pdf_id": box.pdf_id, "page": box.page})
                pdf_item.addChild(child)
                if box.id == selected_box_id:
                    selected_tree_item = child
        if selected_tree_item:
            self.box_list.setCurrentItem(selected_tree_item)

    def refresh_extra_page_list(self) -> None:
        self.extra_page_list.clear()
        valid_pages = []
        current_pdf_id = self.current_pdf_id
        for page in sorted(set(self.extra_pages.get(current_pdf_id, []))):
            if self.pdf_doc and not (0 <= page < self.pdf_doc.page_count):
                continue
            valid_pages.append(page)
            item = QListWidgetItem(f"Page {page + 1}")
            item.setData(Qt.ItemDataRole.UserRole, {"pdf_id": current_pdf_id, "page": page})
            self.extra_page_list.addItem(item)
        if current_pdf_id and valid_pages != self.extra_pages.get(current_pdf_id, []):
            if valid_pages:
                self.extra_pages[current_pdf_id] = valid_pages
            elif current_pdf_id in self.extra_pages:
                del self.extra_pages[current_pdf_id]

    def refresh_type_page_list(self) -> None:
        self.type_page_list.clear()
        address_type = self.selected_type_for_pages()
        current_pdf_id = self.current_pdf_id
        valid_pages = []
        for page in sorted(set(self.type_extra_pages.get(address_type, {}).get(current_pdf_id, []))):
            if self.pdf_doc and not (0 <= page < self.pdf_doc.page_count):
                continue
            valid_pages.append(page)
            item = QListWidgetItem(f"Page {page + 1}")
            item.setData(Qt.ItemDataRole.UserRole, {"pdf_id": current_pdf_id, "page": page})
            self.type_page_list.addItem(item)
        if current_pdf_id and valid_pages != self.type_extra_pages.get(address_type, {}).get(current_pdf_id, []):
            self.type_extra_pages.setdefault(address_type, {})
            if valid_pages:
                self.type_extra_pages[address_type][current_pdf_id] = valid_pages
            elif current_pdf_id in self.type_extra_pages[address_type]:
                del self.type_extra_pages[address_type][current_pdf_id]

    def on_box_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if not self.pdfs:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if not data.get("box_id"):
            return
        self.go_to_pdf_page(str(data.get("pdf_id") or self.current_pdf_id), int(data.get("page", self.current_page)))

    def render_current_page(self) -> None:
        if not self.pdf_doc:
            return
        self.current_page = max(0, min(self.current_page, self.pdf_doc.page_count - 1))
        self.canvas.current_page = self.current_page
        page = self.pdf_doc[self.current_page]
        cache_key = (self.current_pdf_id, self.current_page, RENDER_SCALE)
        image = self.page_image_cache.get(cache_key)
        if image is None:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(RENDER_SCALE, RENDER_SCALE), alpha=False)
            image = QImage(
                pixmap.samples,
                pixmap.width,
                pixmap.height,
                pixmap.stride,
                QImage.Format.Format_RGB888,
            ).copy()
            self.page_image_cache[cache_key] = image

        self.page_spin.blockSignals(True)
        self.page_spin.setValue(self.current_page + 1)
        self.page_spin.blockSignals(False)
        self.thumbnail_list.setCurrentRow(self.current_page)
        self.page_total_label.setText(f"of {self.pdf_doc.page_count}")
        self.pdf_label.setText(
            f"{self.pdf_path.name if self.pdf_path else 'PDF'}\n"
            f"{self.pdf_doc.page_count} pages\n"
            f"{len(self.pdfs)} PDF(s) in project"
        )
        self.canvas.show_page(
            image=image,
            page_width=page.rect.width,
            page_height=page.rect.height,
            addresses=self.addresses,
            current_pdf_id=self.current_pdf_id,
            selected_address_id=self.selected_address().id if self.selected_address() else None,
            show_all=self.show_all_addresses,
        )
        self.update_actions()

    def refresh_thumbnails(self) -> None:
        self.thumbnail_list.clear()
        if not self.pdf_doc:
            return
        for page_index, page in enumerate(self.pdf_doc):
            scale = THUMBNAIL_WIDTH / page.rect.width
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = QImage(
                pixmap.samples,
                pixmap.width,
                pixmap.height,
                pixmap.stride,
                QImage.Format.Format_RGB888,
            ).copy()
            item = QListWidgetItem(QIcon(QPixmap.fromImage(image)), f"Page {page_index + 1}")
            item.setData(Qt.ItemDataRole.UserRole, page_index)
            item.setSizeHint(QSize(THUMBNAIL_WIDTH + 24, image.height() + 32))
            self.thumbnail_list.addItem(item)
        self.thumbnail_list.setCurrentRow(self.current_page)

    def on_thumbnail_clicked(self, item: QListWidgetItem) -> None:
        if not self.pdf_doc:
            return
        page_index = int(item.data(Qt.ItemDataRole.UserRole))
        if page_index != self.current_page:
            self.current_page = page_index
            self.render_current_page()

    def first_page(self) -> None:
        if self.pdf_doc and self.current_page != 0:
            self.current_page = 0
            self.render_current_page()

    def previous_page(self) -> None:
        if self.pdf_doc and self.current_page > 0:
            self.current_page -= 1
            self.render_current_page()

    def next_page(self) -> None:
        if self.pdf_doc and self.current_page < self.pdf_doc.page_count - 1:
            self.current_page += 1
            self.render_current_page()

    def last_page(self) -> None:
        if self.pdf_doc and self.current_page != self.pdf_doc.page_count - 1:
            self.current_page = self.pdf_doc.page_count - 1
            self.render_current_page()

    def go_to_spin_page(self, value: int) -> None:
        if self.pdf_doc:
            self.current_page = value - 1
            self.render_current_page()

    def set_zoom(self, zoom: float) -> None:
        self.current_zoom = max(0.25, min(4.0, zoom))
        self.canvas.set_zoom(self.current_zoom)
        self.zoom_label.setText(f"{int(self.current_zoom * 100)}%")

    def fit_page(self) -> None:
        if not self.pdf_doc:
            return
        page_rect = self.pdf_doc[self.current_page].rect
        available_width = max(100, self.canvas.viewport().width() - 40)
        available_height = max(100, self.canvas.viewport().height() - 40)
        self.set_zoom(min(available_width / page_rect.width, available_height / page_rect.height))

    def set_show_all_addresses(self, checked: bool) -> None:
        self.show_all_addresses = checked
        self.render_current_page()

    def update_actions(self) -> None:
        has_pdf = self.pdf_doc is not None
        self.save_action.setEnabled(has_pdf)
        self.export_action.setEnabled(has_pdf)
        self.page_spin.setEnabled(has_pdf)

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        self.autosave_project()
        self.close_pdfs()
        event.accept()


def address_color(index: int) -> QColor:
    colors = [
        "#2563eb",
        "#dc2626",
        "#059669",
        "#d97706",
        "#7c3aed",
        "#0891b2",
        "#be123c",
        "#4d7c0f",
    ]
    return QColor(colors[index % len(colors)])


def bounded_point(point: QPointF, bounds: QRectF) -> QPointF:
    return QPointF(
        min(max(point.x(), bounds.left()), bounds.right()),
        min(max(point.y(), bounds.top()), bounds.bottom()),
    )


def confirm(parent: QWidget, title: str, message: str) -> bool:
    return (
        QMessageBox.question(
            parent,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        == QMessageBox.StandardButton.Yes
    )


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
