#
# Copyright (C) 2012  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
# Red Hat Author(s): Martin Gracik <mgracik@redhat.com>
#                    Vratislav Podzimek <vpodzime@redhat.com>
#

"""
This module include functions and classes for dealing with multiple layouts
in Anaconda. It wraps the libxklavier functionality to protect Anaconda
from dealing with its "nice" API that looks like a Lisp-influenced
"good old C".

It provides a XklWrapper class with several methods that can be used
for listing and various modifications of keyboard layouts settings.

"""

import os
import dbus
from pyanaconda import iutil

# pylint: disable-msg=E0611
from gi.repository import Xkl

import logging
log = logging.getLogger("anaconda")

LOCALED_SERVICE = "org.freedesktop.locale1"
LOCALED_OBJECT_PATH = "/org/freedesktop/locale1"
LOCALED_IFACE = "org.freedesktop.locale1"

class KeyboardConfigError(Exception):
    """Exception class for keyboard configuration related problems"""

    pass

def _parse_layout_variant(layout):
    """
    Parse layout and variant from the string that may look like 'layout' or
    'layout (variant)'.

    @return: the (layout, variant) pair, where variant can be ""
    @rtype: tuple

    """

    variant = ""

    lbracket_idx = layout.find("(")
    rbracket_idx = layout.rfind(")")
    if lbracket_idx != -1:
        variant = layout[(lbracket_idx + 1) : rbracket_idx]
        layout = layout[:lbracket_idx].strip()

    return (layout, variant)

def _join_layout_variant(layout, variant=""):
    """
    Join layout and variant to form the commonly used 'layout (variant)'
    or 'layout' (if variant is missing) format.

    @type layout: string
    @type variant: string
    @return: 'layout (variant)' or 'layout' string
    @rtype: string

    """

    if variant:
        return "%s (%s)" % (layout, variant)
    else:
        return layout

def get_layouts_xorg_conf(keyboard):
    """
    Get the xorg.conf content setting up layouts in the ksdata.

    @param keyboard: ksdata.keyboard object
    @rtype: str

    """

    layouts = list()
    variants = list()

    for layout_variant in keyboard.x_layouts:
        (layout, variant) = _parse_layout_variant(layout_variant)
        layouts.append(layout)
        variants.append(variant)

    ret = "#This file was generated by the Anaconda installer\n"

    #section header
    ret += 'Section "InputClass"\n'\
           '\tIdentifier\t"anaconda-keyboard"\n'\
           '\tMatchIsKeyboard\t"on"\n'

    #layouts
    ret += '\tOption\t"XkbLayout"\t'
    ret += '"' + ','.join(layouts) + '"\n'

    #variants
    if any(variants):
        #write out this line only if some variants are specified
        ret += '\tOption\t"XkbVariant"\t'
        ret += '"' + ','.join(variants) + '"\n'

    #switching
    if any(keyboard.switch_options):
        ret += '\tOption\t"XkbOptions"\t'
        ret += '"' + ','.join(keyboard.switch_options) + '"\n'

    #section footer
    ret += 'EndSection'

    return ret

