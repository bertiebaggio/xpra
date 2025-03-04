# This file is part of Xpra.
# Copyright (C) 2010 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011-2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import sys
import struct

from xpra.os_util import bytestostr, hexstr
from xpra.util import u, iround, envbool, envint, csv, ellipsizer
from xpra.os_util import is_unity, is_gnome, is_kde, is_Ubuntu, is_Fedora, is_X11, is_Wayland, saved_env
from xpra.log import Logger

log = Logger("posix")
eventlog = Logger("posix", "events")
screenlog = Logger("posix", "screen")
dbuslog = Logger("posix", "dbus")
traylog = Logger("posix", "tray")
mouselog = Logger("posix", "mouse")
xinputlog = Logger("posix", "xinput")

_X11Window = False
def X11WindowBindings():
    global _X11Window
    if _X11Window is False:
        _X11Window = None
        if is_X11():
            try:
                from xpra.x11.bindings.window_bindings import X11WindowBindings as _X11WindowBindings #@UnresolvedImport
                _X11Window = _X11WindowBindings()
            except Exception as e:
                log("X11WindowBindings()", exc_info=True)
                log.error("Error: no X11 bindings")
                log.error(" %s", e)
    return _X11Window

def X11RandRBindings():
    if is_X11():
        try:
            from xpra.x11.bindings.randr_bindings import RandRBindings  #@UnresolvedImport
            return RandRBindings()
        except Exception as e:
            log("RandRBindings()", exc_info=True)
            log.error("Error: no X11 RandR bindings")
            log.error(" %s", e)
    return None

X11XI2 = False
def X11XI2Bindings():
    global X11XI2
    if X11XI2 is False:
        X11XI2 = None
        if is_X11():
            try:
                from xpra.x11.bindings.xi2_bindings import X11XI2Bindings as _X11XI2Bindings       #@UnresolvedImport
                X11XI2 = _X11XI2Bindings()
            except Exception:
                log.error("no XI2 bindings", exc_info=True)
    return X11XI2


device_bell = None
GTK_MENUS = envbool("XPRA_GTK_MENUS", False)
RANDR_DPI = envbool("XPRA_RANDR_DPI", True)
XSETTINGS_DPI = envbool("XPRA_XSETTINGS_DPI", True)
USE_NATIVE_TRAY = envbool("XPRA_USE_NATIVE_TRAY", is_unity() or (is_Ubuntu() and is_gnome()) or (is_gnome() and not is_Fedora()) or is_kde())
XINPUT_WHEEL_DIV = envint("XPRA_XINPUT_WHEEL_DIV", 15)
DBUS_SCREENSAVER = envbool("XPRA_DBUS_SCREENSAVER", False)


def gl_check():
    if not is_X11() and is_Wayland():
        return "disabled under wayland with GTK3 (buggy)"
    return None


def get_native_system_tray_classes():
    return []

def get_wm_name():
    wm_name = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("XDG_SESSION_DESKTOP") or os.environ.get("DESKTOP_SESSION")
    if os.environ.get("XDG_SESSION_TYPE")=="wayland" or os.environ.get("GDK_BACKEND")=="wayland":
        if wm_name:
            wm_name += " on wayland"
        else:
            wm_name = "wayland"
    elif is_X11():
        try:
            wm_check = _get_X11_root_property("_NET_SUPPORTING_WM_CHECK", "WINDOW")
            if wm_check:
                xid = struct.unpack(b"@L", wm_check)[0]
                traylog("_NET_SUPPORTING_WM_CHECK window=%#x", xid)
                wm_name = _get_X11_window_property(xid, "_NET_WM_NAME", "UTF8_STRING")
                traylog("_NET_WM_NAME=%s", wm_name)
                if wm_name:
                    return u(wm_name)
        except Exception as e:
            traylog("get_wm_name()", exc_info=True)
            traylog.error("Error accessing window manager information:")
            traylog.error(" %s", e)
    return wm_name

def get_clipboard_native_class():
    if is_Wayland():
        return "xpra.gtk_common.gtk_clipboard.GTK_Clipboard"
    return "xpra.x11.gtk_x11.clipboard.X11Clipboard"

def get_native_tray_classes():
    #could restrict to only DEs that have a broken system tray like "GNOME Shell"?
    c = []
    if USE_NATIVE_TRAY:
        try:
            from xpra.platform.xposix.appindicator_tray import AppindicatorTray
            c.append(AppindicatorTray)
        except (ImportError, ValueError):
            traylog("cannot load appindicator tray", exc_info=True)
            traylog.warn("Warning: appindicator library not found")
            traylog.warn(" you may want to install libappindicator")
            traylog.warn(" to enable the system tray.")
            if saved_env.get("XDG_CURRENT_DESKTOP")=="GNOME":
                traylog.warn(" With gnome-shell, you may also need some extensions:")
                traylog.warn(" 'top icons plus' and / or 'appindicator'")
    traylog("get_native_tray_classes()=%s (USE_NATIVE_TRAY=%s)", c, USE_NATIVE_TRAY)
    return c


