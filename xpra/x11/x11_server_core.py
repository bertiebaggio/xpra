# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2020 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import threading
from gi.repository import Gdk

from xpra.x11.bindings.core_bindings import set_context_check, X11CoreBindings     #@UnresolvedImport
from xpra.x11.bindings.randr_bindings import RandRBindings  #@UnresolvedImport
from xpra.x11.bindings.keyboard_bindings import X11KeyboardBindings #@UnresolvedImport
from xpra.x11.bindings.window_bindings import X11WindowBindings #@UnresolvedImport
from xpra.gtk_common.error import XError, xswallow, xsync, xlog, trap, verify_sync
from xpra.gtk_common.gtk_util import get_default_root_window
from xpra.server.server_uuid import save_uuid, get_uuid
from xpra.x11.vfb_util import parse_resolution
from xpra.x11.fakeXinerama import find_libfakeXinerama, save_fakeXinerama_config, cleanup_fakeXinerama
from xpra.x11.gtk_x11.prop import prop_get, prop_set, prop_del
from xpra.x11.gtk_x11.gdk_display_source import close_gdk_display_source
from xpra.x11.gtk_x11.gdk_bindings import init_x11_filter, cleanup_x11_filter, cleanup_all_event_receivers
from xpra.common import MAX_WINDOW_SIZE
from xpra.os_util import monotonic_time, strtobytes
from xpra.util import typedict, iround, envbool, first_time, XPRA_DPI_NOTIFICATION_ID
from xpra.net.compression import Compressed
from xpra.server.gtk_server_base import GTKServerBase
from xpra.x11.xkbhelper import clean_keyboard_state
from xpra.scripts.config import FALSE_OPTIONS
from xpra.log import Logger

set_context_check(verify_sync)
RandR = RandRBindings()
X11Keyboard = X11KeyboardBindings()
X11Core = X11CoreBindings()
X11Window = X11WindowBindings()


log = Logger("x11", "server")
keylog = Logger("x11", "server", "keyboard")
mouselog = Logger("x11", "server", "mouse")
grablog = Logger("server", "grab")
cursorlog = Logger("server", "cursor")
screenlog = Logger("server", "screen")
xinputlog = Logger("xinput")


ALWAYS_NOTIFY_MOTION = envbool("XPRA_ALWAYS_NOTIFY_MOTION", False)
FAKE_X11_INIT_ERROR = envbool("XPRA_FAKE_X11_INIT_ERROR", False)


class XTestPointerDevice:

    def __repr__(self):
        return "XTestPointerDevice"

    def move_pointer(self, screen_no, x, y, *_args):
        mouselog("xtest_fake_motion(%i, %s, %s)", screen_no, x, y)
        with xsync:
            X11Keyboard.xtest_fake_motion(screen_no, x, y)

    def click(self, button, pressed, *_args):
        mouselog("xtest_fake_button(%i, %s)", button, pressed)
        with xsync:
            X11Keyboard.xtest_fake_button(button, pressed)

    def close(self):
        pass

    def has_precise_wheel(self):
        return False


