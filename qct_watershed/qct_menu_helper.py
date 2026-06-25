"""
qct_menu_helper.py  v1.1
Shared utility for all QCivilTools plugins.

Each plugin copies this file into its own package directory.
It provides:
  - get_or_create_qct_menu()   : create/find the top-level QCivilTools menu
  - remove_action_from_qct_menu(): safely remove an action (cleans up empty menu)
  - register_about_action()    : called by qct_about to pin the About action at
                                  the bottom, after a separator
  - unregister_about_action()  : called by qct_about on unload
"""
from qgis.PyQt.QtWidgets import QMenu, QAction

MENU_OBJECT_NAME  = "QCivilToolsMenu"
MENU_TITLE        = "QCivilTools"
ABOUT_OBJECT_NAME = "QCivilToolsAboutAction"
SEP_OBJECT_NAME   = "QCivilToolsAboutSep"


# ── Core menu helpers ──────────────────────────────────────────────────────────

def get_or_create_qct_menu(iface):
    """Return the QCivilTools QMenu, creating it in the menu bar if absent."""
    existing = find_qct_menu(iface)
    if existing:
        return existing
    menu = QMenu(MENU_TITLE, iface.mainWindow().menuBar())
    menu.setObjectName(MENU_OBJECT_NAME)
    menu_bar = iface.mainWindow().menuBar()
    help_action = None
    for action in menu_bar.actions():
        if action.text().replace("&", "").lower() == "help":
            help_action = action
            break
    if help_action:
        menu_bar.insertMenu(help_action, menu)
    else:
        menu_bar.addMenu(menu)
    return menu


def find_qct_menu(iface):
    """Return the existing QCivilTools QMenu or None."""
    menu_bar = iface.mainWindow().menuBar()
    for action in menu_bar.actions():
        if action.menu() and action.menu().objectName() == MENU_OBJECT_NAME:
            return action.menu()
    return None


def remove_action_from_qct_menu(iface, action):
    """Remove an action; also remove the menu itself if it becomes empty."""
    if action is None:
        return
    menu = find_qct_menu(iface)
    if menu:
        menu.removeAction(action)
        # Only remove the whole menu if truly empty (About might still be there)
        visible = [a for a in menu.actions()
                   if not a.isSeparator()
                   and a.objectName() != ABOUT_OBJECT_NAME]
        if not visible:
            # Leave menu alive as long as About action is present
            about = _find_about_action(menu)
            if about is None:
                iface.mainWindow().menuBar().removeAction(menu.menuAction())


# ── About action helpers ───────────────────────────────────────────────────────

def register_about_action(iface, about_action):
    """
    Pin the About action at the very bottom of the QCivilTools menu,
    preceded by a separator.  Called by qct_about.plugin on initGui.
    """
    menu = get_or_create_qct_menu(iface)

    # Remove any stale separator / about from a previous session
    _remove_about_fixtures(menu)

    sep = menu.addSeparator()
    sep.setObjectName(SEP_OBJECT_NAME)

    about_action.setObjectName(ABOUT_OBJECT_NAME)
    menu.addAction(about_action)


def unregister_about_action(iface, about_action):
    """Remove the About action and its separator.  Called by qct_about on unload."""
    menu = find_qct_menu(iface)
    if menu is None:
        return
    _remove_about_fixtures(menu)
    # If the menu is now empty, remove it
    if not menu.actions():
        iface.mainWindow().menuBar().removeAction(menu.menuAction())


def _find_about_action(menu):
    for a in menu.actions():
        if a.objectName() == ABOUT_OBJECT_NAME:
            return a
    return None


def _remove_about_fixtures(menu):
    """Remove the separator and About action if present."""
    for a in list(menu.actions()):
        if a.objectName() in (SEP_OBJECT_NAME, ABOUT_OBJECT_NAME):
            menu.removeAction(a)
