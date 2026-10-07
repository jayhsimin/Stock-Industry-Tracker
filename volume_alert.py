"""Windows toast alerts for stocks whose trading volume spikes - checked
every time build_dashboard_live.py runs (every minute, via the scheduled
task). Uses Shioaji's snapshot volume_ratio: today's per-minute average
volume so far, divided by the average per-minute volume over the past 5
trading days. ~1.0 is normal pace; higher means today is unusually busy
for that stock - this is the standard 量比 indicator, not an absolute
share count, so it's comparable across small- and large-cap stocks alike.

Each stock alerts at most once per trading day (state kept in
volume_alert_state.json, reset when the date changes) so a sustained
spike doesn't re-notify every single minute.

Toast delivery borrows PowerShell's own registered AppUserModelID -
Windows silently drops toasts from an unregistered app id, which is why
this isn't just an arbitrary string.
"""
import json
import os
import subprocess
import tempfile
from datetime import date

STATE_FILE = "volume_alert_state.json"
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def _xml_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _load_state(path):
    if not os.path.exists(path):
        return {"date": date.today().isoformat(), "alerted": []}
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    if state.get("date") != date.today().isoformat():
        return {"date": date.today().isoformat(), "alerted": []}
    return state


def _save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def send_toast(title, message):
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null
$xml = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{_xml_escape(title)}</text>
      <text>{_xml_escape(message)}</text>
    </binding>
  </visual>
</toast>
"@
$XmlDocument = New-Object Windows.Data.Xml.Dom.XmlDocument
$XmlDocument.LoadXml($xml)
$Toast = New-Object Windows.UI.Notifications.ToastNotification $XmlDocument
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{POWERSHELL_APP_ID}').Show($Toast)
"""
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        # utf-8-sig, not utf-8: Windows PowerShell 5.1 reads a BOM-less
        # script file using the system codepage (Big5/cp950 here), not
        # UTF-8, which garbled every Chinese character in the toast text.
        # The BOM is what tells it to read the file as UTF-8.
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        # CREATE_NO_WINDOW: without this, the child powershell.exe console
        # flashes on screen every time - even though this script itself may
        # be launched windowless via pythonw.exe, a spawned child process
        # gets its own window unless explicitly suppressed here.
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    finally:
        os.remove(path)


def check_and_alert(live, names, threshold=3.0, min_price=100.0, state_file=STATE_FILE):
    """live: {code: {"close":..., "pct":..., "volume_ratio":...}}.
    Returns the list of (code, name, volume_ratio) newly alerted this run."""
    state = _load_state(state_file)
    alerted_today = set(state["alerted"])
    new_alerts = []

    for code, info in live.items():
        vr = info.get("volume_ratio")
        if vr is None or vr < threshold or code in alerted_today:
            continue
        if info.get("close") is None or info["close"] <= min_price:
            continue
        name = names.get(code, code)
        send_toast(
            f"⚡ {name}({code}) 成交量異常放大",
            f"量比 {vr:.1f} 倍｜現價 {info['close']}｜漲跌 {info['pct']:+.2f}%",
        )
        alerted_today.add(code)
        new_alerts.append((code, name, vr))
        # save after every single alert, not just at the end - so a kill/crash
        # mid-run can't lose the "already alerted" record and cause a resend
        # storm on the next run (see commit message / conversation: an
        # unrealistically low test threshold once queued dozens of toasts
        # before being stopped, with nothing durable in between)
        state["alerted"] = sorted(alerted_today)
        _save_state(state_file, state)

    return new_alerts