def write_keyboard_config(keyboard, root, convert=True, weight=0):
    """
    Function that writes files with layouts configuration to
    $root/etc/X11/xorg.conf.d/01-anaconda-layouts.conf,
    $root/etc/sysconfig/keyboard and $root/etc/vconsole.conf.

    @param keyboard: ksdata.keyboard object
    @param root: path to the root of the installed system
    @param convert: whether to convert specified values to get the missing
                    ones
    @param weight: weight (prefix) of the xorg.conf file written out

    """

    localed = LocaledWrapper()

    if convert:
        # populate vc_keymap and x_layouts if they are missing
        if keyboard.x_layouts and not keyboard.vc_keymap:
            keyboard.vc_keymap = \
                        localed.set_and_convert_layout(keyboard.x_layouts[0])

        if not keyboard.vc_keymap:
            keyboard.vc_keymap = "us"

        if not keyboard.x_layouts:
            c_lay_var = localed.set_and_convert_keymap(keyboard.vc_keymap)
            keyboard.x_layouts.append(c_lay_var)

    xconf_dir = os.path.normpath(root + "/etc/X11/xorg.conf.d")
    xconf_file = "%0.2d-anaconda-keyboard.conf" % weight

    sysconf_dir = os.path.normpath(root + "/etc/sysconfig")
    sysconf_file = "keyboard"

    vcconf_dir = os.path.normpath(root + "/etc")
    vcconf_file = "vconsole.conf"

    errors = []

    try:
        if not os.path.isdir(xconf_dir):
            os.makedirs(xconf_dir)

    except OSError as oserr:
        errors.append("Cannot create directory xorg.conf.d")

    if keyboard.x_layouts:
        try:
            with open(os.path.join(xconf_dir, xconf_file), "w") as fobj:
                fobj.write(get_layouts_xorg_conf(keyboard))
        except IOError as ioerr:
            errors.append("Cannot write X keyboard configuration file")

    if keyboard.vc_keymap:
        try:
            with open(os.path.join(sysconf_dir, sysconf_file), "w") as fobj:
                fobj.write('KEYMAP="%s"\n' % keyboard.vc_keymap)

        except IOError as ioerr:
            errors.append("Cannot write sysconfig keyboard configuration file")

        try:
            with open(os.path.join(vcconf_dir, vcconf_file), "w") as fobj:
                fobj.write('KEYMAP="%s"\n' % keyboard.vc_keymap)
        except IOError as ioerr:
            errors.append("Cannot write vconsole configuration file")

    if errors:
        raise KeyboardConfigError("\n".join(errors))

def _try_to_load_keymap(keymap):
    """
    Method that tries to load keymap and returns boolean indicating if it was
    successfull or not. It can be used to test if given string is VConsole
    keymap or not, but in case it is given valid keymap, IT REALLY LOADS IT!.

    @type keymap: string
    @raise KeyboardConfigError: if loadkeys command is not available
    @return: True if given string was a valid keymap and thus was loaded,
             False otherwise

    """

    # BUG: systemd-localed should be able to tell us if we are trying to
    #      activate invalid keymap. Then we will be able to get rid of this
    #      fuction

    ret = 0

    try:
        ret = iutil.execWithRedirect("loadkeys", [keymap], stdout="/dev/tty5",
                                     stderr="/dev/tty5")
    except OSError as oserr:
        msg = "'loadkeys' command not available (%s)" % oserr.strerror
        raise KeyboardConfigError(msg)

    return ret == 0

def activate_keyboard(keyboard):
    """
    Try to setup VConsole keymap and X11 layouts as specified in kickstart.

    @param keyboard: ksdata.keyboard object
    @type keyboard: ksdata.keyboard object

    """

    localed = LocaledWrapper()
    c_lay_var = ""
    c_keymap = ""

    if keyboard._keyboard and not (keyboard.vc_keymap or keyboard.x_layouts):
        # we were give only one value in old format of the keyboard command
        # try to guess if we were given VConsole keymap or X11 layout
        is_keymap = _try_to_load_keymap(keyboard._keyboard)

        if is_keymap:
            keyboard.vc_keymap = keyboard._keyboard
        else:
            keyboard.x_layouts.append(keyboard._keyboard)

    if keyboard.vc_keymap:
        valid_keymap = _try_to_load_keymap(keyboard.vc_keymap)
        if not valid_keymap:
            log.error("'%s' is not a valid VConsole keymap, not loading" % \
                        keyboard.vc_keymap)
        else:
            # activate VConsole keymap and get converted layout and variant
            c_lay_var = localed.set_and_convert_keymap(keyboard.vc_keymap)

    if not keyboard.x_layouts and c_lay_var:
        keyboard.x_layouts.append(c_lay_var)

    if keyboard.x_layouts:
        c_keymap = localed.set_and_convert_layout(keyboard.x_layouts[0])

        if not keyboard.vc_keymap:
            keyboard.vc_keymap = c_keymap

        # write out full configuration that will be loaded by X server
        # (systemd-localed writes configuration with only one layout)
        write_keyboard_config(keyboard, root="/", convert=False, weight=99)

