#!/usr/bin/env python
 
#        +-----------------------------------------------------------------------------+
#        | GPL                                                                         |
#        +-----------------------------------------------------------------------------+
#        | Copyright (c) Brett Smith <tanktarta@blueyonder.co.uk>                      |
#        |                                                                             |
#        | This program is free software; you can redistribute it and/or               |
#        | modify it under the terms of the GNU General Public License                 |
#        | as published by the Free Software Foundation; either version 2              |
#        | of the License, or (at your option) any later version.                      |
#        |                                                                             |
#        | This program is distributed in the hope that it will be useful,             |
#        | but WITHOUT ANY WARRANTY; without even the implied warranty of              |
#        | MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the               |
#        | GNU General Public License for more details.                                |
#        |                                                                             |
#        | You should have received a copy of the GNU General Public License           |
#        | along with this program; if not, write to the Free Software                 |
#        | Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA. |
#        +-----------------------------------------------------------------------------+
 
import sys
import pygtk
pygtk.require('2.0')
import gtk
import gnome.ui
import os.path
import gobject
import g15_globals as pglobals
import g15_screen as g15screen
import g15_driver as g15driver
import g15_driver_manager as g15drivermanager
import g15_profile as g15profile
import g15_theme as g15theme
import g15_dbus as g15dbus
import traceback
import gconf
import g15_util as g15util
import Xlib.X 
import Xlib.XK
import Xlib.display
import Xlib.protocol
import time
import g15_plugins as g15plugins
import dbus
import glib
from threading import RLock
from threading import Thread
from dbus.mainloop.glib import threads_init
from g15_exceptions import NotConnectedException

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
glib.set_application_name(pglobals.name)

# Determine whether to use XTest for sending key events to X
UseXTest = True
try :
    import Xlib.ext.xtest
except ImportError:
    UseXTest = False
     
local_dpy = Xlib.display.Display()
window = local_dpy.get_input_focus()._data["focus"];

if UseXTest and not local_dpy.query_extension("XTEST") :
    UseXTest = False
    
NAME = "Gnome15"
VERSION = pglobals.version

COLOURS = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255), (255, 255, 255)]

special_X_keysyms = {
    ' ' : "space",
    '\t' : "Tab",
    '\n' : "Return", # for some reason this needs to be cr, not lf
    '\r' : "Return",
    '\e' : "Escape",
    '!' : "exclam",
    '#' : "numbersign",
    '%' : "percent",
    '$' : "dollar",
    '&' : "ampersand",
    '"' : "quotedbl",
    '\'' : "apostrophe",
    '(' : "parenleft",
    ')' : "parenright",
    '*' : "asterisk",
    '=' : "equal",
    '+' : "plus",
    ',' : "comma",
    '-' : "minus",
    '.' : "period",
    '/' : "slash",
    ':' : "colon",
    ';' : "semicolon",
    '<' : "less",
    '>' : "greater",
    '?' : "question",
    '@' : "at",
    '[' : "bracketleft",
    ']' : "bracketright",
    '\\' : "backslash",
    '^' : "asciicircum",
    '_' : "underscore",
    '`' : "grave",
    '{' : "braceleft",
    '|' : "bar",
    '}' : "braceright",
    '~' : "asciitilde"
    }



class G15Splash():
    
    def __init__(self, screen, gconf_client):
        self.screen = screen        
        self.progress = 0.0
        self.text = "Starting up .."
        self.theme = g15theme.G15Theme(pglobals.image_dir, self.screen, "background")
        self.page = self.screen.new_page(self.paint, priority=g15screen.PRI_EXCLUSIVE, id="Splash", thumbnail_painter=self.paint_thumbnail)
        self.screen.redraw(self.page)
        icon_path = g15util.get_icon_path("gnome15")
        if icon_path == None:
            icon_path = os.path.join(pglobals.icons_dir, 'gnome15.svg')
        self.logo = g15util.load_surface_from_file(icon_path)
        
    def paint(self, canvas):
        properties = {
                      "version": VERSION,
                      "progress": self.progress,
                      "text": self.text
                      }
        self.theme.draw(canvas, properties)
        
    def paint_thumbnail(self, canvas, allocated_size, horizontal):
        return g15util.paint_thumbnail_image(allocated_size, self.logo, canvas)
        
    def complete(self):
        self.progress = 100
        self.screen.redraw(self.page)
        time.sleep(1.0)
        self.screen.set_priority(self.page, g15screen.PRI_LOW)
        
    def remove(self):
        self.screen.del_page(self.page)
        
    def update_splash(self, value, max, text=None):
        self.progress = (float(value) / float(max)) * 100.0
        self.screen.redraw(self.page)
        if text != None:
            self.text = text

