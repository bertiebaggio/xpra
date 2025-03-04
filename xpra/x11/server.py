# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2020 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import signal
import math
from collections import deque
from gi.repository import GObject, Gtk, Gdk, GdkX11

from xpra.version_util import XPRA_VERSION
from xpra.util import updict, rindex, envbool, envint, typedict, WORKSPACE_NAMES
from xpra.os_util import memoryview_to_bytes, strtobytes, bytestostr, monotonic_time
from xpra.common import CLOBBER_UPGRADE, MAX_WINDOW_SIZE
from xpra.server import server_features, EXITING_CODE
from xpra.gtk_common.gobject_util import one_arg_signal
from xpra.gtk_common.gtk_util import get_default_root_window, get_pixbuf_from_data
from xpra.x11.common import Unmanageable
from xpra.x11.gtk_x11.prop import raw_prop_set
from xpra.x11.gtk_x11.tray import get_tray_window, SystemTray
from xpra.x11.gtk_x11.selection import AlreadyOwned
from xpra.x11.gtk_x11.gdk_bindings import (
    add_event_receiver,
    get_children,
    )
from xpra.x11.bindings.window_bindings import X11WindowBindings #@UnresolvedImport
from xpra.x11.bindings.keyboard_bindings import X11KeyboardBindings #@UnresolvedImport
from xpra.x11.x11_server_base import X11ServerBase
from xpra.gtk_common.error import xsync, xswallow, xlog, XError
from xpra.log import Logger

log = Logger("server")
focuslog = Logger("server", "focus")
grablog = Logger("server", "grab")
windowlog = Logger("server", "window")
geomlog = Logger("server", "window", "geometry")
traylog = Logger("server", "tray")
workspacelog = Logger("x11", "workspace")
metadatalog = Logger("x11", "metadata")
framelog = Logger("x11", "frame")
eventlog = Logger("x11", "events")
mouselog = Logger("x11", "mouse")
screenlog = Logger("x11", "screen")

X11Window = X11WindowBindings()
X11Keyboard = X11KeyboardBindings()

REPARENT_ROOT = envbool("XPRA_REPARENT_ROOT", True)
CONFIGURE_DAMAGE_RATE = envint("XPRA_CONFIGURE_DAMAGE_RATE", 250)
SHARING_SYNC_SIZE = envbool("XPRA_SHARING_SYNC_SIZE", True)
CLAMP_WINDOW_TO_ROOT = envbool("XPRA_CLAMP_WINDOW_TO_ROOT", False)
ALWAYS_RAISE_WINDOW = envbool("XPRA_ALWAYS_RAISE_WINDOW", False)

WINDOW_SIGNALS = os.environ.get("XPRA_WINDOW_SIGNALS", "SIGINT,SIGTERM,SIGQUIT,SIGCONT,SIGUSR1,SIGUSR2").split(",")


class DesktopState:
    __slots__ = ("geom", "resize_counter", "shown")
    def __init__(self, geom, resize_counter=0, shown=False):
        self.geom = geom
        self.resize_counter = resize_counter
        self.shown = shown
    def __repr__(self):
        return "DesktopState(%s, %i, %s)" % (self.geom, self.resize_counter, self.shown)

class DesktopManager(Gtk.Widget):
    __slots__ = ("_models")
    def __init__(self):
        self._models = {}
        super().__init__()
        self.set_property("can-focus", True)
        self.realize()

    def __repr__(self):
        return "DesktopManager(%s)" % len(self._models)

    ## For communicating with the main WM:

    def add_window(self, model, x, y, w, h):
        self._models[model] = DesktopState([x, y, w, h])
        model.managed_connect("unmanaged", self._unmanaged)
        model.managed_connect("ownership-election", self._elect_me)
        model.ownership_election()

    def window_geometry(self, model):
        return self._models[model].geom

    def update_window_geometry(self, model, x, y, w, h):
        self._models[model].geom = [x, y, w, h]

    def get_resize_counter(self, window, inc=0):
        model = self._models[window]
        v = model.resize_counter+inc
        model.resize_counter = v
        return v

    def show_window(self, model):
        self._models[model].shown = True
        model.ownership_election()
        if model.get_property("iconic"):
            model.set_property("iconic", False)

    def is_shown(self, model):
        if model.is_OR() or model.is_tray():
            return True
        return self._models[model].shown

    def configure_window(self, win, x, y, w, h, resize_counter=0):
        log("DesktopManager.configure_window(%s, %s, %s, %s, %s, %s)", win, x, y, w, h, resize_counter)
        model = self._models[win]
        new_geom = [x, y, w, h]
        update_geometry = False
        if model.geom!=new_geom:
            if resize_counter>0 and resize_counter<model.resize_counter:
                log("resize ignored: counter %s vs %s", resize_counter, model.resize_counter)
            else:
                update_geometry = True
                model.geom = new_geom
        if not self.visible(win):
            model.shown = True
            win.map()
            #Note: this will fire a metadata change event, which will fire a message to the client(s),
            #which is wasteful when we only have one client and it is the one that configured the window,
            #but when we have multiple clients, this keeps things in sync
            if win.get_property("iconic"):
                win.set_property("iconic", False)
            if win.ownership_election():
                #window has been configured already
                update_geometry = False
        if update_geometry:
            win.maybe_recalculate_geometry_for(self)

    def hide_window(self, model):
        self._models[model].shown = False
        model.ownership_election()

    def visible(self, model):
        return self._models[model].shown

    ## For communicating with WindowModels:

    def _unmanaged(self, model, _wm_exiting):
        del self._models[model]

    def _elect_me(self, model):
        if self.visible(model):
            return (1, self)
        return (-1, self)

    def take_window(self, _model, window):
        #log.info("take_window(%s, %s)", model, window)
        if not self.get_realized():
            #with GTK3, the widget is never realized??
            return
        gdkwin = self.get_window()
        assert gdkwin, "DesktopManager does not have a gdk window"
        if REPARENT_ROOT:
            parent = gdkwin.get_screen().get_root_window()
        else:
            parent = gdkwin
        log("take_window: reparenting %s to %s", window, parent)
        window.reparent(parent, 0, 0)

    def window_size(self, model):
        w, h = self._models[model].geom[2:4]
        return w, h

    def window_position(self, model, w=None, h=None):
        x, y, w0, h0 = self._models[model].geom
        if (w is not None and abs(w0-w)>1) or (h is not None and abs(h0-h)>1):
            log("Uh-oh, our size doesn't fit window sizing constraints: "
                     "%sx%s vs %sx%s", w0, h0, w, h)
        return x, y

GObject.type_register(DesktopManager)


