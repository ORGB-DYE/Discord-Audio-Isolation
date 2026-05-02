#!/usr/bin/env python3
# ~/bin/discord-audio-isolator.py - GENTLE FIX (preserves audio quality)

import sys
import subprocess
import json
import threading
import time
import signal
import os
from typing import Optional, List, Tuple
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QDialog,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QIcon, QColor, QPixmap, QPainter, QBrush

# ============= Configuration =============
SINK_NAME = "discord-isolated-sink"
LOOPBACK_PROC = None
ROUTING_ACTIVE = False
CURRENT_SELECTION = None
AUDIO_MONITOR = None

# ============= WirePlumber Policy =============
WP_POLICY_DIR = os.path.expanduser("~/.config/wireplumber/main.lua.d")
WP_POLICY_FILE = os.path.join(WP_POLICY_DIR, "99-discord-capture-block.lua")
WP_POLICY_CONTENT = '''\
rule = {
  matches = {
    {
      { "node.name", "equals", "discord_capture" },
    },
  },
  apply_properties = {
    ["node.autoconnect"] = false,
    ["session.suspend-timeout-seconds"] = 0,
  },
}
table.insert(alsa_monitor.rules, rule)
'''

def install_wp_policy():
    try:
        os.makedirs(WP_POLICY_DIR, exist_ok=True)
        with open(WP_POLICY_FILE, "w") as f:
            f.write(WP_POLICY_CONTENT)
        subprocess.run(["systemctl", "--user", "reload", "wireplumber"], 
                      capture_output=True, timeout=5)
        print("[✓] WirePlumber policy installed")
        time.sleep(0.5)
        return True
    except Exception as e:
        print(f"[!] Failed to install WirePlumber policy: {e}")
        return False

def remove_wp_policy():
    if os.path.exists(WP_POLICY_FILE):
        try:
            os.remove(WP_POLICY_FILE)
            subprocess.run(["systemctl", "--user", "reload", "wireplumber"], 
                          capture_output=True, timeout=5)
            print("[✓] WirePlumber policy removed")
        except Exception as e:
            print(f"[!] Failed to remove WirePlumber policy: {e}")

# ============= PipeWire Utilities =============
def pw_dump():
    result = subprocess.run(["pw-dump"], capture_output=True, text=True)
    return json.loads(result.stdout)

def get_running_dc_nodes(data):
    return [
        n["id"] for n in data
        if n.get("info", {}).get("props", {}).get("node.name") == "discord_capture"
        and n.get("info", {}).get("state") in ("running", "idle")
    ]

def get_sink_node_id(data, sink_name):
    for n in data:
        if n.get("info", {}).get("props", {}).get("node.name") == sink_name:
            return n["id"]
    return None

def get_ports_for_node(data, node_id, direction):
    ports = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Port":
            continue
        info = obj.get("info", {})
        props = info.get("props", {})
        if props.get("node.id") == node_id and info.get("direction") == direction:
            ports.append(obj["id"])
    return ports

def get_links_into_nodes(data, node_ids):
    links = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Link":
            continue
        if obj.get("info", {}).get("input-node-id") in node_ids:
            links.append(obj["id"])
    return links

def destroy_links(link_ids):
    for lid in link_ids:
        subprocess.run(["pw-cli", "destroy", str(lid)], capture_output=True)

# ============= Audio Routing =============
def get_default_sink():
    result = subprocess.run(["pactl", "get-default-sink"], 
                           capture_output=True, text=True)
    return result.stdout.strip()

def get_all_sink_inputs() -> List[Tuple[str, str]]:
    inputs = []
    current_id = None
    current_name = None
    
    result = subprocess.run(["pactl", "list", "sink-inputs"], 
                           capture_output=True, text=True)
    
    for line in result.stdout.splitlines():
        if "Sink Input #" in line:
            if current_id and current_name:
                inputs.append((current_id, current_name))
            current_id = line.split("#")[1].strip()
            current_name = None
        elif "application.name =" in line:
            current_name = line.split("=")[1].strip().strip('"')
    
    if current_id and current_name:
        inputs.append((current_id, current_name))
    
    return inputs

def move_sink_input(sink_input_id: str, target_sink: str):
    subprocess.run(["pactl", "move-sink-input", sink_input_id, target_sink], 
                  capture_output=True)