def get_native_notifier_classes():
    ncs = []
    try:
        from xpra.notifications.dbus_notifier import DBUS_Notifier_factory
        ncs.append(DBUS_Notifier_factory)
    except Exception as e:
        dbuslog("cannot load dbus notifier: %s", e)
    try:
        from xpra.notifications.pynotify_notifier import PyNotify_Notifier
        ncs.append(PyNotify_Notifier)
    except Exception as e:
        log("cannot load pynotify notifier: %s", e)
    return ncs


def get_session_type():
    return os.environ.get("XDG_SESSION_TYPE", "")


#we duplicate some of the code found in gtk_x11.prop ...
#which is still better than having dependencies on that GTK2 code
def _get_X11_window_property(xid, name, req_type):
    try:
        from xpra.gtk_common.error import xsync
        from xpra.x11.bindings.window_bindings import PropertyError #@UnresolvedImport
        try:
            with xsync:
                prop = X11WindowBindings().XGetWindowProperty(xid, name, req_type)
            log("_get_X11_window_property(%#x, %s, %s)=%s, len=%s", xid, name, req_type, type(prop), len(prop or []))
            return prop
        except PropertyError as e:
            log("_get_X11_window_property(%#x, %s, %s): %s", xid, name, req_type, e)
    except Exception as e:
        log.warn("Warning: failed to get X11 window property '%s' on window %#x: %s", name, xid, e)
        log("get_X11_window_property%s", (xid, name, req_type), exc_info=True)
    return None
def _get_X11_root_property(name, req_type):
    try:
        root_xid = X11WindowBindings().getDefaultRootWindow()
        return _get_X11_window_property(root_xid, name, req_type)
    except Exception as e:
        log("_get_X11_root_property(%s, %s)", name, req_type, exc_info=True)
        log.warn("Warning: failed to get X11 root property '%s'", name)
        log.warn(" %s", e)
    return None


def _get_xsettings():
    from xpra.gtk_common.error import xlog
    X11Window = X11WindowBindings()
    if not X11Window:
        return None
    with xlog:
        selection = "_XSETTINGS_S0"
        owner = X11Window.XGetSelectionOwner(selection)
        if not owner:
            return None
        XSETTINGS = "_XSETTINGS_SETTINGS"
        data = X11Window.XGetWindowProperty(owner, XSETTINGS, XSETTINGS)
        if not data:
            return None
        from xpra.x11.xsettings_prop import get_settings
        return get_settings(data)
    return None

def _get_xsettings_dict():
    d = {}
    if is_Wayland():
        return d
    v = _get_xsettings()
    if v:
        _, values = v
        for setting_type, prop_name, value, _ in values:
            d[bytestostr(prop_name)] = (setting_type, value)
    return d


