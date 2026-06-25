# -*- coding: utf-8 -*-
def classFactory(iface):
    import os, shutil
    plugin_dir = os.path.dirname(__file__)
    for root, dirs, files in os.walk(plugin_dir):
        for d in dirs:
            if d == '__pycache__':
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    from .plugin import QCTWatershedPlugin
    return QCTWatershedPlugin(iface)