def route_single_stream(input_id: str, app_name: str):
    """Route a single stream - only called when a brand new stream appears"""
    global CURRENT_SELECTION, ROUTING_ACTIVE
    
    if not ROUTING_ACTIVE:
        return
    
    default_sink = get_default_sink()
    
    if CURRENT_SELECTION == "__SYSTEM__":
        move_sink_input(input_id, SINK_NAME)
        print(f"  → [NEW] '{app_name}' → STREAM")
    elif CURRENT_SELECTION is None:
        move_sink_input(input_id, default_sink)
        print(f"  → [NEW] '{app_name}' → BLOCKED (muted)")
    else:
        if app_name == CURRENT_SELECTION:
            move_sink_input(input_id, SINK_NAME)
            print(f"  → [NEW] '{app_name}' → STREAM (selected app)")
        else:
            move_sink_input(input_id, default_sink)
            print(f"  → [NEW] '{app_name}' → BLOCKED")

# ============= Audio Monitor Thread - GENTLE =============
class AudioMonitor(QThread):
    def __init__(self):
        super().__init__()
        self._running = True
        self._known_streams = set()  # Track known stream IDs
        
    def run(self):
        print("[*] Audio monitor started - gentle mode (no quality impact)")
        
        while self._running:
            try:
                if not ROUTING_ACTIVE:
                    time.sleep(0.5)
                    continue
                
                # Get current sink inputs
                current_streams = set()
                sink_inputs = get_all_sink_inputs()
                
                for input_id, app_name in sink_inputs:
                    stream_key = f"{input_id}:{app_name}"
                    current_streams.add(stream_key)
                    
                    # Only act on BRAND NEW streams (not reused IDs)
                    if stream_key not in self._known_streams:
                        print(f"[*] New audio stream detected: '{app_name}'")
                        route_single_stream(input_id, app_name)
                
                # Update known streams
                self._known_streams = current_streams
                
            except Exception as e:
                print(f"[!] Monitor error: {e}")
            
            time.sleep(1)  # Check every second - gentle
    
    def stop(self):
        self._running = False
        self.wait()

def route_initial_streams(selected_app: str) -> Tuple[int, int]:
    """Initial routing of all existing streams"""
    global AUDIO_MONITOR
    
    default_sink = get_default_sink()
    sink_inputs = get_all_sink_inputs()
    
    routed_to_sink = 0
    routed_to_default = 0
    
    print(f"[*] Initial routing - Selected app: '{selected_app}'")
    print(f"[*] Default sink: {default_sink}")
    print(f"[*] Virtual sink: {SINK_NAME}")
    
    for input_id, app_name in sink_inputs:
        if app_name == selected_app:
            move_sink_input(input_id, SINK_NAME)
            routed_to_sink += 1
            print(f"  → '{app_name}' → VIRTUAL SINK (stream hears)")
            # Track this stream in the monitor if it exists
            if AUDIO_MONITOR:
                AUDIO_MONITOR._known_streams.add(f"{input_id}:{app_name}")
        else:
            move_sink_input(input_id, default_sink)
            routed_to_default += 1
            print(f"  → '{app_name}' → DEFAULT SINK (blocked)")
            if AUDIO_MONITOR:
                AUDIO_MONITOR._known_streams.add(f"{input_id}:{app_name}")
    
    return routed_to_sink, routed_to_default

def reset_all_audio():
    """Reset all audio routing to default sink"""
    global ROUTING_ACTIVE, AUDIO_MONITOR
    
    print("[*] Resetting audio routing...")
    ROUTING_ACTIVE = False
    
    if AUDIO_MONITOR:
        AUDIO_MONITOR.stop()
        AUDIO_MONITOR = None
    
    default_sink = get_default_sink()
    sink_inputs = get_all_sink_inputs()
    
    for input_id, app_name in sink_inputs:
        move_sink_input(input_id, default_sink)
        print(f"  → '{app_name}' → DEFAULT SINK")
    
    # Don't destroy the sink - keep it for next time
    print("[✓] Reset complete")

def get_audio_apps() -> List[str]:
    apps = set()
    for _, app_name in get_all_sink_inputs():
        if app_name and app_name.lower() not in ["discord", "discorddevelopment"]:
            apps.add(app_name)
    return sorted(list(apps))

# ============= Virtual Sink Management =============
def create_virtual_sink() -> bool:
    global LOOPBACK_PROC
    
    # Check if already exists - if so, reuse it
    result = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    if SINK_NAME in result.stdout:
        print("[*] Using existing virtual sink")
        return True
    
    try:
        LOOPBACK_PROC = subprocess.Popen([
            "pw-loopback",
            "--capture-props",
            f"media.class=Audio/Sink,node.name={SINK_NAME},node.description=Discord_Audio_Isolator",
            "--playback-props",
            f"media.class=Audio/Source,node.name={SINK_NAME}-src"
        ])
        time.sleep(1)
        
        print(f"[✓] Virtual sink created")
        return True
    except Exception as e:
        print(f"[!] Failed to create virtual sink: {e}")
        return False