def _get_xsettings_dpi():
    if XSETTINGS_DPI and is_X11():
        from xpra.x11.xsettings_prop import XSettingsTypeInteger
        d = _get_xsettings_dict()
        for k,div in {
            "Xft.dpi"         : 1,
            "Xft/DPI"         : 1024,
            "gnome.Xft/DPI"   : 1024,
            #"Gdk/UnscaledDPI" : 1024, ??
            }.items():
            if k in d:
                value_type, value = d.get(k)
                if value_type==XSettingsTypeInteger:
                    actual_value = max(10, min(1000, value//div))
                    screenlog("_get_xsettings_dpi() found %s=%s, div=%i, actual value=%i", k, value, div, actual_value)
                    return actual_value
    return -1

def _get_randr_dpi():
    if RANDR_DPI and not is_Wayland():
        from xpra.gtk_common.error import xlog
        with xlog:
            randr_bindings = X11RandRBindings()
            if randr_bindings and randr_bindings.has_randr():
                wmm, hmm = randr_bindings.get_screen_size_mm()
                if wmm>0 and hmm>0:
                    w, h =  randr_bindings.get_screen_size()
                    dpix = iround(w * 25.4 / wmm)
                    dpiy = iround(h * 25.4 / hmm)
                    screenlog("xdpi=%s, ydpi=%s - size-mm=%ix%i, size=%ix%i", dpix, dpiy, wmm, hmm, w, h)
                    return dpix, dpiy
    return -1, -1

def get_xdpi():
    dpi = _get_xsettings_dpi()
    if dpi>0:
        return dpi
    return _get_randr_dpi()[0]

def get_ydpi():
    dpi = _get_xsettings_dpi()
    if dpi>0:
        return dpi
    return _get_randr_dpi()[1]


def get_icc_info():
    if not is_Wayland():
        try:
            data = _get_X11_root_property("_ICC_PROFILE", "CARDINAL")
            if data:
                screenlog("_ICC_PROFILE=%s (%s)", type(data), len(data))
                version = _get_X11_root_property("_ICC_PROFILE_IN_X_VERSION", "CARDINAL")
                screenlog("get_icc_info() found _ICC_PROFILE_IN_X_VERSION=%s, _ICC_PROFILE=%s",
                          hexstr(version or ""), hexstr(data))
                icc = {
                        "source"    : "_ICC_PROFILE",
                        "data"      : data,
                        }
                if version:
                    try:
                        version = ord(version)
                    except TypeError:
                        pass
                    icc["version"] = version
                screenlog("get_icc_info()=%s", icc)
                return icc
        except Exception as e:
            screenlog.error("Error: cannot access _ICC_PROFILE X11 window property")
            screenlog.error(" %s", e)
            screenlog("get_icc_info()", exc_info=True)
    from xpra.platform.gui import default_get_icc_info
    return default_get_icc_info()


def get_antialias_info():
    info = {}
    try:
        from xpra.x11.xsettings_prop import XSettingsTypeInteger, XSettingsTypeString
        d = _get_xsettings_dict()
        for prop_name, name in {"Xft/Antialias"    : "enabled",
                                "Xft/Hinting"      : "hinting"}.items():
            if prop_name in d:
                value_type, value = d.get(prop_name)
                if value_type==XSettingsTypeInteger and value>0:
                    info[name] = bool(value)
        def get_contrast(value):
            #win32 API uses numerical values:
            #(this is my best guess at translating the X11 names)
            return {"hintnone"      : 0,
                    "hintslight"    : 1000,
                    "hintmedium"    : 1600,
                    "hintfull"      : 2200}.get(bytestostr(value))
        for prop_name, name, convert in (
                                         ("Xft/HintStyle",  "hintstyle",    bytestostr),
                                         ("Xft/HintStyle",  "contrast",     get_contrast),
                                         ("Xft/RGBA",       "orientation",  lambda x : bytestostr(x).upper())
                                         ):
            if prop_name in d:
                value_type, value = d.get(prop_name)
                if value_type==XSettingsTypeString:
                    cval = convert(value)
                    if cval is not None:
                        info[name] = cval
    except Exception as e:
        screenlog.warn("failed to get antialias info from xsettings: %s", e)
    screenlog("get_antialias_info()=%s", info)
    return info


def get_current_desktop():
    v = -1
    if not is_Wayland():
        d = None
        try:
            d = _get_X11_root_property("_NET_CURRENT_DESKTOP", "CARDINAL")
            if d:
                v = struct.unpack(b"@L", d)[0]
        except Exception as e:
            log.warn("failed to get current desktop: %s", e)
        log("get_current_desktop() %s=%s", hexstr(d or ""), v)
    return v

def get_workarea():
    if not is_Wayland():
        try:
            d = get_current_desktop()
            if d<0:
                return None
            workarea = _get_X11_root_property("_NET_WORKAREA", "CARDINAL")
            if not workarea:
                return None
            screenlog("get_workarea() _NET_WORKAREA=%s (%s), len=%s",
                      ellipsizer(workarea), type(workarea), len(workarea))
            #workarea comes as a list of 4 CARDINAL dimensions (x,y,w,h), one for each desktop
            sizeof_long = struct.calcsize(b"@L")
            if len(workarea)<(d+1)*4*sizeof_long:
                screenlog.warn("get_workarea() invalid _NET_WORKAREA value")
            else:
                cur_workarea = workarea[d*4*sizeof_long:(d+1)*4*sizeof_long]
                v = struct.unpack(b"@LLLL", cur_workarea)
                screenlog("get_workarea() %s=%s", hexstr(cur_workarea), v)
                return v
        except Exception as e:
            screenlog("get_workarea()", exc_info=True)
            screenlog.warn("Warning: failed to query workarea: %s", e)
    return None


def get_number_of_desktops():
    v = 0
    if not is_Wayland():
        d = None
        try:
            d = _get_X11_root_property("_NET_NUMBER_OF_DESKTOPS", "CARDINAL")
            if d:
                v = struct.unpack(b"@L", d)[0]
        except Exception as e:
            screenlog.warn("failed to get number of desktop: %s", e)
        v = max(1, v)
        screenlog("get_number_of_desktops() %s=%s", hexstr(d or ""), v)
    return v

def get_desktop_names():
    v = []
    if not is_Wayland():
        v = ["Main"]
        d = None
        try:
            d = _get_X11_root_property("_NET_DESKTOP_NAMES", "UTF8_STRING")
            if d:
                v = d.split(b"\0")
                if len(v)>1 and v[-1]==b"":
                    v = v[:-1]
                return [x.decode("utf8") for x in v]
        except Exception as e:
            screenlog.warn("failed to get desktop names: %s", e)
        screenlog("get_desktop_names() %s=%s", hexstr(d or ""), v)
    return v


def get_vrefresh():
    v = -1
    if not is_Wayland():
        try:
            from xpra.x11.bindings.randr_bindings import RandRBindings      #@UnresolvedImport
            randr = RandRBindings()
            if randr.has_randr():
                v = randr.get_vrefresh()
        except Exception as e:
            log("get_vrefresh()", exc_info=True)
            log.warn("Warning: failed to query the display vertical refresh rate:")
            log.warn(" %s", e)
        screenlog("get_vrefresh()=%s", v)
    return v


def _get_xresources():
    if not is_Wayland():
        try:
            from xpra.x11.gtk_x11.prop import prop_get
            from xpra.gtk_common.gtk_util import get_default_root_window
            root = get_default_root_window()
            value = prop_get(root, "RESOURCE_MANAGER", "latin1", ignore_errors=True)
            log("RESOURCE_MANAGER=%s", value)
            if value is None:
                return None
            #parse the resources into a dict:
            values={}
            options = value.split("\n")
            for option in options:
                if not option:
                    continue
                parts = option.split(":\t", 1)
                if len(parts)!=2:
                    log("skipped invalid option: '%s'", option)
                    continue
                values[parts[0]] = parts[1]
            return values
        except Exception as e:
            log("_get_xresources error: %s", e)
    return None

def get_cursor_size():
    d = _get_xresources() or {}
    try:
        return int(d.get("Xcursor.size", 0))
    except ValueError:
        return -1


def _get_xsettings_int(name, default_value):
    d = _get_xsettings_dict()
    if name not in d:
        return default_value
    value_type, value = d.get(name)
    from xpra.x11.xsettings_prop import XSettingsTypeInteger
    if value_type!=XSettingsTypeInteger:
        return default_value
    return value

def get_double_click_time():
    return _get_xsettings_int("Net/DoubleClickTime", -1)

def get_double_click_distance():
    v = _get_xsettings_int("Net/DoubleClickDistance", -1)
    return v, v

def get_window_frame_sizes():
    #for X11, have to create a window and then check the
    #_NET_FRAME_EXTENTS value after sending a _NET_REQUEST_FRAME_EXTENTS message,
    #so this is done in the gtk client instead of here...
    return {}


def system_bell(window, device, percent, _pitch, _duration, bell_class, bell_id, bell_name):
    if not is_X11():
        return False
    global device_bell
    if device_bell is False:
        #failed already
        return False
    from xpra.gtk_common.error import XError
    def x11_bell():
        global device_bell
        if device_bell is None:
            #try to load it:
            from xpra.x11.bindings.keyboard_bindings import X11KeyboardBindings       #@UnresolvedImport
            device_bell = X11KeyboardBindings().device_bell
        device_bell(window.get_xid(), device, bell_class, bell_id, percent, bell_name)
    try:
        from xpra.gtk_common.error import xlog
        with xlog:
            x11_bell()
        return  True
    except XError as e:
        log("x11_bell()", exc_info=True)
        log.error("Error using device_bell: %s", e)
        log.error(" switching native X11 bell support off")
        device_bell = False
        return False


def _send_client_message(window, message_type, *values):
    try:
        from xpra.x11.gtk_x11.gdk_display_source import init_gdk_display_source
        init_gdk_display_source()
        from xpra.x11.bindings.window_bindings import constants #@UnresolvedImport
        X11Window = X11WindowBindings()
        root_xid = X11Window.getDefaultRootWindow()
        if window:
            xid = window.get_xid()
        else:
            xid = root_xid
        SubstructureNotifyMask = constants["SubstructureNotifyMask"]
        SubstructureRedirectMask = constants["SubstructureRedirectMask"]
        event_mask = SubstructureNotifyMask | SubstructureRedirectMask
        from xpra.gtk_common.error import xsync
        with xsync:
            X11Window.sendClientMessage(root_xid, xid, False, event_mask, message_type, *values)
    except Exception as e:
        log.warn("failed to send client message '%s' with values=%s: %s", message_type, values, e)

def show_desktop(b):
    _send_client_message(None, "_NET_SHOWING_DESKTOP", int(bool(b)))

def set_fullscreen_monitors(window, fsm, source_indication=0):
    if not isinstance(fsm, (tuple, list)):
        log.warn("invalid type for fullscreen-monitors: %s", type(fsm))
        return
    if len(fsm)!=4:
        log.warn("invalid number of fullscreen-monitors: %s", len(fsm))
        return
    values = list(fsm)+[source_indication]
    _send_client_message(window, "_NET_WM_FULLSCREEN_MONITORS", *values)

def _toggle_wm_state(window, state, enabled):
    if enabled:
        action = 1  #"_NET_WM_STATE_ADD"
    else:
        action = 0  #"_NET_WM_STATE_REMOVE"
    _send_client_message(window, "_NET_WM_STATE", action, state)

def set_shaded(window, shaded):
    _toggle_wm_state(window, "_NET_WM_STATE_SHADED", shaded)



WINDOW_ADD_HOOKS = []
def add_window_hooks(window):
    global WINDOW_ADD_HOOKS
    for x in WINDOW_ADD_HOOKS:
        x(window)
    log("add_window_hooks(%s) added %s", window, WINDOW_ADD_HOOKS)

WINDOW_REMOVE_HOOKS = []
def remove_window_hooks(window):
    global WINDOW_REMOVE_HOOKS
    for x in WINDOW_REMOVE_HOOKS:
        x(window)
    log("remove_window_hooks(%s) added %s", window, WINDOW_REMOVE_HOOKS)


def get_info():
    from xpra.platform.gui import get_info_base
    i = get_info_base()
    s = _get_xsettings()
    if s:
        serial, values = s
        xi = {"serial"  : serial}
        for _,name,value,_ in values:
            xi[bytestostr(name)] = value
        i["xsettings"] = xi
    i.setdefault("dpi", {
                         "xsettings"    : _get_xsettings_dpi(),
                         "randr"        : _get_randr_dpi()
                         })
    return i


class XI2_Window:
    def __init__(self, window):
        log("XI2_Window(%s)", window)
        self.XI2 = X11XI2Bindings()
        self.X11Window = X11WindowBindings()
        self.window = window
        self.xid = window.get_window().get_xid()
        self.windows = ()
        self.motion_valuators = {}
        window.connect("configure-event", self.configured)
        self.configured()
        #replace event handlers with XI2 version:
        self._do_motion_notify_event = window._do_motion_notify_event
        window._do_motion_notify_event = self.noop
        window._do_button_press_event = self.noop
        window._do_button_release_event = self.noop
        window._do_scroll_event = self.noop
        window.connect("destroy", self.cleanup)

    def noop(self, *args):
        pass

    def cleanup(self, *_args):
        for window in self.windows:
            self.XI2.disconnect(window)
        self.windows = []
        self.window = None

    def configured(self, *_args):
        from xpra.gtk_common.error import xlog
        with xlog:
            self.windows = self.get_parent_windows(self.xid)
        for window in (self.windows or ()):
            self.XI2.connect(window, "XI_Motion", self.do_xi_motion)
            self.XI2.connect(window, "XI_ButtonPress", self.do_xi_button)
            self.XI2.connect(window, "XI_ButtonRelease", self.do_xi_button)
            self.XI2.connect(window, "XI_DeviceChanged", self.do_xi_device_changed)
            self.XI2.connect(window, "XI_HierarchyChanged", self.do_xi_hierarchy_changed)

    def do_xi_device_changed(self, *_args):
        self.motion_valuators = {}

    def do_xi_hierarchy_changed(self, *_args):
        self.motion_valuators = {}


    def get_parent_windows(self, oxid):
        windows = [oxid]
        root = self.X11Window.getDefaultRootWindow()
        xid = oxid
        while True:
            xid = self.X11Window.getParent(xid)
            if xid==0 or xid==root:
                break
            windows.append(xid)
        xinputlog("get_parent_windows(%#x)=%s", oxid, csv(hex(x) for x in windows))
        return windows


    def do_xi_button(self, event, device):
        window = self.window
        client = window._client
        if client.readonly:
            return
        xinputlog("do_xi_button(%s, %s) server_input_devices=%s", event, device, client.server_input_devices)
        if client.server_input_devices=="xi" or (client.server_input_devices=="uinput" and client.server_precise_wheel):
            #skip synthetic scroll events,
            #as the server should synthesize them from the motion events
            #those have the same serial:
            matching_motion = self.XI2.find_event("XI_Motion", event.serial)
            #maybe we need more to distinguish?
            if matching_motion:
                return
        button = event.detail
        depressed = (event.name == "XI_ButtonPress")
        args = self.get_pointer_extra_args(event)
        window._button_action(button, event, depressed, *args)

    def do_xi_motion(self, event, device):
        window = self.window
        if window.moveresize_event:
            xinputlog("do_xi_motion(%s, %s) handling as a moveresize event on window %s", event, device, window)
            window.motion_moveresize(event)
            self._do_motion_notify_event(event)
            return
        client = window._client
        if client.readonly:
            return
        pointer, relative_pointer, modifiers, buttons = window._pointer_modifiers(event)
        wid = self.window.get_mouse_event_wid(*pointer)
        #log("server_input_devices=%s, server_precise_wheel=%s",
        #    client.server_input_devices, client.server_precise_wheel)
        valuators = event.valuators
        unused_valuators = valuators.copy()
        dx, dy = 0, 0
        if (valuators and device and device.get("enabled") and
            client.server_input_devices=="uinput" and client.server_precise_wheel):
            XIModeRelative = 0
            classes = device.get("classes")
            val_classes = {}
            for c in classes.values():
                number = c.get("number")
                if number is not None and c.get("type")=="valuator" and c.get("mode")==XIModeRelative:
                    val_classes[number] = c
            #previous values:
            mv = self.motion_valuators.setdefault(event.device, {})
            last_x, last_y = 0, 0
            wheel_x, wheel_y = 0, 0
            unused_valuators = {}
            for number, value in valuators.items():
                valuator = val_classes.get(number)
                if valuator:
                    label = valuator.get("label")
                    if label:
                        mouselog("%s: %s", label, value)
                        if label.lower().find("horiz")>=0:
                            wheel_x = value
                            last_x = mv.get(number)
                            continue
                        elif label.lower().find("vert")>=0:
                            wheel_y = value
                            last_y = mv.get(number)
                            continue
                unused_valuators[number] = value
            #new absolute motion values:
            #calculate delta if we have both old and new values:
            if last_x is not None and wheel_x is not None:
                dx = last_x-wheel_x
            if last_y is not None and wheel_y is not None:
                dy = last_y-wheel_y
            #whatever happens, update our motion cached values:
            mv.update(event.valuators)
        #send plain motion first, if any:
        if unused_valuators:
            xinputlog("do_xi_motion(%s, %s) wid=%s / focus=%s / window wid=%i, device=%s, pointer=%s, modifiers=%s, buttons=%s",
                      event, device, wid, window._client._focused, window._id, event.device, pointer, modifiers, buttons)
            pdata = pointer
            if client.server_pointer_relative:
                pdata = list(pointer)+list(relative_pointer)
            packet = ["pointer-position", wid, pdata, modifiers, buttons] + self.get_pointer_extra_args(event)
            client.send_mouse_position(packet)
        #now see if we have anything to send as a wheel event:
        if dx!=0 or dy!=0:
            xinputlog("do_xi_motion(%s, %s) wheel deltas: dx=%i, dy=%i", event, device, dx, dy)
            #normalize (xinput is always using 15 degrees?)
            client.wheel_event(wid, dx/XINPUT_WHEEL_DIV, dy/XINPUT_WHEEL_DIV, event.device)

    def get_pointer_extra_args(self, event):
        def intscaled(f):
            return int(f*1000000), 1000000
        def dictscaled(d):
            return dict((k,intscaled(v)) for k,v in d.items())
        raw_valuators = {}
        raw_event_name = event.name.replace("XI_", "XI_Raw")    #ie: XI_Motion -> XI_RawMotion
        raw = self.XI2.find_event(raw_event_name, event.serial)
        #mouselog("raw(%s)=%s", raw_event_name, raw)
        if raw:
            raw_valuators = raw.raw_valuators
        args = [event.device]
        for x in ("x", "y", "x_root", "y_root"):
            args.append(intscaled(getattr(event, x)))
        for v in (event.valuators, raw_valuators):
            args.append(dictscaled(v))
        return args


class ClientExtras:
    def __init__(self, client, _opts):
        self.client = client
        self._xsettings_watcher = None
        self._root_props_watcher = None
        self.system_bus = None
        self.session_bus = None
        self.upower_resuming_match = None
        self.upower_sleeping_match = None
        self.login1_match = None
        self.screensaver_match = None
        self.x11_filter = None
        if client.xsettings_enabled:
            self.setup_xprops()
        self.xi_setup_failures = 0
        input_devices = getattr(client, "input_devices", None)
        if input_devices in ("xi", "auto"):
            #this would trigger warnings with our temporary opengl windows:
            #only enable it after we have connected:
            self.client.after_handshake(self.setup_xi)
        self.setup_dbus_signals()

    def ready(self):
        pass

    def init_x11_filter(self):
        if self.x11_filter:
            return
        try:
            from xpra.x11.gtk_x11.gdk_bindings import init_x11_filter  #@UnresolvedImport, @UnusedImport
            self.x11_filter = init_x11_filter()
            log("x11_filter=%s", self.x11_filter)
        except Exception as e:
            log("init_x11_filter()", exc_info=True)
            log.error("Error: failed to initialize X11 GDK filter:")
            log.error(" %s", e)
            self.x11_filter = None

    def cleanup(self):
        log("cleanup() xsettings_watcher=%s, root_props_watcher=%s", self._xsettings_watcher, self._root_props_watcher)
        if self.x11_filter:
            self.x11_filter = None
            from xpra.x11.gtk_x11.gdk_bindings import cleanup_x11_filter   #@UnresolvedImport, @UnusedImport
            cleanup_x11_filter()
        if self._xsettings_watcher:
            self._xsettings_watcher.cleanup()
            self._xsettings_watcher = None
        if self._root_props_watcher:
            self._root_props_watcher.cleanup()
            self._root_props_watcher = None
        if self.system_bus:
            bus = self.system_bus
            log("cleanup() system bus=%s, matches: %s",
                bus, (self.upower_resuming_match, self.upower_sleeping_match, self.login1_match))
            self.system_bus = None
            if self.upower_resuming_match:
                bus._clean_up_signal_match(self.upower_resuming_match)
                self.upower_resuming_match = None
            if self.upower_sleeping_match:
                bus._clean_up_signal_match(self.upower_sleeping_match)
                self.upower_sleeping_match = None
            if self.login1_match:
                bus._clean_up_signal_match(self.login1_match)
                self.login1_match = None
        if self.session_bus:
            if self.screensaver_match:
                self.session_bus._clean_up_signal_match(self.screensaver_match)
                self.screensaver_match = None
        global WINDOW_METHOD_OVERRIDES
        WINDOW_METHOD_OVERRIDES = {}

    def resuming_callback(self, *args):
        eventlog("resuming_callback%s", args)
        self.client.resume()

    def sleeping_callback(self, *args):
        eventlog("sleeping_callback%s", args)
        self.client.suspend()


    def setup_dbus_signals(self):
        try:
            import xpra.dbus
            assert xpra.dbus
        except ImportError as e:
            dbuslog("setup_dbus_signals()", exc_info=True)
            dbuslog.info("dbus support is not installed")
            dbuslog.info(" no support for power events")
            return
        try:
            from xpra.dbus.common import init_system_bus, init_session_bus
        except ImportError as e:
            dbuslog("setup_dbus_signals()", exc_info=True)
            dbuslog.error("Error: dbus bindings are missing,")
            dbuslog.error(" cannot setup event listeners:")
            dbuslog.error(" %s", e)
            return

        try:
            bus = init_system_bus()
            self.system_bus = bus
            dbuslog("setup_dbus_signals() system bus=%s", bus)
        except Exception as e:
            dbuslog("setup_dbus_signals()", exc_info=True)
            dbuslog.error("Error setting up dbus signals:")
            dbuslog.error(" %s", e)
        else:
            #the UPower signals:
            try:
                bus_name    = 'org.freedesktop.UPower'
                dbuslog("bus has owner(%s)=%s", bus_name, bus.name_has_owner(bus_name))
                iface_name  = 'org.freedesktop.UPower'
                self.upower_resuming_match = bus.add_signal_receiver(self.resuming_callback, 'Resuming', iface_name, bus_name)
                self.upower_sleeping_match = bus.add_signal_receiver(self.sleeping_callback, 'Sleeping', iface_name, bus_name)
                dbuslog("listening for 'Resuming' and 'Sleeping' signals on %s", iface_name)
            except Exception as e:
                dbuslog("failed to setup UPower event listener: %s", e)

            #the "logind" signals:
            try:
                bus_name    = 'org.freedesktop.login1'
                dbuslog("bus has owner(%s)=%s", bus_name, bus.name_has_owner(bus_name))
                def sleep_event_handler(suspend):
                    if suspend:
                        self.sleeping_callback()
                    else:
                        self.resuming_callback()
                iface_name  = 'org.freedesktop.login1.Manager'
                self.login1_match = bus.add_signal_receiver(sleep_event_handler, 'PrepareForSleep', iface_name, bus_name)
                dbuslog("listening for 'PrepareForSleep' signal on %s", iface_name)
            except Exception as e:
                dbuslog("failed to setup login1 event listener: %s", e)

        if DBUS_SCREENSAVER:
            try:
                session_bus = init_session_bus()
                self.session_bus = session_bus
                dbuslog("setup_dbus_signals() session bus=%s", session_bus)
            except Exception as e:
                dbuslog("setup_dbus_signals()", exc_info=True)
                dbuslog.error("Error setting up dbus signals:")
                dbuslog.error(" %s", e)
            else:
                #screensaver signals:
                try:
                    bus_name = "org.gnome.ScreenSaver"
                    iface_name = bus_name
                    self.screensaver_match = bus.add_signal_receiver(self.ActiveChanged, "ActiveChanged", iface_name, bus_name)
                    dbuslog("listening for 'ActiveChanged' signal on %s", iface_name)
                except Exception as e:
                    dbuslog.warn("Warning: failed to setup screensaver event listener: %s", e)

    def ActiveChanged(self, active):
        log("ActiveChanged(%s)", active)
        if active:
            self.client.suspend()
        else:
            self.client.resume()


    def setup_xprops(self):
        #wait for handshake to complete:
        if not is_Wayland():
            self.client.after_handshake(self.do_setup_xprops)

    def do_setup_xprops(self, *args):
        log("do_setup_xprops(%s)", args)
        ROOT_PROPS = ["RESOURCE_MANAGER", "_NET_WORKAREA", "_NET_CURRENT_DESKTOP"]
        try:
            self.init_x11_filter()
            from xpra.gtk_common.gtk_util import get_default_root_window
            from xpra.x11.xsettings import XSettingsWatcher
            from xpra.x11.xroot_props import XRootPropWatcher
            root = get_default_root_window()
            if self._xsettings_watcher is None:
                self._xsettings_watcher = XSettingsWatcher()
                self._xsettings_watcher.connect("xsettings-changed", self._handle_xsettings_changed)
                self._handle_xsettings_changed()
            if self._root_props_watcher is None:
                self._root_props_watcher = XRootPropWatcher(ROOT_PROPS, root)
                self._root_props_watcher.connect("root-prop-changed", self._handle_root_prop_changed)
                #ensure we get the initial value:
                self._root_props_watcher.do_notify("RESOURCE_MANAGER")
        except ImportError as e:
            log("do_setup_xprops%s", args, exc_info=True)
            log.error("Error: failed to load X11 properties/settings bindings:")
            log.error(" %s", e)
            log.error(" root window properties will not be propagated")


    def do_xi_devices_changed(self, event):
        log("do_xi_devices_changed(%s)", event)
        XI2 = X11XI2Bindings()
        devices = XI2.get_devices()
        if devices:
            self.client.send_input_devices("xi", devices)

    def setup_xi(self):
        self.client.timeout_add(100, self.do_setup_xi)

    def do_setup_xi(self):
        if self.client.server_input_devices not in ("xi", "uinput"):
            xinputlog("server does not support xi input devices")
            if self.client.server_input_devices:
                log(" server uses: %s", self.client.server_input_devices)
            return False
        try:
            from xpra.gtk_common.error import xsync, XError
            assert X11WindowBindings, "no X11 window bindings"
            assert X11XI2Bindings, "no XI2 window bindings"
            XI2 = X11XI2Bindings()
            #this may fail when windows are being destroyed,
            #ie: when another client disconnects because we are stealing the session
            try:
                with xsync:
                    XI2.select_xi2_events()
            except XError:
                self.xi_setup_failures += 1
                xinputlog("select_xi2_events() failed, attempt %i",
                          self.xi_setup_failures, exc_info=True)
                return self.xi_setup_failures<10    #try again
            with xsync:
                XI2.gdk_inject()
                self.init_x11_filter()
                if self.client.server_input_devices:
                    XI2.connect(0, "XI_HierarchyChanged", self.do_xi_devices_changed)
                    devices = XI2.get_devices()
                    if devices:
                        self.client.send_input_devices("xi", devices)
        except Exception as e:
            xinputlog("enable_xi2()", exc_info=True)
            xinputlog.error("Error: cannot enable XI2 events")
            xinputlog.error(" %s", e)
        else:
            #register our enhanced event handlers:
            self.add_xi2_method_overrides()
        return False

    def add_xi2_method_overrides(self):
        global WINDOW_ADD_HOOKS
        WINDOW_ADD_HOOKS = [XI2_Window]


    def _get_xsettings(self):
        try:
            return self._xsettings_watcher.get_settings()
        except Exception:
            log.error("failed to get XSETTINGS", exc_info=True)
        return None

    def _handle_xsettings_changed(self, *_args):
        settings = self._get_xsettings()
        log("xsettings_changed new value=%s", settings)
        if settings is not None:
            self.client.send("server-settings", {"xsettings-blob": settings})

    def get_resource_manager(self):
        try:
            from xpra.gtk_common.gtk_util import get_default_root_window
            from xpra.x11.gtk_x11.prop import prop_get
            root = get_default_root_window()
            value = prop_get(root, "RESOURCE_MANAGER", "latin1", ignore_errors=True)
            if value is not None:
                return value.encode("utf-8")
        except (ImportError, UnicodeEncodeError):
            log.error("failed to get RESOURCE_MANAGER", exc_info=True)
        return None

    def _handle_root_prop_changed(self, obj, prop):
        log("root_prop_changed(%s, %s)", obj, prop)
        if prop=="RESOURCE_MANAGER":
            rm = self.get_resource_manager()
            if rm is not None:
                self.client.send("server-settings", {"resource-manager" : rm})
        elif prop=="_NET_WORKAREA":
            self.client.screen_size_changed("from %s event" % self._root_props_watcher)
        elif prop=="_NET_CURRENT_DESKTOP":
            self.client.workspace_changed("from %s event" % self._root_props_watcher)
        elif prop in ("_NET_DESKTOP_NAMES", "_NET_NUMBER_OF_DESKTOPS"):
            self.client.desktops_changed("from %s event" % self._root_props_watcher)
        else:
            log.error("unknown property %s", prop)


def main():
    try:
        from xpra.x11.gtk_x11.gdk_display_source import init_gdk_display_source
        init_gdk_display_source()
    except ImportError:
        pass
    from xpra.platform.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    sys.exit(main())