class G15Service(Thread):
    
    def __init__(self, service_host, parent_window=None):
        Thread.__init__(self)
        self.first_page = None
        self.splash = None
        self.parent_window = parent_window
        self.service_host = service_host
        self.reschedule_lock = RLock()
        self.connection_lock = RLock()
        self.last_error = None
        self.loading_complete = False
        self.control_handles = []
        self.active_window = None
        self.color_no = 1
        self.cycle_timer = None
        self.shutting_down = False
        self.conf_client = gconf.client_get_default()
        self.defeat_profile_change = False
        self.screen = g15screen.G15Screen(self)
        
    def run(self):
        
        # Get the driver. If it is not configured, configuration will be required at this point
        self.driver = g15drivermanager.get_driver(self.conf_client, on_close=self.on_driver_close)
        
        # Load main Glade file
        g15Config = os.path.join(pglobals.glade_dir, 'g15-config.glade')        
        self.widget_tree = gtk.Builder()
        self.widget_tree.add_from_file(g15Config)
        
        # Create the screen and plugin manager
        self.screen.add_screen_change_listener(self)
        self.plugins = g15plugins.G15Plugins(self.screen)
        self.plugins.start()

        # Start the driver
        self.attempt_connection() 
        
        # Monitor gconf
        self.conf_client.add_dir("/apps/gnome15", gconf.CLIENT_PRELOAD_NONE)
        self.conf_client.notify_add("/apps/gnome15/cycle_screens", self.resched_cycle);
        self.conf_client.notify_add("/apps/gnome15/active_profile", self.active_profile_changed);
        self.conf_client.notify_add("/apps/gnome15/profiles", self.profiles_changed);
        self.conf_client.notify_add("/apps/gnome15/driver", self.driver_changed);
            
        # Monitor active application    
        self.session_bus = dbus.SessionBus()
        try :
            self.bamf_matcher = self.session_bus.get_object("org.ayatana.bamf", '/org/ayatana/bamf/matcher')   
            self.session_bus.add_signal_receiver(self.application_changed, dbus_interface="org.ayatana.bamf.matcher", signal_name="ActiveApplicationChanged")
        except:
            print "WARNING: BAMF not available, falling back to WNCK"
            try :                
                import wnck
                wnck.__file__
                gobject.timeout_add(500, self.timeout_callback, self)
            except:
                print "WARNING: Python Wnck not available either, no automatic profile switching"
                
        # Expose Gnome15 functions via DBus
        self.dbus_service = g15dbus.G15DBUSService(self) 
 
    def attempt_connection(self, delay=0.0):
        self.connection_lock.acquire()
        try :
            if self.driver == None:
                self.driver = g15drivermanager.get_driver(self.conf_client, on_close=self.on_driver_close)

            if self.driver.is_connected():
                print "WARN: Attempt to reconnect when already connected."
                return
            
            self.loading_complete = False
            self.first_page = self.conf_client.get_string("/apps/gnome15/last_page")
            
            if delay != 0.0:
                self.reconnect_timer = g15util.schedule("ReconnectTimer", delay, self.attempt_connection)
                return
                            
            try :
                self.driver.connect() 
                
                for control in self.driver.get_controls():
                    print "Watching", "/apps/gnome15/" + control.id
                    self.control_handles.append(self.conf_client.notify_add("/apps/gnome15/" + control.id, self.control_configuration_changed));
                
                self.screen.start()
                if self.splash == None:
                    self.splash = G15Splash(self.screen, self.conf_client)
                else:
                    self.splash.update_splash(0, 100, "Starting up ..")
                self.screen.set_mkey(1)
                self.activate_profile()            
                g15util.schedule("ActivatePlugins", 0.1, self.complete_loading)    
                self.last_error = None
            except Exception as e:
                if self._process_exception(e):
                    raise
        finally:
            self.connection_lock.release()
            
    def get_last_error(self):
        return self.last_error
            
    def should_reconnect(self, exception):
        return isinstance(exception, NotConnectedException) or (len(exception.args) == 2 and isinstance(exception.args[0], int) and exception.args[0] in [ 111, 104 ])
            
    def complete_loading(self):              
        try :            
            self.plugins.activate(self.splash.update_splash) 
            if self.first_page != None:
                page = self.screen.get_page(self.first_page)
                if page:
                    self.screen.raise_page(page)
            self.driver.grab_keyboard(self.key_received)
            self.service_host.clear_attention()
            self.splash.complete()
            self.loading_complete = True
            if self.splash != None:
                self.splash.remove()
                self.splash = None
        except Exception as e:
            if self._process_exception(e):
                raise
            
    def error(self, error_text=None):     
        self.service_host.attention(error_text)

    def on_driver_close(self, retry=True):
        if not self.shutting_down:
            if self.splash != None:
                self.splash.remove()
                self.splash = None
        
            for handle in self.control_handles:
                self.conf_client.notify_remove(handle);
            self.control_handles = []
        
            self.plugins.deactivate()
            if retry:
                self._process_exception(NotConnectedException("Keyboard driver disconnected."))
            else:                
                self.service_host.quit()
        
    def __del__(self):
        if self.plugins.get_active():
            self.plugins.deactivate()
        if self.plugins.get_started():
            self.plugins.destroy()
        del self.key_screen
        del self.driver
        
    '''
    screen listener callbacks
    '''
        
    def title_changed(self, page, title):
        pass
        
    def page_changed(self, page):
        self.resched_cycle()
        
    def new_page(self, page):
        pass
    
    def del_page(self, page):
        pass
            
    def screen_cycle(self):
        page = self.screen.get_visible_page()
        if page != None and page.priority < g15screen.PRI_HIGH:
            self.screen.cycle(1)
        else:
            self.resched_cycle()
        
    def resched_cycle(self, arg1=None, arg2=None, arg3=None, arg4=None):
        self.reschedule_lock.acquire()
        try:
            if self.cycle_timer != None:
                self._cancel_timer()
            self._check_cycle()
        finally:
            self.reschedule_lock.release()
            
    def cycle_level(self, val, control):
        level = self.conf_client.get_int("/apps/gnome15/" + control.id)
        level += val
        if level > control.upper - 1:
            level = control.lower
        if level < control.lower - 1:
            level = control.upper
        self.conf_client.set_int("/apps/gnome15/" + control.id, level)
        
    def cycle_color(self, val, control):
        self.color_no += val
        if self.color_no < 0:
            self.color_no = len(COLOURS) - 1
        if self.color_no >= len(COLOURS):
            self.color_no = 0
        color = COLOURS[self.color_no]
        self.conf_client.set_int("/apps/gnome15/" + control.id + "_red", color[0])
        self.conf_client.set_int("/apps/gnome15/" + control.id + "_green", color[1])
        self.conf_client.set_int("/apps/gnome15/" + control.id + "_blue", color[2])
        
    def driver_changed(self, client, connection_id, entry, args):
        if self.driver == None or self.driver.id != entry.value.get_string():
            if self.driver != None and self.driver.is_connected() :
                self.driver.disconnect()
            else:
                self.driver = g15drivermanager.get_driver(self.conf_client, on_close=self.on_driver_close)
                self.attempt_connection(0.0)
        
    def profiles_changed(self, client, connection_id, entry, args):
        self.screen.set_color_for_mkey()
        
    def key_received(self, keys, state):
        if self.screen.handle_key(keys, state, post=False) or self.plugins.handle_key(keys, state, post=False):
            return        
        
        if state == g15driver.KEY_STATE_UP:
            if g15driver.G_KEY_LIGHT in keys:
                self.keyboard_backlight = self.keyboard_backlight + 1
                if self.keyboard_backlight == 3:
                    self.keyboard_backlight = 0
                self.conf_client.set_int("/apps/gnome15/keyboard_backlight", self.keyboard_backlight)

            profile = g15profile.get_active_profile()
            if profile != None:
                macro = profile.get_macro(self.screen.get_mkey(), keys)
                if macro != None:
                    self.send_macro(macro)
                            
        self.screen.handle_key(keys, state, post=True) or self.plugins.handle_key(keys, state, post=True)
        
    def send_macro(self, macro):
        macros = macro.macro.split("\n")
        for macro_text in macros:
            split = macro_text.split(" ")
            op = split[0]
            if len(split) > 1:
                val = split[1]
                if op == "Delay" and macro.profile.send_delays:
                    time.sleep(float(val) / 1000.0)
                elif op == "Press":
                    self.send_string(val, True)
                elif op == "Release":
                    self.send_string(val, False)
    
    def get_keysym(self, ch) :
        keysym = Xlib.XK.string_to_keysym(ch)
        if keysym == 0 :
            # Unfortunately, although this works to get the correct keysym
            # i.e. keysym for '#' is returned as "numbersign"
            # the subsequent display.keysym_to_keycode("numbersign") is 0.
            keysym = Xlib.XK.string_to_keysym(special_X_keysyms[ch])
        return keysym
                
    def char_to_keycode(self, ch) :
        keysym = self.get_keysym(ch)
        keycode = local_dpy.keysym_to_keycode(keysym)
        if keycode == 0 :
            print "Sorry, can't map", ch
    
        if False:
            shift_mask = Xlib.X.ShiftMask
        else :
            shift_mask = 0
    
        return keycode, shift_mask

            
    def send_string(self, ch, press) :
        keycode, shift_mask = self.char_to_keycode(ch)
        if (UseXTest) :
            if press:
                if shift_mask != 0 :
                    Xlib.ext.xtest.fake_input(local_dpy, Xlib.X.KeyPress, 50)
                Xlib.ext.xtest.fake_input(local_dpy, Xlib.X.KeyPress, keycode)
            else:
                Xlib.ext.xtest.fake_input(local_dpy, Xlib.X.KeyRelease, keycode)
                if shift_mask != 0 :
                    Xlib.ext.xtest.fake_input(local_dpy, Xlib.X.KeyRelease, 50)
                
            
        else :
            if press:
                event = Xlib.protocol.event.KeyPress(
                                                         time=int(time.time()),
                                                         root=local_dpy.screen().root,
                                                         window=window,
                                                         same_screen=0, child=Xlib.X.NONE,
                                                         root_x=0, root_y=0, event_x=0, event_y=0,
                                                         state=shift_mask,
                                                         detail=keycode
                                                         )
                window.send_event(event, propagate=True)
            else:
                event = Xlib.protocol.event.KeyRelease(
                                                           time=int(time.time()),
                                                           root=local_dpy.screen().root,
                                                           window=window,
                                                           same_screen=0, child=Xlib.X.NONE,
                                                           root_x=0, root_y=0, event_x=0, event_y=0,
                                                           state=shift_mask,
                                                           detail=keycode
                    )
                window.send_event(event, propagate=True)
                
        local_dpy.sync() 
             
    def control_changed(self, client, connection_id, entry, args):
        self.driver.set_controls_from_configuration(client)
        
    def control_configuration_changed(self, client, connection_id, entry, args):
        key = os.path.basename(entry.key)
        print "Controls changed", key
        if self.driver != None:
            for control in self.driver.get_controls():
                if key == control.id:
                    if isinstance(control.value, int):
                        control.value = entry.value.get_int()
                    else:
                        rgb = entry.value.get_string().split(",")
                        control.value = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
                        
                    self.driver.update_control(control)
                    
                    break
            self.screen.redraw()
        
    def set_defeat_profile_change(self, defeat):
        self.defeat_profile_change = defeat
        
    def application_changed(self, old, object_name):
        if object_name != "":
            app = self.session_bus.get_object("org.ayatana.bamf", object_name)
            view = dbus.Interface(app, 'org.ayatana.bamf.view')
            try :
                if view.IsActive() == 1 and not self.defeat_profile_change:
                    choose_profile = None
                    title = view.Name()                                    
                    # Active window has changed, see if we have a profile that matches it
                    for profile in g15profile.get_profiles():
                        if not profile.get_default() and profile.activate_on_focus and len(profile.window_name) > 0 and title.lower().find(profile.window_name.lower()) != -1:
                            choose_profile = profile 
                            break
                        
                    # No applicable profile found. Look for a default profile, and see if it is set to activate by default
                    active_profile = g15profile.get_active_profile()
                    if choose_profile == None:
                        default_profile = g15profile.get_default_profile()
                        
                        if (active_profile == None or active_profile.id != default_profile.id) and default_profile.activate_on_focus:
                            default_profile.make_active()
                    elif active_profile == None or choose_profile.id != active_profile.id:
                        choose_profile.make_active()
            except dbus.DBusException:
                pass
        
    def timeout_callback(self, event=None):
        try:
            if not self.defeat_profile_change:
                import wnck
                window = wnck.screen_get_default().get_active_window()
                choose_profile = None
                if window != None:
                    title = window.get_name()                                    
                    for profile in g15profile.get_profiles():
                        if not profile.get_default() and profile.activate_on_focus and len(profile.window_name) > 0 and title.lower().find(profile.window_name.lower()) != -1:
                            choose_profile = profile 
                            break
                            
                active_profile = g15profile.get_active_profile()
                if choose_profile == None:
                    default_profile = g15profile.get_default_profile()
                    if (active_profile == None or active_profile.id != default_profile.id) and default_profile.activate_on_focus:
                        default_profile.make_active()
                elif active_profile == None or choose_profile.id != active_profile.id:
                    choose_profile.make_active()
            
        except Exception as exception:
            traceback.print_exc(file=sys.stdout)
            print "Failed to activate profile for active window"
            print type(exception)
            
        gobject.timeout_add(500, self.timeout_callback, self)
        
    def active_profile_changed(self, client, connection_id, entry, args):
        # Check if the active profile has change
        new_profile = g15profile.get_active_profile()
        if new_profile == None:
            self.deactivate_profile()
        else:
            self.activate_profile()
                
        return 1

    def activate_profile(self):
        if self.screen.driver.is_connected():
            self.screen.set_mkey(1)
    
    def deactivate_profile(self):
        if self.screen.driver.is_connected():
            self.screen.set_mkey(0)
        
    def cleanup(self, event):
        self.shutting_down = True
        if self.driver.is_connected():
            self.driver.disconnect()  
        
    def properties(self, event, data=None):
        g15util.run_script("g15-config")
        
    def about_info(self, event, data=None):  
        about = gnome.ui.About("Gnome15", pglobals.version, "GPL", \
                               "GNOME Applet providing integration with\nthe Logitech G15 and G19 keyboards.", ["Brett Smith <tanktarta@blueyonder.co.uk>"], \
                               ["Brett Smith <tanktarta@blueyonder.co.uk>"], "Brett Smith <tanktarta@blueyonder.co.uk>", gtk.gdk.pixbuf_new_from_file(g15util.get_app_icon(self.conf_client, "gnome15", 128)))
        about.show()
        
    '''
    Private
    '''    
    def _check_cycle(self, client=None, connection_id=None, entry=None, args=None):  
        self.reschedule_lock.acquire()
        try:      
            cycle_screens = self.conf_client.get_bool("/apps/gnome15/cycle_screens")
            active = self.driver != None and self.driver.is_connected() and cycle_screens
            if active and self.cycle_timer == None:
                val = self.conf_client.get("/apps/gnome15/cycle_seconds")
                time = 10
                if val != None:
                    time = val.get_int()
                self.cycle_timer = g15util.schedule("CycleTimer", time, self.screen_cycle)
            elif not active and self.cycle_timer != None:
                self._cancel_timer()
        finally:
            self.reschedule_lock.release()
            
    def _cancel_timer(self):
        self.reschedule_lock.acquire()
        try:      
            self.cycle_timer.cancel()
            self.cycle_timer = None  
        finally:
            self.reschedule_lock.release()          
            
    def _process_exception(self, exception):
        self.last_error = exception
        self.service_host.attention(str(exception))
        self.resched_cycle()   
        self.driver = None          
        if self.should_reconnect(exception):
            traceback.print_exc(file=sys.stderr)
            self.reconnect_timer = g15util.schedule("ReconnectTimer", 5.0, self.attempt_connection)
        else:
            traceback.print_exc(file=sys.stderr)
            return True