def link_discord_capture_nodes() -> bool:
    print("[*] Linking discord_capture nodes to virtual sink...")
    
    # Wait for nodes
    dc_nodes = []
    for attempt in range(30):
        data = pw_dump()
        dc_nodes = get_running_dc_nodes(data)
        if dc_nodes:
            print(f"[*] Found {len(dc_nodes)} discord_capture nodes")
            break
        time.sleep(0.2)
    
    if not dc_nodes:
        print("[!] No discord_capture nodes found")
        return False
    
    data = pw_dump()
    sink_id = get_sink_node_id(data, SINK_NAME)
    if not sink_id:
        print("[!] Virtual sink not found")
        return False
    
    # Destroy existing links to avoid duplication
    existing_links = get_links_into_nodes(data, dc_nodes)
    if existing_links:
        print(f"[*] Destroying {len(existing_links)} old links")
        destroy_links(existing_links)
        time.sleep(0.3)
    
    data = pw_dump()
    sink_out_ports = get_ports_for_node(data, sink_id, "output")
    if not sink_out_ports:
        print("[!] No output ports on virtual sink")
        return False
    
    linked = 0
    for dc_id in dc_nodes:
        data = pw_dump()
        cap_in_ports = get_ports_for_node(data, dc_id, "input")
        if not cap_in_ports:
            continue
        
        for out_port, in_port in zip(sink_out_ports[:2], cap_in_ports[:2]):
            result = subprocess.run(["pw-link", str(out_port), str(in_port)], 
                                   capture_output=True, text=True)
            if result.returncode == 0:
                linked += 1
    
    print(f"[✓] Created {linked} links")
    return linked > 0

def redirect_discord_audio(selected_app: Optional[str]):
    """Main function to redirect Discord's audio capture"""
    global CURRENT_SELECTION, ROUTING_ACTIVE, AUDIO_MONITOR
    
    CURRENT_SELECTION = selected_app
    
    if selected_app is None:
        print("[*] No audio selected - Discord will capture silence")
        if not create_virtual_sink():
            return False
        success = link_discord_capture_nodes()
        if success:
            ROUTING_ACTIVE = True
            AUDIO_MONITOR = AudioMonitor()
            AUDIO_MONITOR.start()
        return success
    
    if not create_virtual_sink():
        return False
    
    if selected_app == "__SYSTEM__":
        print("[*] System-wide mode")
    else:
        routed_to_sink, routed_to_default = route_initial_streams(selected_app)
        print(f"[✓] Routed {routed_to_sink} to virtual sink, {routed_to_default} blocked")
        
        if routed_to_sink == 0:
            print(f"[!] Warning: '{selected_app}' not found - play audio first")
    
    success = link_discord_capture_nodes()
    
    if success:
        ROUTING_ACTIVE = True
        AUDIO_MONITOR = AudioMonitor()
        AUDIO_MONITOR.start()
        
        if selected_app == "__SYSTEM__":
            print("[✓] Sharing entire system audio")
        elif selected_app is None:
            print("[✓] Stream muted")
        else:
            print(f"[✓] Sharing ONLY '{selected_app}'")
            print("[✓] Monitor will catch new apps (gentle mode, no quality impact)")
    
    return success

# ============= Screenshare Detection =============
class ScreenshareDetector(QThread):
    screenshare_started = pyqtSignal()
    screenshare_stopped = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self._running = True
        self._was_sharing = False
        self._last_stop = 0
    
    def is_screenshare_active(self) -> bool:
        try:
            data = pw_dump()
            for node in data:
                props = node.get("info", {}).get("props", {})
                state = node.get("info", {}).get("state", "")
                if props.get("node.name") == "discord_capture" and state == "running":
                    return True
            return False
        except:
            return False
    
    def run(self):
        print("[*] Screenshare detector started")
        while self._running:
            active = self.is_screenshare_active()
            now = time.time()
            
            if active and not self._was_sharing and now - self._last_stop > 2:
                self._was_sharing = True
                print("[→] Discord screenshare detected")
                self.screenshare_started.emit()
            elif not active and self._was_sharing:
                self._was_sharing = False
                self._last_stop = now
                print("[←] Discord screenshare ended")
                self.screenshare_stopped.emit()
            
            self.msleep(1000)
    
    def stop(self):
        self._running = False
        self.wait()