class XpraServer(GObject.GObject, X11ServerBase):
    __gsignals__ = {
        "xpra-child-map-event"  : one_arg_signal,
        "xpra-cursor-event"     : one_arg_signal,
        "server-event"          : one_arg_signal,
        }

    def __init__(self, clobber):
        self.clobber = clobber
        self.root_overlay = None
        self.repaint_root_overlay_timer = None
        self.configure_damage_timers = {}
        self._tray = None
        self._has_grab = 0
        self._has_focus = 0
        self._wm = None
        self.last_raised = None
        self.system_tray = False
        GObject.GObject.__init__(self)
        X11ServerBase.__init__(self)
        self.session_type = "seamless"
        self._xsettings_enabled = True

    def init(self, opts):
        self.wm_name = opts.wm_name
        self.sync_xvfb = int(opts.sync_xvfb)
        self.system_tray = opts.system_tray
        super().init(opts)
        self.fake_xinerama = opts.fake_xinerama
        def log_server_event(_, event):
            eventlog("server-event: %s", event)
        self.connect("server-event", log_server_event)

    def server_init(self):
        X11ServerBase.server_init(self)
        screenlog("server_init() clobber=%s, randr=%s, initial_resolution=%s",
                  self.clobber, self.randr, self.initial_resolution)
        if self.randr and (self.initial_resolution or not self.clobber):
            from xpra.x11.vfb_util import set_initial_resolution, DEFAULT_VFB_RESOLUTION
            with xlog:
                set_initial_resolution(self.initial_resolution or DEFAULT_VFB_RESOLUTION)

    def validate(self):
        if not X11Window.displayHasXComposite():
            log.error("Xpra 'start' subcommand runs as a compositing manager")
            log.error(" it cannot use a display which lacks the XComposite extension!")
            return False
        #check for an existing window manager:
        from xpra.x11.gtk_x11.wm_check import wm_check
        return wm_check(self.wm_name, self.clobber & CLOBBER_UPGRADE)

    def setup(self):
        X11ServerBase.setup(self)
        if self.system_tray:
            self.add_system_tray()

    def x11_init(self):
        X11ServerBase.x11_init(self)

        self._focus_history = deque(maxlen=100)
        # Do this before creating the Wm object, to avoid clobbering its
        # selecting SubstructureRedirect.
        root = get_default_root_window()
        root.set_events(root.get_events() | Gdk.EventMask.SUBSTRUCTURE_MASK)
        xid = root.get_xid()
        raw_prop_set(xid, "XPRA_SERVER", "latin1", strtobytes(XPRA_VERSION).decode())
        add_event_receiver(root, self)
        if self.sync_xvfb>0:
            try:
                with xsync:
                    self.root_overlay = X11Window.XCompositeGetOverlayWindow(xid)
                    if self.root_overlay:
                        raw_prop_set(self.root_overlay, "WM_TITLE", "latin1", "RootOverlay")
                        X11Window.AllowInputPassthrough(self.root_overlay)
            except Exception as e:
                log("XCompositeGetOverlayWindow(%#x)", xid, exc_info=True)
                log.error("Error setting up xvfb synchronization:")
                log.error(" %s", e)
                if self.root_overlay:
                    with xswallow:
                        X11Window.XCompositeReleaseOverlayWindow(self.root_overlay)
                    self.root_overlay = None

        ### Create the WM object
        with xsync:
            from xpra.x11.gtk_x11.wm import Wm
            try:
                self._wm = Wm(self.clobber, self.wm_name)
            except AlreadyOwned:
                log("Error: cannot create our window manager", exc_info=True)
                display = os.environ.get("DISPLAY", "")
                #make sure we don't kill the vfb since we don't own it:
                self._upgrading = EXITING_CODE
                from xpra.scripts.config import InitException  #pylint: disable=import-outside-toplevel
                raise InitException("another window manager is active on display '%s'" % display) from None
        if server_features.windows:
            self._wm.connect("new-window", self._new_window_signaled)
        self._wm.connect("quit", lambda _: self.clean_quit(True))
        self._wm.connect("show-desktop", self._show_desktop)

        #for handling resize synchronization between client and server (this is not xsync!):
        self.last_client_configure_event = 0
        self.snc_timer = 0

    def do_cleanup(self):
        if self._tray:
            self._tray.cleanup()
            self._tray = None
        self.cancel_repaint_root_overlay()
        if self.root_overlay:
            with xswallow:
                X11Window.XCompositeReleaseOverlayWindow(self.root_overlay)
            self.root_overlay = None
        self.cancel_all_configure_damage()
        if self._wm:
            self._wm.cleanup()
            self._wm = None
        if self._has_grab:
            #normally we set this value when we receive the NotifyUngrab
            #but at this point in the cleanup, we probably won't, so force set it:
            self._has_grab = 0
            self.X11_ungrab()
        X11ServerBase.do_cleanup(self)

    def clean_x11_properties(self):
        super().clean_x11_properties()
        self.do_clean_x11_properties("XPRA_SERVER")


    def last_client_exited(self):
        #last client is gone:
        X11ServerBase.last_client_exited(self)
        if self._has_grab:
            self._has_grab = 0
            self.X11_ungrab()


    def update_size_constraints(self, minw, minh, maxw, maxh):
        wm = self._wm
        if wm:
            wm.set_size_constraints(minw, minh, maxw, maxh)
        elif server_features.windows:
            #update the default so Wm will use it
            #when we do intantiate it:
            from xpra.x11.gtk_x11 import wm
            wm.DEFAULT_SIZE_CONSTRAINTS = (0, 0, MAX_WINDOW_SIZE, MAX_WINDOW_SIZE)


    def init_packet_handlers(self):
        X11ServerBase.init_packet_handlers(self)
        self.add_packet_handler("window-signal", self._process_window_signal)


    def get_server_mode(self):
        return "X11 seamless"


    def server_event(self, *args):
        super().server_event(*args)
        self.emit("server-event", args)


    def make_hello(self, source):
        capabilities = super().make_hello(source)
        if source.wants_features:
            capabilities["pointer.grabs"] = True
            updict(capabilities, "window", {
                "decorations"            : True,        #v4 clients assume this is enabled
                "frame-extents"          : True,
                "configure.delta"        : True,
                "signals"                : WINDOW_SIGNALS,
                "dragndrop"              : True,
                "states"                 : [
                    "iconified", "focused", "fullscreen",
                    "above", "below",
                    "sticky", "iconified", "maximized",
                    ],
                })
        return capabilities


    ##########################################################################
    # info:
    #
    def do_get_info(self, proto, server_sources):
        info = super().do_get_info(proto, server_sources)
        info.setdefault("state", {}).update({
                                             "focused"  : self._has_focus,
                                             "grabbed"  : self._has_grab,
                                             })
        return info

    def get_ui_info(self, proto, wids=None, *args):
        info = super().get_ui_info(proto, wids, *args)
        #_NET_WM_NAME:
        wm = self._wm
        if wm:
            info.setdefault("state", {})["window-manager-name"] = wm.get_net_wm_name()
        return info

    def get_window_info(self, window):
        info = super().get_window_info(window)
        info.update({
                     "focused"  : self._has_focus and self._window_to_id.get(window, -1)==self._has_focus,
                     "grabbed"  : self._has_grab and self._window_to_id.get(window, -1)==self._has_grab,
                     "shown"    : self._desktop_manager.is_shown(window),
                     })
        try:
            info["client-geometry"] = self._desktop_manager.window_geometry(window)
        except:
            pass        #OR or tray window
        return info


    ##########################################################################
    # Manage the virtual screen:
    #

    def set_screen_geometry_attributes(self, w, h):
        #only run the default code if there are no clients,
        #when we have clients, this should have been done already
        #in the code that synchonizes the screen resolution
        if not self._server_sources:
            super().set_screen_geometry_attributes(w, h)

    def set_desktops(self, names):
        wm = self._wm
        if wm:
            wm.set_desktop_list(names)

    def set_workarea(self, workarea):
        wm = self._wm
        if wm:
            wm.set_workarea(workarea.x, workarea.y, workarea.width, workarea.height)

    def set_desktop_geometry(self, width, height):
        wm = self._wm
        if wm:
            wm.set_desktop_geometry(width, height)

    def set_dpi(self, xdpi, ydpi):
        wm = self._wm
        if wm:
            wm.set_dpi(xdpi, ydpi)


    def add_system_tray(self):
        # Tray handler:
        try:
            with xsync:
                self._tray = SystemTray()
        except Exception as e:
            log.error("cannot setup tray forwarding: %s", e, exc_info=True)


    ##########################################################################
    # Manage windows:
    #

    def is_shown(self, window):
        return self._desktop_manager.is_shown(window)

    def load_existing_windows(self):
        if not self._wm:
            return

        ### Create our window managing data structures:
        self._desktop_manager = DesktopManager()
        add_to = self._wm.get_property("toplevel")
        #may be missing with GTK3..
        if add_to:
            add_to.add(self._desktop_manager)
        self._desktop_manager.show_all()

        ### Load in existing windows:
        for window in self._wm.get_property("windows"):
            self._add_new_window(window)

        root = get_default_root_window()
        try:
            with xsync:
                children = get_children(root)
        except XError:
            log("load_existing_windows()", exc_info=True)
            log("trying again")
            with xsync:
                children = get_children(root)
        for window in children:
            xid = window.get_xid()
            can_add = False
            with xlog:
                can_add = X11Window.is_override_redirect(xid) and X11Window.is_mapped(xid)
            if can_add:
                self._add_new_or_window(window)

    def _lookup_window(self, wid):
        assert isinstance(wid, int), "window id value '%s' is a %s and not a number" % (wid, type(wid))
        return self._id_to_window.get(wid)

    def get_transient_for(self, window):
        transient_for = window.get_property("transient-for")
        log("get_transient_for window=%s, transient_for=%s", window, transient_for)
        if transient_for is None:
            return 0
        xid = transient_for.get_xid()
        log("transient_for.xid=%#x", xid)
        for w,wid in self._window_to_id.items():
            if w.get_property("xid")==xid:
                log("found match, window id=%s", wid)
                return wid
        root = get_default_root_window()
        if root.get_xid()==xid:
            log("transient-for using root")
            return -1       #-1 is the backwards compatible marker for root...
        log("not found transient_for=%s, xid=%#x", transient_for, xid)
        return 0


    def parse_hello_ui_window_settings(self, ss, _caps):
        #FIXME: with multiple users, don't set any frame size?
        frame = None
        if ss in self.window_sources():
            window_frame_sizes = ss.window_frame_sizes
            framelog("parse_hello_ui_window_settings: client window_frame_sizes=%s", window_frame_sizes)
            if window_frame_sizes:
                frame = window_frame_sizes.inttupleget("frame", (0, 0, 0, 0), 4, 4)
        if self._wm:
            self._wm.set_default_frame_extents(frame)


    def send_initial_windows(self, ss, sharing=False):
        # We send the new-window packets sorted by id because this sorts them
        # from oldest to newest -- and preserving window creation order means
        # that the earliest override-redirect windows will be on the bottom,
        # which is usually how things work.  (I don't know that anyone cares
        # about this kind of correctness at all, but hey, doesn't hurt.)
        windowlog("send_initial_windows(%s, %s) will send: %s", ss, sharing, self._id_to_window)
        for wid in sorted(self._id_to_window.keys()):
            window = self._id_to_window[wid]
            if not window.is_managed():
                #we keep references to windows that aren't meant to be displayed..
                continue
            #most of the code here is duplicated from the send functions
            #so we can send just to the new client and request damage
            #just for the new client too:
            if window.is_tray():
                #code more or less duplicated from _send_new_tray_window_packet:
                w, h = window.get_dimensions()
                if ss.system_tray:
                    ss.new_tray(wid, window, w, h)
                    ss.damage(wid, window, 0, 0, w, h)
                elif not sharing:
                    #park it outside the visible area
                    window.move_resize(-200, -200, w, h)
            elif window.is_OR():
                #code more or less duplicated from _send_new_or_window_packet:
                x, y, w, h = window.get_property("geometry")
                wprops = self.client_properties.get(wid, {}).get(ss.uuid)
                ss.new_window("new-override-redirect", wid, window, x, y, w, h, wprops)
                ss.damage(wid, window, 0, 0, w, h)
            else:
                #code more or less duplicated from send_new_window_packet:
                if not sharing:
                    self._desktop_manager.hide_window(window)
                x, y, w, h = self._desktop_manager.window_geometry(window)
                wprops = self.client_properties.get(wid, {}).get(ss.uuid)
                ss.new_window("new-window", wid, window, x, y, w, h, wprops)


    def _new_window_signaled(self, _wm, window):
        self.last_raised = None
        self._add_new_window(window)

    def do_xpra_child_map_event(self, event):
        windowlog("do_xpra_child_map_event(%s)", event)
        if event.override_redirect:
            self._add_new_or_window(event.window)

    def _add_new_window_common(self, window):
        windowlog("adding window %s", window)
        wid = super()._add_new_window_common(window)
        window.managed_connect("client-contents-changed", self._contents_changed)
        window.managed_connect("unmanaged", self._lost_window)
        window.managed_connect("grab", self._window_grab)
        window.managed_connect("ungrab", self._window_ungrab)
        window.managed_connect("bell", self._bell_signaled)
        window.managed_connect("motion", self._motion_signaled)
        window.managed_connect("x11-property-changed", self._x11_property_changed)
        if not window.is_tray():
            window.managed_connect("restack", self._restack_window)
            window.managed_connect("initiate-moveresize", self._initiate_moveresize)
        window_prop_set = getattr(window, "prop_set", None)
        if window_prop_set:
            try:
                window_prop_set("_XPRA_WID", "u32", wid)
            except:
                pass    #this can fail if the window disappears
                #but we don't really care, it will get cleaned up soon enough
        return wid


    def _x11_property_changed(self, window, event):
        #name, dtype, dformat, value = event
        metadata = {"x11-property" : event}
        wid = self._window_to_id[window]
        for ss in self.window_sources():
            ms = getattr(ss, "metadata_supported", ())
            if "x11-property" in ms:
                ss.send("window-metadata", wid, metadata)


    def _add_new_window(self, window):
        self._add_new_window_common(window)
        x, y, w, h = window.get_property("geometry")
        log("Discovered new ordinary window: %s (geometry=%s)", window, (x, y, w, h))
        self._desktop_manager.add_window(window, x, y, w, h)
        window.managed_connect("notify::geometry", self._window_resized_signaled)
        self._send_new_window_packet(window)


    def _window_resized_signaled(self, window, *args):
        x, y, nw, nh = window.get_property("geometry")[:4]
        geom = self._desktop_manager.window_geometry(window)
        geomlog("XpraServer._window_resized_signaled(%s,%s) geometry=%s, desktop manager geometry=%s",
                window, args, (x, y, nw, nh), geom)
        if geom==[x, y, nw, nh]:
            geomlog("XpraServer._window_resized_signaled: unchanged")
            #unchanged
            return
        self._desktop_manager.update_window_geometry(window, x, y, nw, nh)
        lcce = self.last_client_configure_event
        if not self._desktop_manager.is_shown(window):
            self.size_notify_clients(window)
            return
        if self.snc_timer>0:
            self.source_remove(self.snc_timer)
        #TODO: find a better way to choose the timer delay:
        #for now, we wait at least 100ms, up to 250ms if the client has just sent us a resize:
        #(lcce should always be in the past, so min(..) should be redundant here)
        delay = max(100, min(250, 250 + 1000 * (lcce-monotonic_time())))
        self.snc_timer = self.timeout_add(int(delay), self.size_notify_clients, window, lcce)

    def size_notify_clients(self, window, lcce=-1):
        geomlog("size_notify_clients(%s, %s) last_client_configure_event=%s",
                window, lcce, self.last_client_configure_event)
        self.snc_timer = 0
        wid = self._window_to_id.get(window)
        if not wid:
            geomlog("size_notify_clients: window is gone")
            return
        if lcce>0 and lcce!=self.last_client_configure_event:
            geomlog("size_notify_clients: we have received a new client resize since")
            return
        x, y, nw, nh = self._desktop_manager.window_geometry(window)
        resize_counter = self._desktop_manager.get_resize_counter(window, 1)
        for ss in self.window_sources():
            ss.move_resize_window(wid, window, x, y, nw, nh, resize_counter)
            #refresh to ensure the client gets the new window contents:
            #TODO: to save bandwidth, we should compare the dimensions and skip the refresh
            #if the window is smaller than before, or at least only send the new edges rather than the whole window
            ss.damage(wid, window, 0, 0, nw, nh)

    def _add_new_or_window(self, raw_window):
        xid = raw_window.get_xid()
        if self.root_overlay and self.root_overlay==xid:
            windowlog("ignoring root overlay window %#x", self.root_overlay)
            return
        if raw_window.get_window_type()==Gdk.WindowType.TEMP:
            #ignoring one of gtk's temporary windows
            #all the windows we manage should be Gdk.WINDOW_FOREIGN
            windowlog("ignoring TEMP window %#x", xid)
            return
        WINDOW_MODEL_KEY = "_xpra_window_model_"
        wid = getattr(raw_window, WINDOW_MODEL_KEY, None)
        window = self._id_to_window.get(wid)
        if window:
            if window.is_managed():
                windowlog("found existing window model %s for %#x, will refresh it", type(window), xid)
                ww, wh = window.get_dimensions()
                self.refresh_window_area(window, 0, 0, ww, wh, options={"min_delay" : 50})
                return
            windowlog("found existing model %s (but no longer managed!) for %#x", type(window), xid)
            #we could try to re-use the existing model and window ID,
            #but for now it is just easier to create a new one:
            self._lost_window(window)
        try:
            with xsync:
                geom = X11Window.getGeometry(xid)
                windowlog("Discovered new override-redirect window: %#x X11 geometry=%s", xid, geom)
        except Exception as e:
            windowlog("Window error (vanished already?): %s", e)
            return
        try:
            tray_window = get_tray_window(raw_window)
            if tray_window is not None:
                assert self._tray
                from xpra.x11.models.systray import SystemTrayWindowModel
                window = SystemTrayWindowModel(raw_window)
                wid = self._add_new_window_common(window)
                setattr(raw_window, WINDOW_MODEL_KEY, wid)
                window.call_setup()
                self._send_new_tray_window_packet(wid, window)
            else:
                from xpra.x11.models.or_window import OverrideRedirectWindowModel
                window = OverrideRedirectWindowModel(raw_window)
                wid = self._add_new_window_common(window)
                setattr(raw_window, WINDOW_MODEL_KEY, wid)
                window.call_setup()
                window.managed_connect("notify::geometry", self._or_window_geometry_changed)
                self._send_new_or_window_packet(window)
        except Unmanageable as e:
            if window:
                windowlog("window %s is not manageable: %s", window, e)
                #if window is set, we failed after instantiating it,
                #so we need to fail it manually:
                window.setup_failed(e)
                if window in self._window_to_id:
                    self._lost_window(window, False)
            else:
                windowlog.warn("cannot add window %#x: %s", xid, e)
            #from now on, we return to the gtk main loop,
            #so we *should* get a signal when the window goes away

    def _or_window_geometry_changed(self, window, _pspec=None):
        geom = window.get_property("geometry")
        x, y, w, h = geom
        if w>=32768 or h>=32768:
            geomlog.error("not sending new invalid window dimensions: %ix%i !", w, h)
            return
        geomlog("or_window_geometry_changed: %s (window=%s)", geom, window)
        wid = self._window_to_id[window]
        for ss in self.window_sources():
            ss.or_window_geometry(wid, window, x, y, w, h)


    def add_control_commands(self):
        super().add_control_commands()
        from xpra.server.control_command import ArgsControlCommand
        cmd = ArgsControlCommand("show-all-windows", "make all the windows visible", validation=[])
        def control_cb():
            self.show_all_windows()
            return "%i windows shown" % len(self._id_to_window)
        cmd.do_run = control_cb
        self.control_commands[cmd.name] = cmd

    def show_all_windows(self):
        for w in self._id_to_window.values():
            self._desktop_manager.show_window(w)


    def _show_desktop(self, wm, show):
        log("show_desktop(%s, %s)", wm, show)
        for ss in self.window_sources():
            ss.show_desktop(show)


    def _focus(self, server_source, wid, modifiers):
        focuslog("focus wid=%s has_focus=%s", wid, self._has_focus)
        if self.last_raised!=wid:
            self.last_raised = None
        if self._has_focus==wid:
            #nothing to do!
            return
        self._focus_history.append(wid)
        hfid = self._has_focus
        had_focus = self._id_to_window.get(hfid)
        def reset_focus():
            toplevel = None
            if self._wm:
                toplevel = self._wm.get_property("toplevel")
            focuslog("reset_focus() %s / %s had focus (toplevel=%s)", hfid, had_focus, toplevel)
            #this will call clear_keys_pressed() if the server is an InputServer:
            self.reset_focus()
            # FIXME: kind of a hack:
            self._has_focus = 0
            #toplevel may be None during cleanup!
            if toplevel:
                toplevel.reset_x_focus()

        if wid == 0:
            #wid==0 means root window
            return reset_focus()
        window = self._id_to_window.get(wid)
        if not window:
            #not found! (go back to root)
            return reset_focus()
        if window.is_OR():
            focuslog.warn("Warning: cannot focus OR window: %s", window)
            return
        if not window.is_managed():
            focuslog.warn("Warning: window %s is no longer managed!", window)
            return
        focuslog("focus: giving focus to %s", window)
        with xswallow:
            self.last_raised = wid
            window.raise_window()
            window.give_client_focus()
        if server_source and modifiers is not None:
            make_keymask_match = getattr(server_source, "make_keymask_match", None)
            if make_keymask_match:
                focuslog("focus: will set modified mask to %s using %s", modifiers, make_keymask_match)
                make_keymask_match(modifiers)
        self._has_focus = wid

    def get_focus(self):
        return self._has_focus


    def _send_new_window_packet(self, window):
        geometry = self._desktop_manager.window_geometry(window)
        self._do_send_new_window_packet("new-window", window, geometry)

    def _send_new_or_window_packet(self, window):
        geometry = window.get_property("geometry")
        self._do_send_new_window_packet("new-override-redirect", window, geometry)
        self.refresh_window(window)

    def _send_new_tray_window_packet(self, wid, window):
        ww, wh = window.get_dimensions()
        for ss in self.window_sources():
            ss.new_tray(wid, window, ww, wh)
        self.refresh_window(window)


    def _lost_window(self, window, wm_exiting=False):
        wid = self._remove_window(window)
        self.cancel_configure_damage(wid)
        if not wm_exiting:
            self.repaint_root_overlay()

    def _contents_changed(self, window, event):
        if window.is_OR() or window.is_tray() or self._desktop_manager.visible(window):
            self.refresh_window_area(window, event.x, event.y, event.width, event.height, options={"damage" : True})


    def _window_grab(self, window, event):
        grab_id = self._window_to_id.get(window, -1)
        grablog("window_grab(%s, %s) has_grab=%s, has focus=%s, grab window=%s",
                window, event, self._has_grab, self._has_focus, grab_id)
        if grab_id<0 or self._has_grab==grab_id:
            return
        self._has_grab = grab_id
        for ss in self.window_sources():
            ss.pointer_grab(self._has_grab)

    def _window_ungrab(self, window, event):
        grab_id = self._window_to_id.get(window, -1)
        grablog("window_ungrab(%s, %s) has_grab=%s, has focus=%s, grab window=%s",
                window, event, self._has_grab, self._has_focus, grab_id)
        if not self._has_grab:
            return
        self._has_grab = 0
        for ss in self.window_sources():
            ss.pointer_ungrab(grab_id)


    def _initiate_moveresize(self, window, event):
        log("initiate_moveresize(%s, %s)", window, event)
        assert len(event.data)==5
        #x_root, y_root, direction, button, source_indication = event.data
        wid = self._window_to_id[window]
        #find clients that handle windows:
        wsources = self.window_sources()
        if not wsources:
            return
        #prefer the "UI driver" if we find it:
        driversources = [ss for ss in wsources if self.ui_driver==ss.uuid]
        if driversources:
            driversources[0].initiate_moveresize(wid, window, *event.data)
            return
        #otherwise, fallback to the first one:
        wsources[0].initiate_moveresize(wid, window, *event.data)


    def _restack_window(self, window, detail, sibling):
        wid = self._window_to_id[window]
        focuslog("restack window(%s) wid=%s, current focus=%s", (window, detail, sibling), wid, self._has_focus)
        if self.last_raised!=wid:
            #ensure we will raise the window for the next pointer event
            self.last_raised = None
        if detail==0 and self._has_focus==wid:  #Above=0
            return
        for ss in self.window_sources():
            ss.restack_window(wid, window, detail, sibling)


    def _set_window_state(self, proto, wid, window, new_window_state):
        assert proto in self._server_sources
        if not new_window_state:
            return []
        nws = typedict(new_window_state)
        metadatalog("set_window_state%s", (wid, window, new_window_state))
        changes = []
        if "frame" in new_window_state:
            #the size of the window frame may have changed
            frame = nws.inttupleget("frame", (0, 0, 0, 0))
            window.set_property("frame", frame)
        #boolean: but not a wm_state and renamed in the model... (iconic vs inconified!)
        iconified = nws.boolget("iconified", None)
        if iconified is not None:
            if window.is_OR():
                log("ignoring iconified=%s on OR window %s", iconified, window)
            else:
                if window.get_property("iconic")!=bool(iconified):
                    window.set_property("iconic", iconified)
                    changes.append("iconified")
        #handle wm_state virtual booleans:
        for k in (
            "maximized", "above",
            "below", "fullscreen",
            "sticky", "shaded",
            "skip-pager", "skip-taskbar", "focused",
            ):
            #metadatalog.info("window.get_property=%s", window.get_property)
            new_state = nws.boolget(k, None)
            if new_state is None:
                continue
            cur_state = bool(window.get_property(k))
            #metadatalog.info("set window state for '%s': current state=%s, new state=%s", k, cur_state, new_state)
            if cur_state!=new_state:
                window.update_wm_state(k, new_state)
                changes.append(k)
        metadatalog("set_window_state: changes=%s", changes)
        return changes


    def get_window_position(self, window):
        #used to adjust the pointer position with multiple clients
        if window is None or window.is_OR() or window.is_tray():
            return None
        return self._desktop_manager.window_position(window)


    def _process_map_window(self, proto, packet):
        wid, x, y, w, h = packet[1:6]
        window = self._lookup_window(wid)
        if not window:
            windowlog("cannot map window %i: not found, already removed?", wid)
            return
        if window.is_OR():
            windowlog.warn("Warning: received map event on OR window %s", wid)
            return
        ss = self.get_server_source(proto)
        if ss is None:
            return
        geomlog("client %s mapped window %i - %s, at: %s", ss, wid, window, (x, y, w, h))
        self._window_mapped_at(proto, wid, window, (x, y, w, h))
        if len(packet)>=7:
            self._set_client_properties(proto, wid, window, packet[6])
        if not self.ui_driver:
            self.set_ui_driver(ss)
        if self.ui_driver==ss.uuid or not self._desktop_manager.is_shown(window):
            if len(packet)>=8:
                self._set_window_state(proto, wid, window, packet[7])
            ax, ay, aw, ah = self._clamp_window(proto, wid, window, x, y, w, h)
            self._desktop_manager.configure_window(window, ax, ay, aw, ah)
            self._desktop_manager.show_window(window)
        self.refresh_window_area(window, 0, 0, w, h)


    def _process_unmap_window(self, proto, packet):
        wid = packet[1]
        window = self._lookup_window(wid)
        if not window:
            log("cannot unmap window %i: not found, already removed?", wid)
            return
        assert not window.is_OR()
        ss = self.get_server_source(proto)
        if ss is None:
            return
        self._window_mapped_at(proto, wid, window)
        #if self.ui_driver!=ss.uuid:
        #    return
        if len(packet)>=4:
            #optional window_state added in 0.15 to update flags
            #during iconification events:
            self._set_window_state(proto, wid, window, packet[3])
        if self._desktop_manager.is_shown(window):
            geomlog("client %s unmapped window %s - %s", ss, wid, window)
            for ss in self.window_sources():
                ss.unmap_window(wid, window)
            window.unmap()
            iconified = len(packet)>=3 and bool(packet[2])
            if iconified and not window.get_property("iconic"):
                window.set_property("iconic", True)
            self._desktop_manager.hide_window(window)
            self.repaint_root_overlay()

    def _clamp_window(self, proto, wid, window, x, y, w, h, resize_counter=0):
        if not CLAMP_WINDOW_TO_ROOT:
            return x, y, w, h
        rw, rh = self.get_root_window_size()
        #clamp to root window size
        if x>=rw or y>=rh:
            log("clamping window position %ix%i to root window size %ix%i", x, y, rw, rh)
            x = max(0, min(x, rw-w))
            y = max(0, min(y, rh-h))
            #tell this client to honour the new location
            ss = self.get_server_source(proto)
            if ss:
                resize_counter = max(resize_counter, self._desktop_manager.get_resize_counter(window, 1))
                ss.move_resize_window(wid, window, x, y, w, h, resize_counter)
        return x, y, w, h

    def _process_configure_window(self, proto, packet):
        wid, x, y, w, h = packet[1:6]
        window = self._lookup_window(wid)
        if not window:
            geomlog("cannot configure window %i: not found, already removed?", wid)
            return
        ss = self.get_server_source(proto)
        if not ss:
            return
        #some "configure-window" packets are only meant for metadata updates:
        skip_geometry = len(packet)>=10 and packet[9]
        if not skip_geometry:
            self._window_mapped_at(proto, wid, window, (x, y, w, h))
        if len(packet)>=7:
            cprops = packet[6]
            if cprops:
                metadatalog("window client properties updates: %s", cprops)
                self._set_client_properties(proto, wid, window, cprops)
        if not self.ui_driver:
            self.set_ui_driver(ss)
        is_ui_driver = self.ui_driver==ss.uuid
        shown = self._desktop_manager.is_shown(window)
        if window.is_OR() or window.is_tray() or skip_geometry or self.readonly:
            size_changed = False
        else:
            oww, owh = self._desktop_manager.window_size(window)
            size_changed = oww!=w or owh!=h
        if is_ui_driver or size_changed or not shown:
            damage = False
            if is_ui_driver and len(packet)>=13 and server_features.input_devices and not self.readonly:
                pwid = packet[10]
                pointer = packet[11]
                modifiers = packet[12]
                if pwid==wid and window.is_OR():
                    #some clients may send the OR window wid
                    #this causes focus issues (see #1999)
                    pwid = -1
                mouselog("configure pointer data: %s", (pwid, pointer, modifiers))
                if self._process_mouse_common(proto, pwid, pointer):
                    #only update modifiers if the window is in focus:
                    if self._has_focus==wid:
                        self._update_modifiers(proto, wid, modifiers)
            if window.is_tray():
                assert self._tray
                if not skip_geometry and not self.readonly:
                    traylog("tray %s configured to: %s", window, (x, y, w, h))
                    self._tray.move_resize(window, x, y, w, h)
                    damage = True
            else:
                assert skip_geometry or not window.is_OR(), "received a configure packet with geometry for OR window %s from %s: %s" % (window, proto, packet)
                self.last_client_configure_event = monotonic_time()
                if is_ui_driver and len(packet)>=9:
                    changes = self._set_window_state(proto, wid, window, packet[8])
                    if changes:
                        damage = True
                if not skip_geometry:
                    owx, owy, oww, owh = self._desktop_manager.window_geometry(window)
                    resize_counter = 0
                    if len(packet)>=8:
                        resize_counter = packet[7]
                    geomlog("_process_configure_window(%s) old window geometry: %s", packet[1:], (owx, owy, oww, owh))
                    ax, ay, aw, ah = self._clamp_window(proto, wid, window, x, y, w, h, resize_counter)
                    self._desktop_manager.configure_window(window, ax, ay, aw, ah, resize_counter)
                    resized = oww!=aw or owh!=ah
                    if resized and SHARING_SYNC_SIZE:
                        #try to ensure this won't trigger a resizing loop:
                        counter = max(0, resize_counter-1)
                        for s in self.window_sources():
                            if s!=ss:
                                s.resize_window(wid, window, aw, ah, resize_counter=counter)
                    damage |= owx!=ax or owy!=ay or resized
            if not shown and not skip_geometry:
                self._desktop_manager.show_window(window)
                damage = True
            self.repaint_root_overlay()
        else:
            damage = True
        if damage:
            self.schedule_configure_damage(wid)

    def schedule_configure_damage(self, wid):
        #rate-limit the damage events
        timer = self.configure_damage_timers.get(wid)
        if timer:
            return  #we already have one pending
        def damage():
            self.configure_damage_timers.pop(wid, None)
            window = self._lookup_window(wid)
            if window and window.is_managed():
                self.refresh_window(window)
        self.configure_damage_timers[wid] = self.timeout_add(CONFIGURE_DAMAGE_RATE, damage)

    def cancel_configure_damage(self, wid):
        timer = self.configure_damage_timers.pop(wid, None)
        if timer:
            self.source_remove(timer)

    def cancel_all_configure_damage(self):
        timers = tuple(self.configure_damage_timers.values())
        self.configure_damage_timers = {}
        for timer in timers:
            self.source_remove(timer)


    def _set_client_properties(self, proto, wid, window, new_client_properties):
        """
        Override so we can update the workspace on the window directly,
        instead of storing it as a client property
        """
        workspace = typedict(new_client_properties).intget("workspace", None)
        def wn(w):
            return WORKSPACE_NAMES.get(w, w)
        workspacelog("workspace from client properties %s: %s", new_client_properties, wn(workspace))
        if workspace is not None:
            window.move_to_workspace(workspace)
            #we have handled it on the window directly, so remove it from client properties
            new_client_properties.pop("workspace", None)
        #handle the rest as normal:
        super()._set_client_properties(proto, wid, window, new_client_properties)


    """ override so we can raise the window under the cursor
        (gtk raise does not change window stacking, just focus) """
    def _move_pointer(self, wid, pos, *args):
        if wid>0 and (self.last_raised!=wid or ALWAYS_RAISE_WINDOW):
            window = self._lookup_window(wid)
            if not window:
                mouselog("_move_pointer(%s, %s) invalid window id", wid, pos)
            else:
                self.last_raised = wid
                mouselog("raising %s", window)
                with xswallow:
                    window.raise_window()
        super()._move_pointer(wid, pos, *args)


    def _process_close_window(self, proto, packet):
        assert proto in self._server_sources
        wid = packet[1]
        window = self._lookup_window(wid)
        windowlog("client closed window %s - %s", wid, window)
        if window:
            window.request_close()
        else:
            windowlog("cannot close window %s: it is already gone!", wid)
        self.repaint_root_overlay()


    def _process_window_signal(self, proto, packet):
        assert proto in self._server_sources
        wid = packet[1]
        sig = bytestostr(packet[2])
        if sig not in WINDOW_SIGNALS:
            log.warn("Warning: window signal '%s' not handled", sig)
            return
        w = self._lookup_window(wid)
        if not w:
            log.warn("Warning: window %s not found", wid)
            return
        pid = w.get_property("pid")
        log("window-signal %s for wid=%i, pid=%s", sig, wid, pid)
        if not pid:
            log.warn("Warning: no pid found for window %s, cannot send %s", wid, sig)
            return
        try:
            sigval = getattr(signal, sig)       #ie: signal.SIGINT
            os.kill(pid, sigval)
            log.info("sent signal %s to pid %i for window %i", sig, pid, wid)
        except Exception as e:
            log("_process_window_signal(%s, %s)", proto, packet, exc_info=True)
            log.error("Error: failed to send signal %s to pid %i for window %i", sig, pid, wid)
            log.error(" %s", e)


    def refresh_window_area(self, window, x, y, width, height, options=None):
        super().refresh_window_area(window, x, y, width, height, options)
        if self.root_overlay:
            image = window.get_image(x, y, width, height)
            if image:
                self.update_root_overlay(window, x, y, image)


    ##########################################################################
    # paint the root overlay
    # so the server-side root window gets updated if this feature is enabled
    #

    def update_root_overlay(self, window, x, y, image):
        display = Gdk.Display.get_default()
        overlaywin = GdkX11.X11Window.foreign_new_for_display(display, self.root_overlay)
        wx, wy = window.get_property("geometry")[:2]
        #FIXME: we should paint the root overlay directly
        # either using XCopyArea or XShmPutImage,
        # using GTK and having to unpremultiply then convert to RGB is just too slooooow
        width = image.get_width()
        height = image.get_height()
        rowstride = image.get_rowstride()
        img_data = image.get_pixels()
        rgb_format = image.get_pixel_format()
        from xpra.codecs.argb.argb import ( #@UnresolvedImport
            unpremultiply_argb, bgra_to_rgba, bgra_to_rgbx, r210_to_rgbx, bgr565_to_rgbx  #@UnresolvedImport
            )
        from cairo import OPERATOR_OVER, OPERATOR_SOURCE  #pylint: disable=no-name-in-module
        log("update_root_overlay%s rgb_format=%s, img_data=%i (%s)",
                 (window, x, y, image), rgb_format, len(img_data), type(img_data))
        operator = OPERATOR_SOURCE
        if rgb_format=="BGRA":
            img_data = unpremultiply_argb(img_data)
            img_data = bgra_to_rgba(img_data)
            operator = OPERATOR_OVER
        elif rgb_format=="BGRX":
            img_data = bgra_to_rgbx(img_data)
        elif rgb_format=="r210":
            #lossy...
            img_data = r210_to_rgbx(img_data, width, height, rowstride, width*4)
            rowstride = width*4
        elif rgb_format=="BGR565":
            img_data = bgr565_to_rgbx(img_data)
            rowstride *= 2
        else:
            raise Exception("xync-xvfb root overlay paint code does not handle %s pixel format" % image.get_pixel_format())
        img_data = memoryview_to_bytes(img_data)
        log("update_root_overlay%s painting rectangle %s", (window, x, y, image), (wx+x, wy+y, width, height))
        pixbuf = get_pixbuf_from_data(img_data, True, width, height, rowstride)
        cr = overlaywin.cairo_create()
        cr.new_path()
        cr.rectangle(wx+x, wy+y, width, height)
        cr.clip()
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, wx+x, wy+y)
        cr.set_operator(operator)
        cr.paint()
        image.free()

    def repaint_root_overlay(self):
        if not self.root_overlay:
            return
        log("repaint_root_overlay() root_overlay=%s, due=%s, sync-xvfb=%ims",
            self.root_overlay, self.repaint_root_overlay_timer, self.sync_xvfb)
        if self.repaint_root_overlay_timer:
            return
        self.repaint_root_overlay_timer = self.timeout_add(self.sync_xvfb, self.do_repaint_root_overlay)

    def cancel_repaint_root_overlay(self):
        rrot = self.repaint_root_overlay_timer
        if rrot:
            self.repaint_root_overlay_timer = None
            self.source_remove(rrot)

    def do_repaint_root_overlay(self):
        self.repaint_root_overlay_timer = None
        root_width, root_height = self.get_root_window_size()
        display = Gdk.Display.get_default()
        overlaywin = GdkX11.X11Window.foreign_new_for_display(display, self.root_overlay)
        log("overlaywin: %s", overlaywin.get_geometry())
        cr = overlaywin.cairo_create()
        def fill_grey_rect(shade, x, y, w, h):
            log("paint_grey_rect%s", (shade, x, y, w, h))
            cr.new_path()
            cr.set_source_rgb(*shade)
            cr.rectangle(x, y, w, h)
            cr.fill()
        def paint_grey_rect(shade, x, y, w, h):
            log("paint_grey_rect%s", (shade, x, y, w, h))
            cr.new_path()
            cr.set_line_width(2)
            cr.set_source_rgb(*shade)
            cr.rectangle(x, y, w, h)
            cr.stroke()
        #clear to black
        fill_grey_rect((0, 0, 0), 0, 0, root_width, root_height)
        sources = [source for source in self._server_sources.values() if source.ui_client]
        ss = None
        if len(sources)==1:
            ss = sources[0]
            if ss.screen_sizes and len(ss.screen_sizes)==1:
                screen1 = ss.screen_sizes[0]
                if len(screen1)>=10:
                    display_name, width, height, width_mm, height_mm, \
                        monitors, work_x, work_y, work_width, work_height = screen1[:10]
                    assert display_name or width_mm or height_mm or True    #just silences pydev warnings
                    #paint dark grey background for display dimensions:
                    fill_grey_rect((0.2, 0.2, 0.2), 0, 0, width, height)
                    paint_grey_rect((0.2, 0.2, 0.4), 0, 0, width, height)
                    #paint lighter grey background for workspace dimensions:
                    paint_grey_rect((0.5, 0.5, 0.5), work_x, work_y, work_width, work_height)
                    #paint each monitor with even lighter shades of grey:
                    for m in monitors:
                        if len(m)<7:
                            continue
                        plug_name, plug_x, plug_y, plug_width, plug_height, plug_width_mm, plug_height_mm = m[:7]
                        assert plug_name or plug_width_mm or plug_height_mm or True #just silences pydev warnings
                        paint_grey_rect((0.7, 0.7, 0.7), plug_x, plug_y, plug_width, plug_height)
                        if len(m)>=10:
                            dwork_x, dwork_y, dwork_width, dwork_height = m[7:11]
                            paint_grey_rect((1, 1, 1), dwork_x, dwork_y, dwork_width, dwork_height)
        #now paint all the windows on top:
        order = {}
        focus_history = tuple(self._focus_history)
        for wid, window in self._id_to_window.items():
            prio = int(self._has_focus==wid)*32768 + int(self._has_grab==wid)*65536
            if prio==0:
                try:
                    prio = rindex(focus_history, wid)
                except:
                    pass        #not in focus history!
            order[(prio, wid)] = window
        log("do_repaint_root_overlay() has_focus=%s, has_grab=%s, windows in order=%s",
            self._has_focus, self._has_grab, order)
        for k in sorted(order):
            window = order[k]
            x, y, w, h = window.get_property("geometry")[:4]
            image = window.get_image(0, 0, w, h)
            if image:
                self.update_root_overlay(window, 0, 0, image)
                frame = window.get_property("frame")
                if frame and tuple(frame)!=(0, 0, 0, 0):
                    left, right, top, bottom = frame
                    #always add a little something so we can see the edge:
                    left = max(1, left)
                    right = max(1, right)
                    top = max(1, top)
                    bottom = max(1, bottom)
                    rectangles = (
                                  (x-left,      y,          left,           h,      True),      #left side
                                  (x-left,      y-top,      w+left+right,   top,    True),      #top
                                  (x+w,         y,          right,          h,      True),      #right
                                  (x-left,      y+h,        w+left+right,   bottom, True),      #bottom
                                 )
                else:
                    rectangles = (
                                  (x, y, w, h, False),
                                 )
                log("rectangles for window frame=%s and geometry=%s : %s", frame, (x, y, w, h), rectangles)
                for x, y, w, h, fill in rectangles:
                    cr.new_path()
                    cr.set_source_rgb(0.1, 0.1, 0.1)
                    cr.set_line_width(1)
                    cr.rectangle(x, y, w, h)
                    if fill:
                        cr.fill()
                    else:
                        cr.stroke()
        #FIXME: use server mouse position, and use current cursor shape
        if ss:
            mlp = getattr(ss, "mouse_last_position", (0, 0))
            if mlp!=(0, 0):
                x, y = mlp
                cr.set_source_rgb(1.0, 0.5, 0.7)
                cr.new_path()
                cr.arc(x, y, 10.0, 0, 2.0 * math.pi)
                cr.stroke_preserve()
                cr.set_source_rgb(0.3, 0.4, 0.6)
                cr.fill()
        return False


    def do_make_screenshot_packet(self):
        log("grabbing screenshot")
        regions = []
        OR_regions = []
        for wid in reversed(sorted(self._id_to_window.keys())):
            window = self._id_to_window.get(wid)
            log("screenshot: window(%s)=%s", wid, window)
            if window is None:
                continue
            if not window.is_managed():
                log("screenshot: window %s is not/no longer managed", wid)
                continue
            x, y, w, h = window.get_property("geometry")[:4]
            log("screenshot: geometry(%s)=%s", window, (x, y, w, h))
            try:
                with xsync:
                    img = window.get_image(0, 0, w, h)
            except XError:
                log("%s.get_image%s", window, (0, 0, w, h), exc_info=True)
                log.warn("screenshot: window %s could not be captured", wid)
                continue
            if img is None:
                log.warn("screenshot: no pixels for window %s", wid)
                continue
            log("screenshot: image=%s, size=%s", img, img.get_size())
            if img.get_pixel_format() not in ("RGB", "RGBA", "XRGB", "BGRX", "ARGB", "BGRA"):
                log.warn("window pixels for window %s using an unexpected rgb format: %s", wid, img.get_pixel_format())
                continue
            item = (wid, x, y, img)
            if window.is_OR() or window.is_tray():
                OR_regions.append(item)
            elif self._has_focus==wid:
                #window with focus first (drawn last)
                regions.insert(0, item)
            else:
                regions.append(item)
        log("screenshot: found regions=%s, OR_regions=%s", len(regions), len(OR_regions))
        return self.make_screenshot_packet_from_regions(OR_regions+regions)


    def make_dbus_server(self):
        from xpra.x11.dbus.x11_dbus_server import X11_DBUS_Server
        return X11_DBUS_Server(self, os.environ.get("DISPLAY", "").lstrip(":"))


GObject.type_register(XpraServer)
