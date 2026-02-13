CHECK_AUTH_JS = """
    var code = document.getElementById("code");
    var access_denied = document.getElementById("access_denied");
    var result;
    
    if (code) {
        result = {authorized: true, code: code.value};
    } else if (access_denied) {
        result = {authorized: false, message: access_denied.innerText};
    } else {
        result = {};
    }
    result;
"""

def _on_qt_page_load_finished(dialog, webview):
    frame = webview.page().currentFrame()
    try: #PySide does not QStrings
        from QtCore import QString
        jscode = QString(CHECK_AUTH_JS)
    except ImportError:
        jscode = CHECK_AUTH_JS
    res = frame.evaluateJavaScript(jscode)
    try:
        authorization = dict((_to_string(k), _to_string(v)) for (k, v) in res.toPyObject().items())
    except AttributeError: #PySide returns the result in pure Python
        authorization = dict((_to_string(k), _to_string(v)) for (k, v) in res.items())
    if "authorized" in authorization:
        dialog.authorization_code = authorization.get("code")
        dialog.close()

def _to_string(x):
    return str(x.toUtf8()) if hasattr(x,'toUtf8') else x
   
def get_code(url, size=(640, 480), title="Google authentication"):
    """Open a QT webkit window and return the access code."""
    try:
        from PyQt4 import QtCore, QtGui, QtWebKit
    except ImportError:
        try:
            from PySide6 import QtCore, QtWebEngineWidgets
            from PySide6.QtCore import QUrl
            from PySide6.QtWidgets import QApplication, QDialog, QGridLayout
            from PySide6.QtWebEngineCore import QWebEnginePage
            
            # Use WebEngine for PySide6
            app = QApplication.instance()
            if app is None:
                app = QApplication([])
            dialog = QDialog()
            dialog.setWindowTitle(title)
            dialog.resize(*size)
            webview = QtWebEngineWidgets.QWebEngineView()
            webpage = QWebEnginePage()
            webview.setPage(webpage)
            
            # For PySide6, we need to handle the load finished signal differently
            def on_load_finished(success):
                if success:
                    # Run JavaScript to get authorization code
                    webview.page().runJavaScript(CHECK_AUTH_JS, lambda result: handle_js_result(result, dialog))
            
            def handle_js_result(result, dialog):
                if result and isinstance(result, dict) and "authorized" in result:
                    dialog.authorization_code = result.get("code")
                    dialog.close()
            
            webpage.loadFinished.connect(on_load_finished)
            webview.setUrl(QUrl(url))
            layout = QGridLayout()
            layout.addWidget(webview)
            dialog.setLayout(layout)
            dialog.authorization_code = None
            dialog.show()
            app.exec_()
            return dialog.authorization_code
        except ImportError:
            from PySide import QtCore, QtGui, QtWebKit
    app = QtGui.QApplication([])
    dialog = QtGui.QDialog()
    dialog.setWindowTitle(title)
    dialog.resize(*size)
    webview = QtWebKit.QWebView()
    webpage = QtWebKit.QWebPage()
    webview.setPage(webpage)           
    webpage.loadFinished.connect(lambda: _on_qt_page_load_finished(dialog, webview))
    webview.setUrl(QtCore.QUrl.fromEncoded(url))
    layout = QtGui.QGridLayout()
    layout.addWidget(webview)
    dialog.setLayout(layout)
    dialog.authorization_code = None
    dialog.show()
    app.exec_()
    return dialog.authorization_code
