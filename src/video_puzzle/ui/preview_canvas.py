from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from video_puzzle.layout import Layout, slot_count
from video_puzzle.ui.slot_widget import SlotWidget


class PreviewCanvas(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("previewCanvas")
        self.slots = [SlotWidget(i, self) for i in range(4)]
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._inner = QWidget(self)
        self._root.addWidget(self._inner)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.apply_layout(Layout.FOUR_SQUARE)

    def ensure_slots(self, count: int) -> list[SlotWidget]:
        created: list[SlotWidget] = []
        while len(self.slots) < count:
            widget = SlotWidget(len(self.slots), self)
            self.slots.append(widget)
            created.append(widget)
        return created

    def set_wall_mode(self, enabled: bool) -> None:
        for slot in self.slots:
            slot.set_wall_mode(enabled)

    def apply_layout(self, kind: Layout) -> None:
        count = slot_count(kind)
        self.ensure_slots(count)
        inner = self._replace_inner()
        visible = self.slots[:count]

        if kind is Layout.TWO_HORIZONTAL:
            layout = QHBoxLayout(inner)
            for slot in visible:
                layout.addWidget(slot, 1)
        elif kind is Layout.TWO_VERTICAL:
            layout = QVBoxLayout(inner)
            for slot in visible:
                layout.addWidget(slot, 1)
        elif kind is Layout.THREE_PYRAMID:
            layout = QVBoxLayout(inner)
            layout.addWidget(visible[0], 1)
            bottom = QHBoxLayout()
            bottom.addWidget(visible[1], 1)
            bottom.addWidget(visible[2], 1)
            layout.addLayout(bottom, 1)
        elif kind is Layout.THREE_PYRAMID_NARROW:
            layout = QVBoxLayout(inner)
            top = QHBoxLayout()
            top.addStretch(1)
            top.addWidget(visible[0], 2)
            top.addStretch(1)
            layout.addLayout(top, 1)
            bottom = QHBoxLayout()
            bottom.addWidget(visible[1], 1)
            bottom.addWidget(visible[2], 1)
            layout.addLayout(bottom, 1)
        else:
            layout = QVBoxLayout(inner)
            top = QHBoxLayout()
            top.addWidget(visible[0], 1)
            top.addWidget(visible[1], 1)
            bottom = QHBoxLayout()
            bottom.addWidget(visible[2], 1)
            bottom.addWidget(visible[3], 1)
            layout.addLayout(top, 1)
            layout.addLayout(bottom, 1)

        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        for slot in visible:
            slot.show()

    def apply_grid(self, rows: int, cols: int) -> None:
        count = rows * cols
        self.ensure_slots(count)
        inner = self._replace_inner()
        grid = QGridLayout(inner)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(8)
        for index in range(count):
            slot = self.slots[index]
            grid.addWidget(slot, index // cols, index % cols)
            slot.show()

    def _new_inner(self) -> QWidget:
        inner = QWidget(self)
        inner.setObjectName("previewInner")
        return inner

    def _replace_inner(self) -> QWidget:
        """Rebuild the mosaic host without destroying slot widgets.

        Unused cells stay parented to the canvas. If they remained children of
        the old inner, ``deleteLater`` would wipe them and the next layout
        change would leave an empty preview.
        """
        for slot in self.slots:
            slot.setParent(self)
            slot.hide()
        inner = self._new_inner()
        old = self._inner
        self._root.replaceWidget(old, inner)
        self._inner = inner
        old.deleteLater()
        return inner
