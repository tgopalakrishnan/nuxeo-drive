"""
Created on 8 mai 2014

@author: Remi Cattiau
"""
from PyQt4 import QtGui, QtCore
from nxdrive.gui.folders_treeview import FolderTreeview, FilteredFsClient
from nxdrive.wui.translator import Translator


class FiltersDialog(QtGui.QDialog):

    def apply_filters(self):
        for item in self.treeview.getDirtyItems():
            path = item.get_path()
            if item.get_checkstate() == QtCore.Qt.Unchecked:
                self._engine.add_filter(path)
            elif item.get_checkstate() == QtCore.Qt.Checked:
                self._engine.remove_filter(path)
            elif item.get_old_value() == QtCore.Qt.Unchecked:
                # Now partially checked and was before a filter

                # Remove current parent filter and need to commit to enable the
                # add
                self._engine.remove_filter(path)
                # We need to browse every child and create a filter for
                # unchecked as they are not dirty but has become root filter
                for child in item.get_children():
                    if child.get_checkstate() == QtCore.Qt.Unchecked:
                        self._engine.add_filter(child.get_path())

        # Need to refresh the client for now
        # TO_REVIEW Check if we still need to invalidate_cache

    def accept(self):
        self.apply_filters()
        super(FiltersDialog, self).accept()

    def _get_tree_view(self):
        filters = self._engine.get_dao().get_filters()
        fs_client = self._engine.get_remote_client(filtered=False)
        client = FilteredFsClient(fs_client, filters)
        return FolderTreeview(self, client)

    def __init__(self, application, engine, parent=None):
        """
        Constructor
        """
        super(FiltersDialog, self).__init__(parent)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setWindowTitle(Translator.get("FILTERS_WINDOW_TITLE"))

        self.resize(491, 443)
        self.verticalLayout = QtGui.QVBoxLayout(self)
        self.verticalLayout.setContentsMargins(0, 0, 0, 0)

        self._engine = engine
        self._application = application
        icon = self._application.get_window_icon()
        if icon is not None:
            self.setWindowIcon(QtGui.QIcon(icon))

        self.treeview = self._get_tree_view()
        self.verticalLayout.addWidget(self.treeview)

        self.buttonBox = QtGui.QDialogButtonBox(self)
        self.buttonBox.setOrientation(QtCore.Qt.Horizontal)
        self.buttonBox.setStandardButtons(QtGui.QDialogButtonBox.Cancel
                                          | QtGui.QDialogButtonBox.Ok)
        self.verticalLayout.addWidget(self.buttonBox)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)
