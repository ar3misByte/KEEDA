"""KiCad Live - Action Plugin.

This plugin deliberately does very little. It shows a small dialog and then
launches the KiCad Live **agent as a separate process**.

Why not do the synchronising in here? Because an Action Plugin runs inside
KiCad's own Python interpreter and its wxWidgets event loop. A background
WebSocket thread living in that loop can hang or crash KiCad and take the
user's unsaved board with it. Running the agent out-of-process means the worst
a KiCad Live bug can do is stop syncing.

Installed by tools/install_plugin.py, which also writes kicad_live_config.json
next to this file recording where the repository and its virtualenv live.
"""
import json
import os
import subprocess
import sys

import pcbnew
import wx

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "kicad_live_config.json")
SETTINGS_PATH = os.path.join(HERE, "kicad_live_settings.json")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


class LaunchDialog(wx.Dialog):
    def __init__(self, parent, settings):
        wx.Dialog.__init__(self, parent, title="KiCad Live", size=(430, 290))

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(panel, label="Connect this KiCad to a KiCad Live session")
        font = heading.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        heading.SetFont(font)
        sizer.Add(heading, 0, wx.ALL, 12)

        grid = wx.FlexGridSizer(3, 2, 8, 10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Server IP:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.server = wx.TextCtrl(panel, value=settings.get("server", "192.168.1.50"))
        grid.Add(self.server, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.port = wx.TextCtrl(panel, value=str(settings.get("port", 8000)))
        grid.Add(self.port, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Your name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.name = wx.TextCtrl(panel, value=settings.get("name", "Designer A"))
        grid.Add(self.name, 1, wx.EXPAND)

        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        self.read_only = wx.CheckBox(panel, label="Read-only (receive changes, send none)")
        self.read_only.SetValue(bool(settings.get("read_only", False)))
        sizer.Add(self.read_only, 0, wx.ALL, 12)

        note = wx.StaticText(
            panel,
            label="The agent opens in its own console window.\n"
                  "Close that window to stop syncing.")
        note.SetForegroundColour(wx.Colour(110, 110, 110))
        sizer.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        panel.SetSizer(sizer)
        self.Centre()

    def values(self):
        try:
            port = int(self.port.GetValue().strip())
        except ValueError:
            port = 8000
        return {
            "server": self.server.GetValue().strip(),
            "port": port,
            "name": self.name.GetValue().strip() or "Designer",
            "read_only": self.read_only.GetValue(),
        }


class KiCadLivePlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "KiCad Live - Start collaboration"
        self.category = "Collaboration"
        self.description = "Connect this board to a KiCad Live sync server"
        self.show_toolbar_button = True
        icon = os.path.join(HERE, "icon.png")
        if os.path.exists(icon):
            self.icon_file_name = icon

    def Run(self):
        parent = wx.GetActiveWindow()
        config = load_json(CONFIG_PATH, {})
        repo_root = config.get("repo_root")
        python_exe = config.get("python_exe")

        if not repo_root or not os.path.isdir(repo_root):
            wx.MessageBox(
                "KiCad Live is not configured.\n\n"
                "Run this from the repository, with KiCad closed:\n\n"
                "    python tools/install_plugin.py\n\n"
                "That records where the code and its virtualenv live.",
                "KiCad Live", wx.OK | wx.ICON_ERROR, parent)
            return

        if not python_exe or not os.path.exists(python_exe):
            wx.MessageBox(
                "The Python interpreter recorded for KiCad Live no longer exists:\n\n"
                "%s\n\nRe-run  python tools/install_plugin.py" % python_exe,
                "KiCad Live", wx.OK | wx.ICON_ERROR, parent)
            return

        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox("Open a board before starting KiCad Live.",
                          "KiCad Live", wx.OK | wx.ICON_WARNING, parent)
            return

        settings = load_json(SETTINGS_PATH, {})
        dialog = LaunchDialog(parent, settings)
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        values = dialog.values()
        dialog.Destroy()
        save_json(SETTINGS_PATH, values)

        command = [python_exe, "-m", "agent.main",
                   "--server", values["server"], "--port", str(values["port"]),
                   "--name", values["name"]]
        if values["read_only"]:
            command.append("--read-only")

        try:
            creation_flags = 0
            if sys.platform == "win32":
                # Give the agent its own console so its log is visible and it
                # can be stopped independently of KiCad.
                creation_flags = subprocess.CREATE_NEW_CONSOLE
            subprocess.Popen(command, cwd=repo_root, creationflags=creation_flags)
        except Exception as exc:
            wx.MessageBox("Could not start the KiCad Live agent:\n\n%s" % exc,
                          "KiCad Live", wx.OK | wx.ICON_ERROR, parent)
            return

        wx.MessageBox(
            "KiCad Live agent started.\n\n"
            "Server:  %s:%s\n"
            "Name:    %s\n\n"
            "Watch the new console window for its status.\n"
            "Dashboard: http://%s:%s/"
            % (values["server"], values["port"], values["name"],
               values["server"], values["port"]),
            "KiCad Live", wx.OK | wx.ICON_INFORMATION, parent)


KiCadLivePlugin().register()
