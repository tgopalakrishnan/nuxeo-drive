from PyQt4 import QtCore
from nxdrive.logging_config import get_logger
from nxdrive.wui.dialog import WebDialog
from nxdrive.wui.translator import Translator
import urllib2

log = get_logger(__name__)


class WebAuthenticationApi(QtCore.QObject):

    def __init__(self, settings_api, callback_params):
        super(WebAuthenticationApi, self).__init__()
        self._settings_api = settings_api
        self._callback_params = callback_params

    def set_dialog(self, dlg):
        self._dialog = dlg

    @QtCore.pyqtSlot(str, str)
    def create_account(self, username, token):
        error = None
        try:
            username = str(username)
            token = str(token)
            local_folder = self._callback_params['local_folder']
            server_url = self._callback_params['server_url']
            engine_name = self._callback_params['engine_name']
            log.debug('Creating new account [%s, %s, %s]', local_folder, server_url, username)
            self._settings_api.create_account(local_folder, server_url, username, token, engine_name)
            self._settings_api.set_new_local_folder(local_folder)
        except urllib2.URLError as e:
            log.exception(e)
            if e.errno == 61:
                error = 'CONNECTION_REFUSED'
            else:
                error = 'CONNECTION_ERROR'
        except urllib2.HTTPError as e:
            log.exception(e)
            error = 'CONNECTION_ERROR'
        except:
            log.exception('Unexpected error while trying to create a new account [%s, %s, %s]',
                          local_folder, server_url, username)
            error = 'CONNECTION_UNKNOWN'
        finally:
            self._dialog.accept()
            if error is not None:
                self._settings_api.set_account_creation_error(error)
            self._settings_api.get_dialog().get_view().reload()


class WebAuthenticationDialog(WebDialog):

    def __init__(self, application, url, api):
        super(WebAuthenticationDialog, self).__init__(application, url,
                                                      title=Translator.get("WEB_AUTHENTICATION_WINDOW_TITLE"), api=api)
        self.resize(1000, 800)
