"""Explicit QObject receiver for worker callbacks that update the GUI."""
from PySide6.QtCore import QObject, Slot


class GuiJobReceiver(QObject):
    def __init__(self, parent, item, error, finished):
        super().__init__(parent)
        self.item_callback, self.error_callback, self.finish_callback = item, error, finished

    @Slot(object)
    def item(self, value):
        self.item_callback(value)

    @Slot(str)
    def error(self, message):
        self.error_callback(message)

    @Slot()
    def finished(self):
        self.finish_callback()
        self.deleteLater()