# ============= UI Components =============
class AppPickerDialog(QDialog):
    def __init__(self, apps: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Audio Source for Discord")
        self.setMinimumWidth(450)
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        
        layout = QVBoxLayout(self)
        
        info_label = QLabel(
            "<b>🎤 Discord Screenshare Detected!</b><br><br>"
            "Select which audio source to share:<br>"
            "<i>• Only the selected app will be heard on stream</i><br>"
            "<i>• New browser tabs and apps are automatically blocked</i><br>"
            "<i>• High quality audio - no continuous rerouting</i>"
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        
        self.list_widget = QListWidget()
        self.list_widget.addItem("🔊 Entire system audio")
        self.list_widget.addItem("🔇 No audio (mute stream)")
        
        if apps:
            self.list_widget.addItem("")
            self.list_widget.addItem("─── Applications ───")
            for app in apps:
                self.list_widget.addItem(f"🎮 {app}")
        
        if not apps:
            self.list_widget.addItem("")
            self.list_widget.addItem("⚠️ No audio-playing apps detected")
        
        self.list_widget.setCurrentRow(0)
        layout.addWidget(self.list_widget)
        
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("Apply")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
    
    def get_selection(self):
        item = self.list_widget.currentItem()
        if not item:
            return None
        
        text = item.text()
        if "Entire system" in text:
            return "__SYSTEM__"
        elif "No audio" in text:
            return None
        elif "───" in text or not text.strip() or "⚠️" in text:
            return None
        else:
            return text.replace("🎮 ", "").strip()

class SystemTrayApp(QSystemTrayIcon):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.detector = ScreenshareDetector()
        
        self.setup_tray()
        self.detector.screenshare_started.connect(self.on_screenshare_start)
        self.detector.screenshare_stopped.connect(self.on_screenshare_stop)
        
        self.detector.start()
        self.setVisible(True)
        
        install_wp_policy()
        
        self.showMessage(
            "Discord Audio Isolator",
            "✓ Running\nStart a Discord screenshare to configure audio",
            QSystemTrayIcon.MessageIcon.Information,
            3000
        )
    
    def setup_tray(self):
        self.setIcon(self.create_icon(False))
        self.setToolTip("Discord Audio Isolator")
        
        self.menu = QMenu()
        self.status_action = self.menu.addAction("Status: Ready")
        self.status_action.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Quit").triggered.connect(self.quit_app)
        self.setContextMenu(self.menu)
    
    def setup_signals(self):
        self.detector.screenshare_started.connect(self.on_screenshare_start)
        self.detector.screenshare_stopped.connect(self.on_screenshare_stop)
    
    def create_icon(self, active: bool) -> QIcon:
        color = "#4caf50" if active else "#9e9e9e"
        px = QPixmap(22, 22)
        px.fill(QColor(0, 0, 0, 0))
        painter = QPainter(px)
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(QColor(color))
        painter.drawEllipse(2, 2, 18, 18)
        painter.end()
        return QIcon(px)
    
    def on_screenshare_start(self):
        print("[*] Screenshare started")
        time.sleep(1)
        
        apps = get_audio_apps()
        
        dialog = AppPickerDialog(apps)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            print("[*] User cancelled")
            return
        
        selection = dialog.get_selection()
        
        thread = threading.Thread(target=self.apply_routing, args=(selection,), daemon=True)
        thread.start()
    
    def apply_routing(self, selection):
        success = redirect_discord_audio(selection)
        if success:
            self.setIcon(self.create_icon(True))
            if selection == "__SYSTEM__":
                self.status_action.setText("Status: System audio")
            elif selection is None:
                self.status_action.setText("Status: Muted")
            else:
                self.status_action.setText(f"Status: {selection[:20]}")
    
    def on_screenshare_stop(self):
        print("[*] Screenshare ended")
        self.setIcon(self.create_icon(False))
        self.status_action.setText("Status: Ready")
        reset_all_audio()
    
    def quit_app(self):
        print("[*] Quitting...")
        self.detector.stop()
        reset_all_audio()
        if LOOPBACK_PROC:
            LOOPBACK_PROC.terminate()
        remove_wp_policy()
        self.app.quit()

# ============= Main =============
def main():
    if subprocess.run(["which", "pw-loopback"], capture_output=True).returncode != 0:
        print("Error: pw-loopback not found. Install pipewire-utils")
        print("On CachyOS: sudo pacman -S pipewire-utils")
        sys.exit(1)
    
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Error: System tray not available!")
        sys.exit(1)
    
    tray = SystemTrayApp(app)
    sys.exit(app.exec())

if __name__ == "__main__":
    main()