def item_str(s):
    """Convert a zero-terminated byte array to a proper str"""

    i = s.find(b'\x00')
    return s[:i].decode("utf-8") #there are some non-ascii layout descriptions

class _Layout(object):
    """Internal class representing a single layout variant"""

    def __init__(self, name, desc):
        self.name = name
        self.desc = desc

    def __str__(self):
        return '%s (%s)' % (self.name, self.desc)

    def __eq__(self, obj):
        return isinstance(obj, self.__class__) and \
            self.name == obj.name

    @property
    def description(self):
        return self.desc

class XklWrapperError(KeyboardConfigError):
    """Exception class for reporting libxklavier-related problems"""

    pass

class XklWrapper(object):
    """
    Class wrapping the libxklavier functionality

    Use this class as a singleton class because it provides read-only data
    and initialization (that takes quite a lot of time) reads always the
    same data. It doesn't have sense to make multiple instances

    """

    _instance = None

    @staticmethod
    def get_instance():
        if not XklWrapper._instance:
            XklWrapper._instance = XklWrapper()

        return XklWrapper._instance

    def __init__(self):
        # pylint: disable-msg=E0611
        from gi.repository import GdkX11

        #initialize Xkl-related stuff
        display = GdkX11.x11_get_default_xdisplay()
        self._engine = Xkl.Engine.get_instance(display)

        self._rec = Xkl.ConfigRec()
        if not self._rec.get_from_server(self._engine):
            raise XklWrapperError("Failed to get configuration from server")

        #X is probably initialized to the 'us' layout without any variant and
        #since we want to add layouts with variants we need the layouts and
        #variants lists to have the same length. Add "" padding to variants.
        #See docstring of the add_layout method for details.
        diff = len(self._rec.layouts) - len(self._rec.variants)
        if diff > 0:
            self._rec.set_variants(self._rec.variants + (diff * [""]))
            if not self._rec.activate(self._engine):
                raise XklWrapperError("Failed to initialize layouts")

        #needed also for Gkbd.KeyboardDrawingDialog
        self.configreg = Xkl.ConfigRegistry.get_instance(self._engine)
        self.configreg.load(False)

        self._language_keyboard_variants = dict()
        self._country_keyboard_variants = dict()
        self._switching_options = list()

        #we want to display layouts as 'language (description)'
        self.name_to_show_str = dict()

        #we want to display layout switching options as e.g. "Alt + Shift" not
        #as "grp:alt_shift_toggle"
        self.switch_to_show_str = dict()

        #this might take quite a long time
        self.configreg.foreach_language(self._get_language_variants, None)
        self.configreg.foreach_country(self._get_country_variants, None)

        #'grp' means that we want layout (group) switching options
        self.configreg.foreach_option('grp', self._get_switch_option, None)

    def _get_variant(self, c_reg, item, subitem, dest):
        if subitem:
            name = item_str(item.name) + " (" + item_str(subitem.name) + ")"
            description = item_str(subitem.description)
        else:
            name = item_str(item.name)
            description = item_str(item.description)

        if dest:
            self.name_to_show_str[name] = "%s (%s)" % (dest.encode("utf-8"),
                                                       description.encode("utf-8"))
        self._variants_list.append(_Layout(name, description))

    def _get_language_variants(self, c_reg, item, user_data=None):
        #helper "global" variable
        self._variants_list = list()
        lang_name, lang_desc = item_str(item.name), item_str(item.description)

        c_reg.foreach_language_variant(lang_name, self._get_variant, lang_desc)

        self._language_keyboard_variants[lang_desc] = self._variants_list

    def _get_country_variants(self, c_reg, item, user_data=None):
        #helper "global" variable
        self._variants_list = list()
        country_name, country_desc = item_str(item.name), item_str(item.description)

        c_reg.foreach_country_variant(country_name, self._get_variant, None)

        self._country_keyboard_variants[country_name] = self._variants_list

    def _get_switch_option(self, c_reg, item, user_data=None):
        """Helper function storing layout switching options in foreach cycle"""
        desc = item_str(item.description)
        name = item_str(item.name)

        self._switching_options.append(name)
        self.switch_to_show_str[name] = desc.encode("utf-8")

    def get_available_layouts(self):
        """A generator yielding layouts (no need to store them as a bunch)"""

        for lang_desc, variants in sorted(self._language_keyboard_variants.items()):
            for layout in variants:
                yield layout.name

    def get_switching_options(self):
        """Method returning list of available layout switching options"""

        return self._switching_options

    def get_default_language_layout(self, language):
        """Get the default layout for a given language"""

        language_layouts = self._language_keyboard_variants.get(language, None)

        if not language_layouts:
            return None

        #first layout (should exist for every language)
        return language_layouts[0].name

    def get_default_lang_country_layout(self, language, country):
        """
        Get default layout matching both language and country. If none such
        layout is found, get default layout for language.

        """

        language_layouts = self._language_keyboard_variants.get(language, None)
        country_layouts = self._country_keyboard_variants.get(country, None)
        if not language_layouts:
            return None

        matches_both = (layout for layout in language_layouts
                                if layout in country_layouts)

        try:
            return matches_both.next().name
        except StopIteration:
            return language_layouts[0].name

    def get_current_layout_name(self):
        """
        Get current activated X layout's name

        @return: current activated X layout's name (e.g. "Czech (qwerty)")

        """

        self._engine.start_listen(Xkl.EngineListenModes.TRACK_KEYBOARD_STATE)
        state = self._engine.get_current_state()
        groups_names = self._engine.get_groups_names()
        self._engine.stop_listen(Xkl.EngineListenModes.TRACK_KEYBOARD_STATE)

        return groups_names[state.group]

    def is_valid_layout(self, layout):
        """Return if given layout is valid layout or not"""

        return layout in self.name_to_show_str

    def add_layout(self, layout):
        """
        Method that tries to add a given layout to the current X configuration.

        The X layouts configuration is handled by two lists. A list of layouts
        and a list of variants. Index-matching items in these lists (as if they
        were zipped) are used for the construction of real layouts (e.g.
        'cz (qwerty)').

        @param layout: either 'layout' or 'layout (variant)'
        @raise XklWrapperError: if the given layout cannot be added

        """

        #we can get 'layout' or 'layout (variant)'
        (layout, variant) = _parse_layout_variant(layout)

        #do not add the same layout-variant combinanion multiple times
        if (layout, variant) in zip(self._rec.layouts, self._rec.variants):
            return

        self._rec.set_layouts(self._rec.layouts + [layout])
        self._rec.set_variants(self._rec.variants + [variant])

        if not self._rec.activate(self._engine):
            raise XklWrapperError("Failed to add layout '%s (%s)'" % (layout,
                                                                      variant))

    def remove_layout(self, layout):
        """
        Method that tries to remove a given layout from the current X
        configuration.

        See also the documentation for the add_layout method.

        @param layout: either 'layout' or 'layout (variant)'
        @raise XklWrapperError: if the given layout cannot be removed

        """

        #we can get 'layout' or 'layout (variant)'
        (layout, variant) = _parse_layout_variant(layout)

        layouts_variants = zip(self._rec.layouts, self._rec.variants)

        if not (layout, variant) in layouts_variants:
            msg = "'%s (%s)' not in the list of added layouts" % (layout,
                                                                  variant)
            raise XklWrapperError(msg)

        idx = layouts_variants.index((layout, variant))
        new_layouts = self._rec.layouts[:idx] + self._rec.layouts[(idx + 1):]
        new_variants = self._rec.variants[:idx] + self._rec.variants[(idx + 1):]

        self._rec.set_layouts(new_layouts)
        self._rec.set_variants(new_variants)

        if not self._rec.activate(self._engine):
            raise XklWrapperError("Failed to remove layout '%s (%s)'" % (layout,
                                                                       variant))
    def replace_layouts(self, layouts_list):
        """
        Method that replaces the layouts defined in the current X configuration
        with the new ones given.

        @param layouts_list: list of layouts defined as either 'layout' or
                             'layout (variant)'
        @raise XklWrapperError: if layouts cannot be replaced with the new ones

        """

        new_layouts = list()
        new_variants = list()

        for layout_variant in layouts_list:
            (layout, variant) = _parse_layout_variant(layout_variant)
            new_layouts.append(layout)
            new_variants.append(variant)

        self._rec.set_layouts(new_layouts)
        self._rec.set_variants(new_variants)

        if not self._rec.activate(self._engine):
            msg = "Failed to replace layouts with: %s" % ",".join(layouts_list)
            raise XklWrapperError(msg)

    def set_switching_options(self, options):
        """
        Method that sets options for layout switching. It replaces the old
        options with the new ones.

        @param options: layout switching options to be set
        @type options: list or generator
        @raise XklWrapperError: if the old options cannot be replaced with the
                                new ones

        """

        #preserve old "non-switching options"
        new_options = [opt for opt in self._rec.options if "grp:" not in opt]
        new_options += options

        self._rec.set_options(new_options)

        if not self._rec.activate(self._engine):
            msg = "Failed to set switching options to: %s" % ",".join(options)
            raise XklWrapperError(msg)