class X11ServerCore(GTKServerBase):
    """
        Base class for X11 servers,
        adds X11 specific methods to GTKServerBase.
        (see XpraServer or XpraX11ShadowServer for actual implementations)
    """

    def __init__(self):
        self.screen_number = Gdk.Screen.get_default().get_number()
        self.root_window = get_default_root_window()
        self.pointer_device = XTestPointerDevice()
        self.touchpad_device = None
        self.pointer_device_map = {}
        self.keys_pressed = {}
        self.last_mouse_user = None
        self.libfakeXinerama_so = None
        self.initial_resolution = None
        self.x11_filter = False
        self.randr_sizes_added = []
        super().__init__()
        log("XShape=%s", X11Window.displayHasXShape())

    def init(self, opts):
        self.do_init(opts)
        super().init(opts)

    def server_init(self):
        self.x11_init()
        from xpra.server import server_features
        if server_features.windows:
            from xpra.x11.x11_window_filters import init_x11_window_filters
            init_x11_window_filters()
        super().server_init()

    def do_init(self, opts):
        try:
            self.initial_resolution = parse_resolution(opts.resize_display)
        except ValueError:
            pass
        self.randr = bool(self.initial_resolution) or not (opts.resize_display in FALSE_OPTIONS)
        self.randr_exact_size = False
        self.fake_xinerama = "no"      #only enabled in seamless server
        self.current_xinerama_config = None
        #x11 keyboard bits:
        self.current_keyboard_group = None


    def x11_init(self):
        if FAKE_X11_INIT_ERROR:
            raise Exception("fake x11 init error")
        self.init_fake_xinerama()
        with xlog:
            clean_keyboard_state()
        with xlog:
            if not X11Keyboard.hasXFixes() and self.cursors:
                log.error("Error: cursor forwarding support disabled")
            if not X11Keyboard.hasXTest():
                log.error("Error: keyboard and mouse disabled")
            elif not X11Keyboard.hasXkb():
                log.error("Error: limited keyboard support")
        with xsync:
            self.init_x11_atoms()
        with xlog:
            if self.randr:
                self.init_randr()
        with xlog:
            self.init_cursor()
        with xlog:
            self.x11_filter = init_x11_filter()
        assert self.x11_filter
        with xlog:
            self.save_mode()

    def save_mode(self):
        prop_set(get_default_root_window(), "XPRA_SERVER_MODE", "latin1", self.get_server_mode())

    def init_fake_xinerama(self):
        if self.fake_xinerama in FALSE_OPTIONS:
            self.libfakeXinerama_so = None
        elif os.path.isabs(self.fake_xinerama):
            self.libfakeXinerama_so = self.fake_xinerama
        else:
            self.libfakeXinerama_so = find_libfakeXinerama()

    def init_randr(self):
        self.randr = RandR.has_randr()
        screenlog("randr=%s", self.randr)
        #check the property first,
        #because we may be inheriting this display,
        #in which case the screen sizes list may be longer than 1
        eprop = prop_get(self.root_window, "_XPRA_RANDR_EXACT_SIZE", "u32", ignore_errors=True, raise_xerrors=False)
        screenlog("_XPRA_RANDR_EXACT_SIZE=%s", eprop)
        self.randr_exact_size = eprop==1
        if not self.randr_exact_size:
            #ugly hackish way of detecting Xvfb with randr,
            #assume that it has only one resolution pre-defined:
            sizes = RandR.get_xrr_screen_sizes()
            if len(sizes)==1:
                self.randr_exact_size = True
                prop_set(self.root_window, "_XPRA_RANDR_EXACT_SIZE", "u32", 1)
            elif not sizes:
                #xwayland?
                self.randr = False
                self.randr_exact_size = False
        screenlog("randr=%s, exact size=%s", self.randr, self.randr_exact_size)
        screenlog("randr enabled: %s", self.randr)
        if not self.randr:
            screenlog.warn("Warning: no X11 RandR support on %s", os.environ.get("DISPLAY"))


    def init_cursor(self):
        #cursor:
        self.default_cursor_image = None
        self.last_cursor_serial = None
        self.last_cursor_image = None
        self.send_cursor_pending = False
        def get_default_cursor():
            self.default_cursor_image = X11Keyboard.get_cursor_image()
            cursorlog("get_default_cursor=%s", self.default_cursor_image)
        trap.swallow_synced(get_default_cursor)
        X11Keyboard.selectCursorChange(True)

    def get_display_bit_depth(self):
        with xlog:
            return X11Window.get_depth(X11Window.getDefaultRootWindow())
        return 0


    def init_x11_atoms(self):
        #some applications (like openoffice), do not work properly
        #if some x11 atoms aren't defined, so we define them in advance:
        atom_names = tuple("_NET_WM_WINDOW_TYPE"+wtype for wtype in (
            "",
            "_NORMAL",
            "_DESKTOP",
            "_DOCK",
            "_TOOLBAR",
            "_MENU",
            "_UTILITY",
            "_SPLASH",
            "_DIALOG",
            "_DROPDOWN_MENU",
            "_POPUP_MENU",
            "_TOOLTIP",
            "_NOTIFICATION",
            "_COMBO",
            "_DND",
            "_NORMAL"
            ))
        X11Core.intern_atoms(atom_names)


    def set_keyboard_layout_group(self, grp):
        if not self.keyboard_config:
            keylog("set_keyboard_layout_group(%i) ignored, no config", grp)
            return
        if not self.keyboard_config.xkbmap_layout_groups:
            keylog("set_keyboard_layout_group(%i) ignored, no layout groups support", grp)
            #not supported by the client that owns the current keyboard config,
            #so make sure we stick to the default group:
            grp = 0
        if not X11Keyboard.hasXkb():
            keylog("set_keyboard_layout_group(%i) ignored, no Xkb support", grp)
            return
        if grp<0:
            grp = 0
        if self.current_keyboard_group!=grp:
            keylog("set_keyboard_layout_group(%i) ignored, value unchanged", grp)
            return
        keylog("set_keyboard_layout_group(%i) config=%s, current keyboard group=%s",
               grp, self.keyboard_config, self.current_keyboard_group)
        try:
            with xsync:
                self.current_keyboard_group = X11Keyboard.set_layout_group(grp)
        except XError as e:
            keylog("set_keyboard_layout_group group=%s", grp, exc_info=True)
            keylog.error("Error: failed to set keyboard layout group '%s'", grp)
            keylog.error(" %s", e)

    def init_packet_handlers(self):
        super().init_packet_handlers()
        self.add_packet_handler("force-ungrab", self._process_force_ungrab)
        self.add_packet_handler("wheel-motion", self._process_wheel_motion)


    def init_virtual_devices(self, _devices):
        self.input_devices = "xtest"


    def get_child_env(self) -> dict:
        #adds fakeXinerama:
        env = super().get_child_env()
        if self.fake_xinerama and self.libfakeXinerama_so:
            env["LD_PRELOAD"] = self.libfakeXinerama_so
        return env

    def do_cleanup(self):
        log("do_cleanup() x11_filter=%s", self.x11_filter)
        if self.x11_filter:
            self.x11_filter = False
            cleanup_x11_filter()
            #try a few times:
            #errors happen because windows are being destroyed
            #(even more so when we cleanup)
            #and we don't really care too much about this
            for l in (log, log, log, log, log.warn):
                try:
                    with xsync:
                        cleanup_all_event_receivers()
                        #all went well, we're done
                        log("all event receivers have been removed")
                        break
                except Exception as e:
                    l("failed to remove event receivers: %s", e)
        if self.fake_xinerama:
            cleanup_fakeXinerama()
        with xlog:
            clean_keyboard_state()
        #prop_del does its own xsync:
        self.clean_x11_properties()
        super().do_cleanup()
        log("close_gdk_display_source()")
        close_gdk_display_source()


    def clean_x11_properties(self):
        self.do_clean_x11_properties("XPRA_SERVER_MODE", "_XPRA_RANDR_EXACT_SIZE")

    def do_clean_x11_properties(self, *properties):
        root = get_default_root_window()
        for prop in properties:
            try:
                prop_del(root, prop)
            except Exception as e:
                log("prop_del(%s, %s) %s", root, prop, e)


    def get_uuid(self):
        return get_uuid()

    def save_uuid(self):
        save_uuid(str(self.uuid))

    def set_keyboard_repeat(self, key_repeat):
        if key_repeat:
            self.key_repeat_delay, self.key_repeat_interval = key_repeat
            if self.key_repeat_delay>0 and self.key_repeat_interval>0:
                X11Keyboard.set_key_repeat_rate(self.key_repeat_delay, self.key_repeat_interval)
                keylog.info("setting key repeat rate from client: %sms delay / %sms interval",
                            self.key_repeat_delay, self.key_repeat_interval)
        else:
            #dont do any jitter compensation:
            self.key_repeat_delay = -1
            self.key_repeat_interval = -1
            #but do set a default repeat rate:
            X11Keyboard.set_key_repeat_rate(500, 30)
            keylog("keyboard repeat disabled")

    def make_hello(self, source):
        capabilities = super().make_hello(source)
        capabilities["server_type"] = "Python/gtk/x11"
        if source.wants_features:
            capabilities.update({
                    "resize_screen"             : self.randr,
                    "resize_exact"              : self.randr_exact_size,
                    "force_ungrab"              : True,
                    "keyboard.fast-switching"   : True,
                    "wheel.precise"             : self.pointer_device.has_precise_wheel(),
                    "touchpad-device"              : bool(self.touchpad_device),
                    })
            if self.randr:
                sizes = self.get_all_screen_sizes()
                if len(sizes)>1:
                    capabilities["screen-sizes"] = sizes
            if self.default_cursor_image and source.wants_default_cursor:
                capabilities["cursor.default"] = self.default_cursor_image
        return capabilities

    def do_get_info(self, proto, server_sources) -> dict:
        start = monotonic_time()
        info = super().do_get_info(proto, server_sources)
        sinfo = info.setdefault("server", {})
        sinfo.update({
            "type"                  : "Python/gtk/x11",
            "fakeXinerama"          : bool(self.libfakeXinerama_so),
            "libfakeXinerama"       : self.libfakeXinerama_so or "",
            })
        log("X11ServerCore.do_get_info took %ims", (monotonic_time()-start)*1000)
        return info

    def get_ui_info(self, proto, wids=None, *args) -> dict:
        log("do_get_info thread=%s", threading.current_thread())
        info = super().get_ui_info(proto, wids, *args)
        #this is added here because the server keyboard config doesn't know about "keys_pressed"..
        if not self.readonly:
            with xlog:
                info.setdefault("keyboard", {}).update({
                    "state"             : {
                        "keys_pressed"  : tuple(self.keys_pressed.keys()),
                        "keycodes-down" : X11Keyboard.get_keycodes_down(),
                        },
                    "fast-switching"    : True,
                    "layout-group"      : X11Keyboard.get_layout_group(),
                    })
        sinfo = info.setdefault("server", {})
        try:
            from xpra.x11.gtk_x11.composite import CompositeHelper
            sinfo["XShm"] = CompositeHelper.XShmEnabled
        except ImportError:
            pass
        #cursor:
        info.setdefault("cursor", {}).update(self.get_cursor_info())
        with xswallow:
            sinfo.update({
                "Xkb"                   : X11Keyboard.hasXkb(),
                "XTest"                 : X11Keyboard.hasXTest(),
                })
        #randr:
        if self.randr:
            with xlog:
                sizes = self.get_all_screen_sizes()
                if sizes:
                    sinfo["randr"] = {
                        ""          : True,
                        "options"   : tuple(reversed(sorted(sizes))),
                        "exact"     : self.randr_exact_size,
                        }
        return info


    def get_cursor_info(self) -> dict:
        #(NOT from UI thread)
        #copy to prevent race:
        cd = self.last_cursor_image
        if cd is None:
            return {"" : "None"}
        dci = self.default_cursor_image
        cinfo = {
            "is-default"   : bool(dci) and len(dci)>=8 and len(cd)>=8 and cd[7]==dci[7],
            }
        #all but pixels:
        for i, x in enumerate(("x", "y", "width", "height", "xhot", "yhot", "serial", None, "name")):
            if x:
                v = cd[i] or ""
                cinfo[x] = v
        return cinfo

    def get_window_info(self, window) -> dict:
        info = super().get_window_info(window)
        info["XShm"] = window.uses_XShm()
        info["geometry"] = window.get_geometry()
        return info


    def get_keyboard_config(self, props=typedict()):
        from xpra.x11.server_keyboard_config import KeyboardConfig
        keyboard_config = KeyboardConfig()
        keyboard_config.enabled = props.boolget("keyboard", True)
        keyboard_config.parse_options(props)
        keyboard_config.xkbmap_layout = props.strget("xkbmap_layout")
        keyboard_config.xkbmap_variant = props.strget("xkbmap_variant")
        keyboard_config.xkbmap_options = props.strget("xkbmap_options")
        keylog("get_keyboard_config(..)=%s", keyboard_config)
        return keyboard_config


    def set_keymap(self, server_source, force=False):
        if self.readonly:
            return
        try:
            #prevent _keys_changed() from firing:
            #(using a flag instead of keymap.disconnect(handler) as this did not seem to work!)
            self.keymap_changing = True

            #if sharing, don't set the keymap, translate the existing one:
            other_ui_clients = [s.uuid for s in self._server_sources.values() if s!=server_source and s.ui_client]
            translate_only = len(other_ui_clients)>0
            with xsync:
                server_source.set_keymap(self.keyboard_config, self.keys_pressed, force, translate_only)    #pylint: disable=access-member-before-definition
                self.keyboard_config = server_source.keyboard_config
        finally:
            # re-enable via idle_add to give all the pending
            # events a chance to run first (and get ignored)
            def reenable_keymap_changes(*args):
                keylog("reenable_keymap_changes(%s)", args)
                self.keymap_changing = False
                self._keys_changed()
            self.idle_add(reenable_keymap_changes)


    def clear_keys_pressed(self):
        if self.readonly:
            return
        keylog("clear_keys_pressed()")
        #make sure the timer doesn't fire and interfere:
        self.cancel_key_repeat_timer()
        #clear all the keys we know about:
        if self.keys_pressed:
            keylog("clearing keys pressed: %s", self.keys_pressed)
            with xsync:
                for keycode in self.keys_pressed:
                    self.fake_key(keycode, False)
            self.keys_pressed = {}
        #this will take care of any remaining ones we are not aware of:
        #(there should not be any - but we want to be certain)
        clean_keyboard_state()


    def get_cursor_sizes(self):
        display = Gdk.Display.get_default()
        return display.get_default_cursor_size(), display.get_maximal_cursor_size()

    def get_cursor_image(self):
        #must be called from the UI thread!
        with xlog:
            return X11Keyboard.get_cursor_image()

    def get_cursor_data(self):
        #must be called from the UI thread!
        cursor_image = self.get_cursor_image()
        if cursor_image is None:
            cursorlog("get_cursor_data() failed to get cursor image")
            return None, []
        self.last_cursor_image = cursor_image
        pixels = self.last_cursor_image[7]
        cursorlog("get_cursor_image() cursor=%s", cursor_image[:7]+["%s bytes" % len(pixels)]+cursor_image[8:])
        if self.default_cursor_image is not None and str(pixels)==str(self.default_cursor_image[7]):
            cursorlog("get_cursor_data(): default cursor - clearing it")
            cursor_image = None
        cursor_sizes = self.get_cursor_sizes()
        return (cursor_image, cursor_sizes)


    def get_all_screen_sizes(self):
        #workaround for #2910: the resolutions we add are not seen by XRRSizes!
        # so we keep track of the ones we have added ourselves:
        sizes = list(RandR.get_xrr_screen_sizes())
        for w, h in self.randr_sizes_added:
            if (w, h) not in sizes:
                sizes.append((w, h))
        return tuple(sizes)

    def get_max_screen_size(self):
        max_w, max_h = self.root_window.get_geometry()[2:4]
        if self.randr:
            sizes = self.get_all_screen_sizes()
            if len(sizes)>=1:
                for w,h in sizes:
                    max_w = max(max_w, w)
                    max_h = max(max_h, h)
            if max_w>MAX_WINDOW_SIZE or max_h>MAX_WINDOW_SIZE:
                screenlog.warn("Warning: maximum screen size is very large: %sx%s", max_w, max_h)
                screenlog.warn(" you may encounter window sizing problems")
            screenlog("get_max_screen_size()=%s", (max_w, max_h))
        return max_w, max_h


    def configure_best_screen_size(self):
        #return ServerBase.set_best_screen_size(self)
        """ sets the screen size to use the largest width and height used by any of the clients """
        root_w, root_h = self.root_window.get_geometry()[2:4]
        if not self.randr:
            return root_w, root_h
        sss = tuple(x for x in self._server_sources.values() if x.ui_client)
        if len(sss)>1:
            screenlog.info("screen used by %i clients:", len(sss))
        bigger = True
        max_w, max_h = 0, 0
        min_w, min_h = 16384, 16384
        for ss in sss:
            client_size = ss.desktop_size
            if not client_size:
                size = "unknown"
            else:
                w, h = client_size
                size = "%ix%i" % (w, h)
                max_w = max(max_w, w)
                max_h = max(max_h, h)
                if w>0:
                    min_w = min(min_w, w)
                if h>0:
                    min_h = min(min_h, h)
                bigger = bigger and ss.screen_resize_bigger
            if len(sss)>1:
                screenlog.info("* %s: %s", ss.uuid, size)
        if bigger:
            w, h = max_w, max_h
        else:
            w, h = min_w, min_h
        screenlog("current server resolution is %ix%i", root_w, root_h)
        screenlog("maximum client resolution is %ix%i",  max_w, max_h)
        screenlog("minimum client resolution is %ix%i",  min_w, min_h)
        screenlog("want: %s, so using %ix%i", "bigger" if bigger else "smaller", w, h)
        if w<=0 or h<=0:
            #invalid - use fallback
            return root_w, root_h
        return self.set_screen_size(w, h, bigger)

    def get_best_screen_size(self, desired_w, desired_h, bigger=True):
        return self.do_get_best_screen_size(desired_w, desired_h, bigger)

    def do_get_best_screen_size(self, desired_w, desired_h, bigger=True):
        if not self.randr:
            return desired_w, desired_h
        screen_sizes = self.get_all_screen_sizes()
        if (desired_w, desired_h) in screen_sizes:
            return desired_w, desired_h
        if self.randr_exact_size:
            try:
                with xsync:
                    v = RandR.add_screen_size(desired_w, desired_h)
                    if v:
                        #we have to wait a little bit
                        #to make sure that everything sees the new resolution
                        #(ideally this method would be split in two and this would be a callback)
                        self.randr_sizes_added.append(v)
                        import time
                        time.sleep(0.5)
                        return v
            except XError as e:
                screenlog("add_screen_size(%s, %s)", desired_w, desired_h, exc_info=True)
                screenlog.warn("Warning: failed to add resolution %ix%i:", desired_w, desired_h)
                screenlog.warn(" %s", e)
            #re-query:
            screen_sizes = self.get_all_screen_sizes()
        #try to find the best screen size to resize to:
        new_size = None
        closest = {}
        for w,h in screen_sizes:
            if (w<desired_w)==bigger or (h<desired_h)==bigger:
                distance = abs(desired_w*desired_h - w*h)
                closest[distance] = (w, h)
                continue            #size is too small/big for client
            if new_size:
                ew,eh = new_size    #pylint: disable=unpacking-non-sequence
                if (ew*eh<w*h)==bigger:
                    continue        #we found a better (smaller/bigger) candidate already
            new_size = w,h
        if not new_size:
            screenlog.warn("Warning: no matching resolution found for %sx%s", desired_w, desired_h)
            if closest:
                min_dist = sorted(closest.keys())[0]
                new_size = closest[min_dist]
                screenlog.warn(" using %sx%s instead", *new_size)
            else:
                root_w, root_h = self.root_window.get_size()
                return root_w, root_h
        screenlog("best %s resolution for client(%sx%s) is: %s",
                  ["smaller", "bigger"][bigger], desired_w, desired_h, new_size)
        w, h = new_size
        return w, h

    def set_screen_size(self, desired_w, desired_h, bigger=True):
        screenlog("set_screen_size%s", (desired_w, desired_h, bigger))
        root_w, root_h = self.root_window.get_geometry()[2:4]
        if not self.randr:
            return root_w,root_h
        if desired_w==root_w and desired_h==root_h and not self.fake_xinerama:
            return root_w,root_h    #unlikely: perfect match already!
        #clients may supply "xdpi" and "ydpi" (v0.15 onwards), or just "dpi", or nothing...
        xdpi = self.xdpi or self.dpi
        ydpi = self.ydpi or self.dpi
        screenlog("set_screen_size(%s, %s, %s) xdpi=%s, ydpi=%s",
                  desired_w, desired_h, bigger, xdpi, ydpi)
        if xdpi<=0 or ydpi<=0:
            #use some sane defaults: either the command line option, or fallback to 96
            #(96 is better than nothing, because we do want to set the dpi
            # to avoid Xdummy setting a crazy dpi from the virtual screen dimensions)
            xdpi = self.default_dpi or 96
            ydpi = self.default_dpi or 96
            #find the "physical" screen dimensions, so we can calculate the required dpi
            #(and do this before changing the resolution)
            wmm, hmm = 0, 0
            client_w, client_h = 0, 0
            sss = self._server_sources.values()
            for ss in sss:
                for s in ss.screen_sizes:
                    if len(s)>=10:
                        #(display_name, width, height, width_mm, height_mm, monitors,
                        # work_x, work_y, work_width, work_height)
                        client_w = max(client_w, s[1])
                        client_h = max(client_h, s[2])
                        wmm = max(wmm, s[3])
                        hmm = max(hmm, s[4])
            if wmm>0 and hmm>0 and client_w>0 and client_h>0:
                #calculate "real" dpi:
                xdpi = iround(client_w * 25.4 / wmm)
                ydpi = iround(client_h * 25.4 / hmm)
                screenlog("calculated DPI: %s x %s (from w: %s / %s, h: %s / %s)",
                          xdpi, ydpi, client_w, wmm, client_h, hmm)
        self.set_dpi(xdpi, ydpi)

        #try to find the best screen size to resize to:
        w, h = self.get_best_screen_size(desired_w, desired_h, bigger)

        #fakeXinerama:
        ui_clients = [s for s in self._server_sources.values() if s.ui_client]
        source = None
        screen_sizes = []
        if len(ui_clients)==1:
            source = ui_clients[0]
            screen_sizes = source.screen_sizes
        else:
            screenlog("fakeXinerama can only be enabled for a single client (found %s)" % len(ui_clients))
        xinerama_changed = save_fakeXinerama_config(self.fake_xinerama and len(ui_clients)==1, source, screen_sizes)
        #we can only keep things unchanged if xinerama was also unchanged
        #(many apps will only query xinerama again if they get a randr notification)
        if (w==root_w and h==root_h) and not xinerama_changed:
            screenlog.info("best resolution matching %sx%s is unchanged: %sx%s", desired_w, desired_h, w, h)
            return root_w, root_h
        try:
            if (w==root_w and h==root_h) and xinerama_changed:
                #xinerama was changed, but the RandR resolution will not be...
                #and we need a RandR change to force applications to re-query it
                #so we temporarily switch to another resolution to force
                #the change! (ugly! but this works)
                with xsync:
                    temp = {}
                    for tw,th in self.get_all_screen_sizes():
                        if tw!=w or th!=h:
                            #use the number of extra pixels as key:
                            #(so we can choose the closest resolution)
                            temp[abs((tw*th) - (w*h))] = (tw, th)
                if not temp:
                    screenlog.warn("cannot find a temporary resolution for Xinerama workaround!")
                else:
                    k = sorted(temp.keys())[0]
                    tw, th = temp[k]
                    screenlog.info("temporarily switching to %sx%s as a Xinerama workaround", tw, th)
                    with xsync:
                        RandR.set_screen_size(tw, th)
            with xsync:
                RandR.get_screen_size()
            #Xdummy with randr 1.2:
            screenlog("using XRRSetScreenConfigAndRate with %ix%i", w, h)
            with xsync:
                RandR.set_screen_size(w, h)
            if self.randr_exact_size:
                #Xvfb with randr > 1.2: the resolution has been added
                #we can use XRRSetScreenSize:
                try:
                    with xsync:
                        RandR.xrr_set_screen_size(w, h, self.xdpi or self.dpi or 96, self.ydpi or self.dpi or 96)
                except XError:
                    screenlog("XRRSetScreenSize failed", exc_info=True)
            screenlog("calling RandR.get_screen_size()")
            with xsync:
                root_w, root_h = RandR.get_screen_size()
            screenlog("RandR.get_screen_size()=%s,%s", root_w, root_h)
            screenlog("RandR.get_vrefresh()=%s", RandR.get_vrefresh())
            if root_w!=w or root_h!=h:
                screenlog.warn("Warning: tried to set resolution to %ix%i", w, h)
                screenlog.warn(" and ended up with %ix%i", root_w, root_h)
            else:
                msg = "server virtual display now set to %sx%s" % (root_w, root_h)
                if desired_w!=root_w or desired_h!=root_h:
                    msg += " (best match for %sx%s)" % (desired_w, desired_h)
                screenlog.info(msg)
            def show_dpi():
                wmm, hmm = RandR.get_screen_size_mm()      #ie: (1280, 1024)
                screenlog("RandR.get_screen_size_mm=%s,%s", wmm, hmm)
                actual_xdpi = iround(root_w * 25.4 / wmm)
                actual_ydpi = iround(root_h * 25.4 / hmm)
                if abs(actual_xdpi-xdpi)<=1 and abs(actual_ydpi-ydpi)<=1:
                    screenlog.info("DPI set to %s x %s", actual_xdpi, actual_ydpi)
                    screenlog("wanted: %s x %s", xdpi, ydpi)
                else:
                    #should this be a warning:
                    l = screenlog.info
                    maxdelta = max(abs(actual_xdpi-xdpi), abs(actual_ydpi-ydpi))
                    if maxdelta>=10:
                        l = log.warn
                    messages = [
                        "DPI set to %s x %s (wanted %s x %s)" % (actual_xdpi, actual_ydpi, xdpi, ydpi),
                        ]
                    if maxdelta>=10:
                        messages.append("you may experience scaling problems, such as huge or small fonts, etc")
                        messages.append("to fix this issue, try the dpi switch, or use a patched Xorg dummy driver")
                        self.notify_dpi_warning("\n".join(messages))
                    for i,message in enumerate(messages):
                        l("%s%s", ["", " "][i>0], message)
            #show dpi via idle_add so server has time to change the screen size (mm)
            self.idle_add(show_dpi)
        except Exception as e:
            screenlog.error("ouch, failed to set new resolution: %s", e, exc_info=True)
        return root_w, root_h

    def notify_dpi_warning(self, body):
        sources = tuple(self._server_sources.values())
        if len(sources)==1:
            ss = sources[0]
            if first_time("DPI-warning-%s" % ss.uuid):
                sources[0].may_notify(XPRA_DPI_NOTIFICATION_ID, "DPI Issue", body, icon_name="font")


    def _process_server_settings(self, _proto, packet):
        settings = packet[1]
        log("process_server_settings: %s", settings)
        self.update_server_settings(settings)

    def update_server_settings(self, _settings, _reset=False):
        #implemented in the X11 xpra server only for now
        #(does not make sense to update a shadow server)
        log("ignoring server settings update in %s", self)


    def _process_force_ungrab(self, proto, _packet):
        #ignore the window id: wid = packet[1]
        grablog("force ungrab from %s", proto)
        self.X11_ungrab()

    def X11_ungrab(self):
        grablog("X11_ungrab")
        with xsync:
            X11Core.UngrabKeyboard()
            X11Core.UngrabPointer()


    def fake_key(self, keycode, press):
        keylog("fake_key(%s, %s)", keycode, press)
        mink, maxk = X11Keyboard.get_minmax_keycodes()
        if keycode<mink or keycode>maxk:
            return
        with xsync:
            X11Keyboard.xtest_fake_key(keycode, press)


    def do_xpra_cursor_event(self, event):
        if not self.cursors:
            return
        if self.last_cursor_serial==event.cursor_serial:
            cursorlog("ignoring cursor event %s with the same serial number %s", event, self.last_cursor_serial)
            return
        cursorlog("cursor_event: %s", event)
        self.last_cursor_serial = event.cursor_serial
        for ss in self.window_sources():
            ss.send_cursor()


    def _motion_signaled(self, model, event):
        mouselog("motion_signaled(%s, %s) last mouse user=%s", model, event, self.last_mouse_user)
        #find the window model for this gdk window:
        wid = self._window_to_id.get(model)
        if not wid:
            return
        for ss in self._server_sources.values():
            if ALWAYS_NOTIFY_MOTION or self.last_mouse_user is None or self.last_mouse_user!=ss.uuid:
                if hasattr(ss, "update_mouse"):
                    ss.update_mouse(wid, event.x_root, event.y_root, event.x, event.y)


    def do_xpra_xkb_event(self, event):
        #X11: XKBNotify
        log("WindowModel.do_xpra_xkb_event(%r)" % event)
        if event.subtype!="bell":
            log.error("do_xpra_xkb_event(%r) unknown event type: %s" % (event, event.type))
            return
        #bell events on our windows will come through the bell signal,
        #this method is a catch-all for events on windows we don't manage,
        #so we use wid=0 for that:
        wid = 0
        for ss in self.window_sources():
            name = strtobytes(event.bell_name or "")
            ss.bell(wid, event.device, event.percent, event.pitch, event.duration, event.bell_class, event.bell_id, name)


    def _bell_signaled(self, wm, event):
        log("bell signaled on window %#x", event.window.get_xid())
        if not self.bell:
            return
        wid = 0
        if event.window!=get_default_root_window() and event.window_model is not None:
            wid = self._window_to_id.get(event.window_model, 0)
        log("_bell_signaled(%s,%r) wid=%s", wm, event, wid)
        for ss in self.window_sources():
            name = strtobytes(event.bell_name or "")
            ss.bell(wid, event.device, event.percent, event.pitch, event.duration, event.bell_class, event.bell_id, name)


    def get_screen_number(self, _wid):
        #maybe this should be in all cases (it is in desktop_server):
        #model = self._id_to_window.get(wid)
        #return model.client_window.get_screen().get_number()
        #return Gdk.Display.get_default().get_default_screen().get_number()
        #-1 uses the current screen
        return -1


    def cleanup_input_devices(self):
        pass


    def setup_input_devices(self):
        from xpra.server import server_features
        xinputlog("setup_input_devices() input_devices feature=%s", server_features.input_devices)
        if not server_features.input_devices:
            return
        xinputlog("setup_input_devices() format=%s, input_devices=%s", self.input_devices_format, self.input_devices)
        xinputlog("setup_input_devices() input_devices_data=%s", self.input_devices_data)
        #xinputlog("setup_input_devices() input_devices_data=%s", self.input_devices_data)
        xinputlog("setup_input_devices() pointer device=%s", self.pointer_device)
        xinputlog("setup_input_devices() touchpad device=%s", self.touchpad_device)
        self.pointer_device_map = {}
        if not self.touchpad_device:
            #no need to assign anything, we only have one device anyway
            return
        #if we find any absolute pointer devices,
        #map them to the "touchpad_device"
        XIModeAbsolute = 1
        for deviceid, device_data in self.input_devices_data.items():
            name = device_data.get("name")
            #xinputlog("[%i]=%s", deviceid, device_data)
            xinputlog("[%i]=%s", deviceid, name)
            if device_data.get("use")!="slave pointer":
                continue
            classes = device_data.get("classes")
            if not classes:
                continue
            #look for absolute pointer devices:
            touchpad_axes = []
            for i, defs in classes.items():
                xinputlog(" [%i]=%s", i, defs)
                mode = defs.get("mode")
                label = defs.get("label")
                if not mode or mode!=XIModeAbsolute:
                    continue
                if defs.get("min", -1)==0 and defs.get("max", -1)==(2**24-1):
                    touchpad_axes.append((i, label))
            if len(touchpad_axes)==2:
                xinputlog.info("found touchpad device: %s", name)
                xinputlog("axes: %s", touchpad_axes)
                self.pointer_device_map[deviceid] = self.touchpad_device


    def _process_wheel_motion(self, proto, packet):
        assert self.pointer_device.has_precise_wheel()
        wid, button, distance, pointer, modifiers, _buttons = packet[1:7]
        with xsync:
            if self.do_process_mouse_common(proto, wid, pointer):
                self._update_modifiers(proto, wid, modifiers)
                self.pointer_device.wheel_motion(button, distance/1000.0)   #pylint: disable=no-member

    def get_pointer_device(self, deviceid):
        #mouselog("get_pointer_device(%i) input_devices_data=%s", deviceid, self.input_devices_data)
        if self.input_devices_data:
            device_data = self.input_devices_data.get(deviceid)
            if device_data:
                mouselog("get_pointer_device(%i) device=%s", deviceid, device_data.get("name"))
        device = self.pointer_device_map.get(deviceid) or self.pointer_device
        return device


    def _get_pointer_abs_coordinates(self, wid, pos):
        #simple absolute coordinates
        x, y = pos[:2]
        from xpra.server.mixins.window_server import WindowServer
        if len(pos)>=4 and isinstance(self, WindowServer):
            #relative coordinates
            model = self._id_to_window.get(wid)
            if model:
                rx, ry = pos[2:4]
                geom = model.get_geometry()
                x = geom[0]+rx
                y = geom[1]+ry
                log("_get_pointer_abs_coordinates(%i, %s)=%s window geometry=%s", wid, pos, (x, y), geom)
        return x, y

    def _move_pointer(self, wid, pos, deviceid=-1, *args):
        #(this is called within an xswallow context)
        screen_no = self.get_screen_number(wid)
        device = self.get_pointer_device(deviceid)
        x, y = self._get_pointer_abs_coordinates(wid, pos)
        mouselog("move_pointer(%s, %s, %s) screen_no=%i, device=%s, position=%s",
                 wid, pos, deviceid, screen_no, device, (x, y))
        try:
            device.move_pointer(screen_no, x, y, *args)
        except Exception as e:
            mouselog.error("Error: failed to move the pointer to %sx%s using %s", x, y, device)
            mouselog.error(" %s", e)

    def do_process_mouse_common(self, proto, wid, pointer, deviceid=-1, *args):
        mouselog("do_process_mouse_common%s", tuple([proto, wid, pointer, deviceid]+list(args)))
        if self.readonly:
            return None
        pos = self.root_window.get_pointer()[-3:-1]
        uuid = None
        if proto:
            ss = self.get_server_source(proto)
            if ss:
                uuid = ss.uuid
        if pos!=pointer[:2] or self.input_devices=="xi":
            self.last_mouse_user = uuid
            with xswallow:
                self._move_pointer(wid, pointer, deviceid, *args)
        return pointer

    def _update_modifiers(self, proto, wid, modifiers):
        if self.readonly:
            return
        ss = self.get_server_source(proto)
        if ss:
            if self.ui_driver and self.ui_driver!=ss.uuid:
                return
            ss.make_keymask_match(modifiers)
            if wid==self.get_focus():
                ss.user_event()

    def do_process_button_action(self, proto, wid, button, pressed, pointer, modifiers, _buttons=(), deviceid=-1, *_args):
        self._update_modifiers(proto, wid, modifiers)
        #TODO: pass extra args
        if self._process_mouse_common(proto, wid, pointer, deviceid):
            self.button_action(pointer, button, pressed, deviceid)

    def button_action(self, pointer, button, pressed, deviceid=-1, *args):
        device = self.get_pointer_device(deviceid)
        assert device, "pointer device %s not found" % deviceid
        try:
            log("%s%s", device.click, (button, pressed, args))
            with xsync:
                device.click(button, pressed, *args)
        except XError:
            log("button_action(%s, %s, %s, %s, %s)", pointer, button, pressed, deviceid, args, exc_info=True)
            log.error("Error: failed (un)press mouse button %s", button)
            if button>=4:
                log.error(" (perhaps your Xvfb does not support mousewheels?)")


    def make_screenshot_packet_from_regions(self, regions):
        #regions = array of (wid, x, y, PIL.Image)
        if not regions:
            log("screenshot: no regions found, returning empty 0x0 image!")
            return ["screenshot", 0, 0, "png", -1, ""]
        #in theory, we could run the rest in a non-UI thread since we're done with GTK..
        minx = min(x for (_,x,_,_) in regions)
        miny = min(y for (_,_,y,_) in regions)
        maxx = max((x+img.get_width()) for (_,x,_,img) in regions)
        maxy = max((y+img.get_height()) for (_,_,y,img) in regions)
        width = maxx-minx
        height = maxy-miny
        log("screenshot: %sx%s, min x=%s y=%s", width, height, minx, miny)
        from PIL import Image                           #@UnresolvedImport
        screenshot = Image.new("RGBA", (width, height))
        for wid, x, y, img in reversed(regions):
            pixel_format = img.get_pixel_format()
            target_format = {
                     "XRGB"   : "RGB",
                     "BGRX"   : "RGB",
                     "BGRA"   : "RGBA"}.get(pixel_format, pixel_format)
            pixels = img.get_pixels()
            w = img.get_width()
            h = img.get_height()
            #PIL cannot use the memoryview directly:
            if isinstance(pixels, memoryview):
                pixels = pixels.tobytes()
            try:
                window_image = Image.frombuffer(target_format, (w, h), pixels, "raw", pixel_format, img.get_rowstride())
            except Exception:
                log.error("Error parsing window pixels in %s format for window %i", pixel_format, wid, exc_info=True)
                continue
            tx = x-minx
            ty = y-miny
            screenshot.paste(window_image, (tx, ty))
        from io import BytesIO
        buf = BytesIO()
        screenshot.save(buf, "png")
        data = buf.getvalue()
        buf.close()
        packet = ["screenshot", width, height, "png", width*4, Compressed("png", data)]
        log("screenshot: %sx%s %s", packet[1], packet[2], packet[-1])
        return packet