class LocaledWrapperError(KeyboardConfigError):
    """Exception class for reporting Localed-related problems"""
    pass

class LocaledWrapper(object):
    """
    Class wrapping systemd-localed daemon functionality.

    """

    def __init__(self):
        bus = dbus.SystemBus()

        try:
            localed = bus.get_object(LOCALED_SERVICE, LOCALED_OBJECT_PATH)
        except dbus.DBusException:
            raise LocaledWrapperError("Failed to get locale object")

        try:
            self._locale_iface = dbus.Interface(localed, LOCALED_IFACE)
        except dbus.DBusException:
            raise LocaledWrapperError("Failed to get locale interface")

        try:
            self._props_iface = dbus.Interface(localed, dbus.PROPERTIES_IFACE)
        except dbus.DBusException:
            raise LocaledWrapperError("Failed to get properties interface")

    def set_and_convert_keymap(self, keymap):
        """
        Method that sets VConsole keymap and returns X11 layout and
        variant that (systemd-localed thinks) match given keymap best.

        @return: string containing "layout (variant)" or "layout" if variant
                 is missing
        @rtype: string

        """

        # args: keymap, keymap_toggle, convert, user_interaction
        # where convert indicates whether the keymap should be converted
        # to X11 layout and user_interaction indicates whether PolicyKit
        # should ask for credentials or not
        try:
            self._locale_iface.SetVConsoleKeyboard(keymap, "", True, False)
        except dbus.DBusException:
            msg = "Failed to call SetVConsoleKeyboard method"
            raise LocaledWrapperError(msg)

        try:
            layout = self._props_iface.Get(LOCALED_IFACE, "X11Layout")
        except dbus.DBusException:
            raise LocaledWrapperError("locale has no X11Layout property")

        try:
            variant = self._props_iface.Get(LOCALED_IFACE, "X11Variant")
        except dbus.DBusException:
            raise LocaledWrapperError("locale has no X11Variant property")

        return _join_layout_variant(layout, variant)

    def set_and_convert_layout(self, layout_variant):
        """
        Method that sets X11 layout and variant (for later X sessions)
        and returns VConsole keymap that (systemd-localed thinks) matches
        given layout and variant best.

        @return: a keymap matching layout and variant best
        @rtype: string

        """

        (layout, variant) = _parse_layout_variant(layout_variant)

        # args: layout, model, variant, options, convert, user_interaction
        # where convert indicates whether the keymap should be converted
        # to X11 layout and user_interaction indicates whether PolicyKit
        # should ask for credentials or not
        try:
            self._locale_iface.SetX11Keyboard(layout, "", variant, "", True, False)
        except dbus.DBusException:
            msg = "Failed to call SetX11Keyboard method"
            raise LocaledWrapperError(msg)

        try:
            return self._props_iface.Get(LOCALED_IFACE, "VConsoleKeymap")
        except dbus.DBusException:
            raise LocaledWrapperError("locale has no VConsoleKeymap property")