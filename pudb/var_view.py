# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function

__copyright__ = """
Copyright (C) 2009-2017 Andreas Kloeckner
Copyright (C) 2014-2017 Aaron Meurer
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


# {{{ constants and imports

import urwid
import inspect
import warnings

from pudb.lowlevel import ui_log
from pudb.py3compat import _PudbCollection, _PudbMapping, _PudbSequence

try:
    import numpy
    HAVE_NUMPY = 1
except ImportError:
    HAVE_NUMPY = 0

from pudb.py3compat import PY3, execfile, raw_input, xrange, \
        integer_types, string_types, text_type
if PY3:
    ELLIPSIS = '…'
else:
    ELLIPSIS = unicode('…', 'utf-8')  # noqa: F821

from pudb.ui_tools import text_width

# }}}


# {{{ container metaclasses

class PudbCollection(_PudbCollection):
    @classmethod
    def __subclasshook__(cls, c):
        if cls is PudbCollection:
            try:
                return all([
                    any("__contains__" in b.__dict__ for b in c.__mro__),
                    any("__iter__" in b.__dict__ for b in c.__mro__),
                ])
            except Exception:
                pass
        return NotImplemented

    @classmethod
    def entries(cls, collection):
        """
        :yield: (label, entry, id_path_ext) tuples for each entry in the
        collection.
        """
        assert isinstance(collection, cls)
        for count, entry in enumerate(collection):
            yield None, entry, "[%d]" % count


class PudbSequence(_PudbSequence):
    @classmethod
    def __subclasshook__(cls, c):
        if cls is PudbSequence:
            try:
                return all([
                    any("__getitem__" in b.__dict__ for b in c.__mro__),
                    any("__iter__" in b.__dict__ for b in c.__mro__),
                ])
            except Exception:
                pass
        return NotImplemented

    @classmethod
    def entries(cls, sequence):
        """
        :yield: (label, entry, id_path_ext) tuples for each entry in the
        sequence.
        """
        assert isinstance(sequence, cls)
        for count, entry in enumerate(sequence):
            yield str(count), entry, "[%d]" % count


class PudbMapping(_PudbMapping):
    @classmethod
    def __subclasshook__(cls, c):
        if cls is PudbMapping:
            try:
                return all([
                    any("__getitem__" in b.__dict__ for b in c.__mro__),
                    any("__iter__" in b.__dict__ for b in c.__mro__),
                    any("keys" in b.__dict__ for b in c.__mro__),
                ])
            except Exception:
                pass
        return NotImplemented

    @classmethod
    def entries(cls, mapping):
        """
        :yield: (label, entry, id_path_ext) tuples for each entry in the
        mapping.
        """
        assert isinstance(mapping, cls)
        for key in mapping.keys():
            try:
                entry = mapping[key]
            except TypeError:
                # Some kind of implementation error
                ui_log.error("Object '%r' appears to be a mapping, but does "
                             "not behave like one." % mapping)
            else:
                yield repr(key), entry, "[%r]" % key


# Order is important here- A mapping without keys could be viewed as a
# sequence, and they're both collections.
CONTAINER_CLASSES = [
    PudbMapping,
    PudbSequence,
    PudbCollection,
]

# }}}

# {{{ data

class FrameVarInfo(object):
    def __init__(self):
        self.id_path_to_iinfo = {}
        self.watches = []

    def get_inspect_info(self, id_path, read_only):
        if read_only:
            return self.id_path_to_iinfo.get(
                    id_path, InspectInfo())
        else:
            return self.id_path_to_iinfo.setdefault(
                    id_path, InspectInfo())


class InspectInfo(object):
    def __init__(self):
        # Do not globalize: cyclic import
        from pudb.debugger import CONFIG

        self.show_detail = False
        self.display_type = CONFIG["stringifier"]
        self.highlighted = False
        self.repeated_at_top = False
        self.access_level = CONFIG["default_variables_access_level"]
        self.show_methods = False
        self.wrap = CONFIG["wrap_variables"]


class WatchExpression(object):
    def __init__(self, expression):
        self.expression = expression


class WatchEvalError(object):
    def __str__(self):
        return "<error>"

# }}}


# {{{ safe types

def get_str_safe_types():
    import types

    return tuple(getattr(types, s) for s in
        "BuiltinFunctionType BuiltinMethodType  ClassType "
        "CodeType FileType FrameType FunctionType GetSetDescriptorType "
        "LambdaType MemberDescriptorType MethodType ModuleType "
        "SliceType TypeType TracebackType UnboundMethodType XRangeType".split()
        if hasattr(types, s)) + (WatchEvalError,)


STR_SAFE_TYPES = get_str_safe_types()

# }}}


# {{{ widget

class VariableWidget(urwid.FlowWidget):
    PREFIX = "| "

    def __init__(self, parent, var_label, value_str, id_path,
            attr_prefix=None, watch_expr=None, iinfo=None):
        assert isinstance(id_path, str)
        self.parent = parent
        self.nesting_level = 0 if parent is None else parent.nesting_level + 1
        self.prefix = self.PREFIX * self.nesting_level
        self.var_label = var_label
        self.value_str = value_str
        self.id_path = id_path
        self.attr_prefix = attr_prefix or "var"
        self.watch_expr = watch_expr
        if iinfo is None:
            # Do not globalize: cyclic import
            from pudb.debugger import CONFIG

            self.wrap = CONFIG["wrap_variables"]
        else:
            self.wrap = iinfo.wrap

    def __str__(self):
        return ('VariableWidget: {value_str}, level {nesting_level}, at {id_path}'
                .format(
                    value_str=self.value_str,
                    nesting_level=self.nesting_level,
                    id_path=self.id_path,
                ))

    def selectable(self):
        return True

    SIZE_LIMIT = 20

    def _get_text(self, size):
        maxcol = size[0] - len(self.prefix)  # self.prefix is a padding
        var_label = self.var_label or ''
        value_str = self.value_str or ''
        alltext = var_label + ": " + value_str
        # The first line is not indented
        firstline = self.prefix + alltext[:maxcol]
        if not alltext[maxcol:]:
            return [firstline]
        fulllines, rest = divmod(text_width(alltext) - maxcol, maxcol - 2)
        restlines = [alltext[(maxcol - 2)*i + maxcol:(maxcol - 2)*i + 2*maxcol - 2]
            for i in xrange(fulllines + bool(rest))]
        return [firstline] + ["  " + self.prefix + i for i in restlines]

    def rows(self, size, focus=False):
        if self.wrap:
            return len(self._get_text(size))

        if (self.value_str is not None
                and self.var_label is not None
                and len(self.prefix) + text_width(self.var_label) > self.SIZE_LIMIT):
            return 2
        else:
            return 1

    def render(self, size, focus=False):
        from pudb.ui_tools import make_canvas

        maxcol = size[0]
        if focus:
            apfx = "focused "+self.attr_prefix+" "
        else:
            apfx = self.attr_prefix+" "

        var_label = self.var_label or ''

        if self.wrap:
            text = self._get_text(size)

            extralabel_full, extralabel_rem = divmod(
                    text_width(var_label[maxcol:]), maxcol)
            totallen = sum([text_width(i) for i in text])
            labellen = (
                    len(self.prefix)  # Padding of first line

                    + (len(self.prefix) + 2)  # Padding of subsequent lines
                    * (extralabel_full + bool(extralabel_rem))

                    + text_width(var_label)

                    + 2  # for ": "
                    )

            _attr = [(apfx+"label", labellen), (apfx+"value", totallen - labellen)]
            from urwid.util import rle_subseg

            fullcols, rem = divmod(totallen, maxcol)

            attr = [rle_subseg(_attr, i*maxcol, (i + 1)*maxcol)
                for i in xrange(fullcols + bool(rem))]

            return make_canvas(text, attr, maxcol, apfx+"value")

        lprefix = len(self.prefix)

        if self.value_str is not None:
            if self.var_label is not None:
                if len(self.prefix) + text_width(self.var_label) > self.SIZE_LIMIT:
                    # label too long? generate separate value line
                    text = [self.prefix + self.var_label,
                            self.prefix+"  " + self.value_str]

                    attr = [
                        [(apfx+"label", lprefix+text_width(self.var_label))],
                        [(apfx+"value", lprefix+2+text_width(self.value_str))]
                        ]
                else:
                    text = [self.prefix + self.var_label + ": " + self.value_str]

                    attr = [[
                            (apfx+"label", lprefix+text_width(self.var_label)+2),
                            (apfx+"value", text_width(self.value_str)),
                            ]]
            else:
                text = [self.prefix + self.value_str]

                attr = [[
                        (apfx+"label", len(self.prefix)),
                        (apfx+"value", text_width(self.value_str)),
                        ]]
        else:
            text = [self.prefix + self.var_label]

            attr = [[(apfx+"label", lprefix + text_width(self.var_label)), ]]

        # Ellipses to show text was cut off
        #encoding = urwid.util.detected_encoding

        if False:  # encoding[:3] == "UTF":
            # Unicode is supported, use single character ellipsis
            for i in xrange(len(text)):
                if len(text[i]) > maxcol:
                    text[i] = (unicode(text[i][:maxcol-1])  # noqa: F821
                            + ELLIPSIS + unicode(text[i][maxcol:]))  # noqa: F821
                    # XXX: This doesn't work.  It just gives a ?
                    # Strangely, the following does work (it gives the …
                    # three characters from the right):
                    #
                    # text[i] = (unicode(text[i][:maxcol-3])
                    # + unicode(u'…')) + unicode(text[i][maxcol-2:])
        else:
            for i in xrange(len(text)):
                if text_width(text[i]) > maxcol:
                    text[i] = text[i][:maxcol-3] + "..."

        return make_canvas(text, attr, maxcol, apfx+"value")

    def keypress(self, size, key):
        return key

# }}}


custom_stringifier_dict = {}


def type_stringifier(value):
    if HAVE_NUMPY and isinstance(value, numpy.ndarray):
        return text_type("%s(%s) %s") % (
                type(value).__name__, value.dtype, value.shape)

    elif HAVE_NUMPY and isinstance(value, numpy.number):
        return text_type("%s (%s)" % (value, value.dtype))

    elif isinstance(value, STR_SAFE_TYPES):
        try:
            return text_type(value)
        except Exception:
            message = 'string safe type stringifier failed'
            ui_log.exception(message)
            return '!! %s !!' % message

    elif hasattr(type(value), "safely_stringify_for_pudb"):
        try:
            # (E.g.) Mock objects will pretend to have this
            # and return nonsense.
            result = value.safely_stringify_for_pudb()
        except Exception:
            message = 'safely_stringify_for_pudb call failed'
            ui_log.exception(message)
            result = '!! %s !!' % message

        if isinstance(result, string_types):
            return text_type(result)

    elif type(value) in [set, frozenset, list, tuple, dict]:
        return text_type("%s (%s)") % (type(value).__name__, len(value))

    return text_type(type(value).__name__)


def get_stringifier(iinfo):
    """Return a function that turns an object into a Unicode text object."""

    if iinfo.display_type == "type":
        return type_stringifier
    elif iinfo.display_type == "repr":
        if PY3:
            return repr
        else:
            return lambda value: repr(value).decode("utf-8")
    elif iinfo.display_type == "str":
        if PY3:
            return str
        else:
            return lambda value: (
                    value.decode("utf-8") if isinstance(value, bytes)
                    else text_type(value))
    else:
        try:
            if not custom_stringifier_dict:  # Only execfile once
                from os.path import expanduser
                execfile(expanduser(iinfo.display_type), custom_stringifier_dict)
        except Exception:
            print("Error when importing custom stringifier:")
            from traceback import print_exc
            print_exc()
            raw_input("Hit enter:")
            return lambda value: text_type("ERROR: Invalid custom stringifier file.")
        else:
            if "pudb_stringifier" not in custom_stringifier_dict:
                print("%s does not contain a function named pudb_stringifier at "
                      "the module level." % iinfo.display_type)
                raw_input("Hit enter:")
                return lambda value: text_type(
                        "ERROR: Invalid custom stringifier file: "
                        "pudb_stringifer not defined.")
            else:
                return (lambda value:
                    text_type(custom_stringifier_dict["pudb_stringifier"](value)))


# {{{ tree walking

class ValueWalker:
    BASIC_TYPES = []
    BASIC_TYPES.append(type(None))
    BASIC_TYPES.extend(integer_types)
    BASIC_TYPES.extend(string_types)
    BASIC_TYPES.extend((float, complex))
    BASIC_TYPES = tuple(BASIC_TYPES)

    NUM_PREVIEW_ITEMS = 3
    MAX_PREVIEW_ITEM_LEN = 16

    CONTENTS_LABEL = "<contents>"
    EMPTY_LABEL = "<empty>"

    def __init__(self, frame_var_info):
        self.frame_var_info = frame_var_info

    def add_continuation_item(self, parent, id_path, count):
        """
        :param VariableWidget or None parent
        :param str id_path:
        :param int count:
        :returns: True if a continuation item ("...") was added, else False
        :rtype: bool
        """
        cont_id_path = "%s.cont-%d" % (id_path, count)
        if not self.frame_var_info.get_inspect_info(
                cont_id_path, read_only=True).show_detail:
            self.add_item(parent, "...", None, id_path=cont_id_path)
            return True
        return False

    def walk_container(self, parent, label, value, id_path=None):
        """
        :param VariableWidget parent:
        :param str label:
        :param * value:
        :param str id_path:
        """
        try:
            container_cls = next(cls for cls in CONTAINER_CLASSES
                                 if isinstance(value, cls))
        except StopIteration:
            # Not recognized as a container
            return False

        is_empty = True
        for count, (entry_label, entry, id_path_ext) in enumerate(
                container_cls.entries(value)):
            is_empty = False
            if ((count > 0 and count % 10 == 0)
                    and self.add_continuation_item(parent, id_path, count)):
                return True

            entry_id_path = "%s%s" % (id_path, id_path_ext)
            self.walk_value(parent, entry_label, entry, entry_id_path)

        if is_empty:
            self.add_item(parent, self.EMPTY_LABEL, None,
                          id_path="%s%s" % (id_path, self.EMPTY_LABEL))

        return True

    @classmethod
    def _preview_entry(cls, entry):
        if len(entry) > cls.MAX_PREVIEW_ITEM_LEN:
            return entry[:cls.MAX_PREVIEW_ITEM_LEN - 3] + "..."
        return entry

    @classmethod
    def preview_contents(cls, container):
        """
        Generates a short preview string made up of the first NUM_PREVIEW_ITEMS
        items in the container.
        """
        # Use this counter along with zip() to limit comprehensions to 3 items.
        counter = range(cls.NUM_PREVIEW_ITEMS)

        # Order is important here- A mapping without keys could be viewed as a
        # sequence, and they're both containers.
        if isinstance(container, PudbMapping):
            items = ["{k}: {v}".format(k=key, v=container[key])
                     for _, key in zip(counter, container.keys())]
            surrounds = ("{", "}")
        elif isinstance(container, PudbSequence):
            items = [str(entry) for _, entry in zip(counter, container)]
            surrounds = ("[", "]")
        elif isinstance(container, PudbCollection):
            items = [str(entry) for _, entry in zip(counter, container)]
            surrounds = ("{", "}")

        preview = "{open}{items}{cont}{close}".format(
            items=", ".join(items),
            cont=", ..." if len(items) == cls.NUM_PREVIEW_ITEMS else "",
            open=surrounds[0],
            close=surrounds[1],
        )

        return preview

    def walk_value(self, parent, label, value, id_path=None, attr_prefix=None):
        if id_path is None:
            id_path = label

        assert isinstance(id_path, str)
        iinfo = self.frame_var_info.get_inspect_info(id_path, read_only=True)

        if isinstance(value, self.BASIC_TYPES):
            displayed_value = repr(value)
        else:
            try:
                displayed_value = get_stringifier(iinfo)(value)
            except Exception:
                # Unfortunately, anything can happen when calling str() or
                # repr() on a random object.
                displayed_value = type_stringifier(value) \
                                + " (!! %s error !!)" % iinfo.display_type
                ui_log.exception('stringifier failed')

        if iinfo.show_detail:
            if iinfo.access_level == "public":
                marker = "pub"
            elif iinfo.access_level == "private":
                marker = "pri"
            else:
                marker = "all"
            if iinfo.show_methods:
                marker += "+()"
            displayed_value += " [%s]" % marker

        new_parent_item = self.add_item(parent, label, displayed_value,
            id_path, attr_prefix)

        if not iinfo.show_detail:
            return

        # containers --------------------------------------------------
        if isinstance(value, (PudbCollection, PudbMapping, PudbSequence)):
            metaitem_id_path = "%s%s" % (id_path, self.CONTENTS_LABEL)
            show_contents = self.frame_var_info.get_inspect_info(
                metaitem_id_path, read_only=True).show_detail

            if show_contents:
                value_str = None
            else:
                try:
                    value_str = self.preview_contents(value)
                except Exception as error:
                    # This almost certainly means that we failed to call str()
                    # on a user's object somewhere. Let's not be too noisy about
                    # that, the lack/failure of a str() method may be intentional
                    value_str = "<error generating preview: {}>".format(error)

            contents_metaitem = self.add_item(
                parent=new_parent_item,
                var_label=self.CONTENTS_LABEL,
                value_str=value_str,
                id_path=metaitem_id_path)
            if show_contents:
                self.walk_container(contents_metaitem, label, value, id_path)

        # general attributes ------------------------------------------
        key_its = []

        try:
            key_its.append(dir(value))
        except Exception:
            ui_log.exception("Failed to look up attributes on {}"
                                .format(label))

        keys = [key
                for ki in key_its
                for key in ki]
        keys.sort()

        cnt_omitted_private = cnt_omitted_methods = 0

        for key in keys:
            if iinfo.access_level == "public":
                if key.startswith("_"):
                    cnt_omitted_private += 1
                    continue
            elif iinfo.access_level == "private":
                if key.startswith("__") and key.endswith("__"):
                    cnt_omitted_private += 1
                    continue

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    attr_value = getattr(value, key)
                if inspect.isroutine(attr_value) and not iinfo.show_methods:
                    cnt_omitted_methods += 1
                    continue
            except Exception:
                attr_value = WatchEvalError()

            self.walk_value(new_parent_item,
                    ".%s" % key, attr_value,
                    "%s.%s" % (id_path, key))

        if not keys:
            if cnt_omitted_private:
                label = "<omitted private attributes>"
            elif cnt_omitted_methods:
                label = "<omitted methods>"
            else:
                label = self.EMPTY_LABEL
            self.add_item(new_parent_item, label,
                          id_path="%s%s" % (id_path, self.EMPTY_LABEL))

        if not key_its:
            self.add_item(new_parent_item, "<?>",
                          id_path="%s%s" % (id_path, self.EMPTY_LABEL))


class BasicValueWalker(ValueWalker):
    def __init__(self, frame_var_info):
        ValueWalker.__init__(self, frame_var_info)

        self.widget_list = []

    def add_item(self, parent, var_label, value_str, id_path, attr_prefix=None):
        iinfo = self.frame_var_info.get_inspect_info(id_path, read_only=True)
        if iinfo.highlighted:
            attr_prefix = "highlighted var"

        new_item = VariableWidget(parent, var_label, value_str, id_path,
            attr_prefix, iinfo=iinfo)
        self.widget_list.append(new_item)
        return new_item


class WatchValueWalker(ValueWalker):
    def __init__(self, frame_var_info, widget_list, watch_expr):
        ValueWalker.__init__(self, frame_var_info)
        self.widget_list = widget_list
        self.watch_expr = watch_expr

    def add_item(self, parent, var_label, value_str, id_path, attr_prefix=None):
        iinfo = self.frame_var_info.get_inspect_info(id_path, read_only=True)
        if iinfo.highlighted:
            attr_prefix = "highlighted var"

        new_item = VariableWidget(parent, var_label, value_str, id_path,
            attr_prefix, watch_expr=self.watch_expr, iinfo=iinfo)
        self.widget_list.append(new_item)
        return new_item


class TopAndMainVariableWalker(ValueWalker):
    def __init__(self, frame_var_info):
        ValueWalker.__init__(self, frame_var_info)

        self.main_widget_list = []
        self.top_widget_list = []

        self.top_id_path_prefixes = []

    @staticmethod
    def _should_repeat_at_top(id_path, tipp):
        """
        :returns: True if the id_path is a child path of tipp
        """
        if id_path is None:
            return False
        if id_path == tipp:
            return True

        # Perhaps it's a child of the top-level path
        before, sep, after = id_path.partition(tipp)
        return (before == '' and
                sep == tipp and
                len(after) > 0 and
                after[0] in '.<[')

    def add_item(self, parent, var_label, value_str, id_path, attr_prefix=None):
        iinfo = self.frame_var_info.get_inspect_info(id_path, read_only=True)
        if iinfo.highlighted:
            attr_prefix = "highlighted var"

        repeated_at_top = iinfo.repeated_at_top
        if repeated_at_top and id_path is not None:
            self.top_id_path_prefixes.append(id_path)

        for tipp in self.top_id_path_prefixes:
            if self._should_repeat_at_top(id_path, tipp):
                repeated_at_top = True

        if repeated_at_top:
            self.top_widget_list.append(VariableWidget(parent, var_label,
                value_str, id_path, attr_prefix, iinfo=iinfo))

        new_item = VariableWidget(parent, var_label, value_str, id_path,
            attr_prefix, iinfo=iinfo)
        self.main_widget_list.append(new_item)
        return new_item

# }}}


# {{{ top level

SEPARATOR = urwid.AttrMap(urwid.Text(""), "variable separator")


def make_var_view(frame_var_info, locals, globals):
    vars = list(locals.keys())
    vars.sort(key=str.lower)

    tmv_walker = TopAndMainVariableWalker(frame_var_info)
    ret_walker = BasicValueWalker(frame_var_info)
    watch_widget_list = []

    for watch_expr in frame_var_info.watches:
        try:
            value = eval(watch_expr.expression, globals, locals)
        except Exception:
            value = WatchEvalError()

        WatchValueWalker(frame_var_info, watch_widget_list, watch_expr) \
                .walk_value(None, watch_expr.expression, value)

    if "__return__" in vars:
        ret_walker.walk_value(None, "Return", locals["__return__"],
                attr_prefix="return")

    for var in vars:
        if not (var.startswith('__') and var.endswith('__')):
            tmv_walker.walk_value(None, var, locals[var])

    result = tmv_walker.main_widget_list

    if watch_widget_list:
        result = (watch_widget_list + [SEPARATOR] + result)

    if tmv_walker.top_widget_list:
        result = (tmv_walker.top_widget_list + [SEPARATOR] + result)

    if ret_walker.widget_list:
        result = (ret_walker.widget_list + result)

    return result


class FrameVarInfoKeeper(object):
    def __init__(self):
        self.frame_var_info = {}

    def get_frame_var_info(self, read_only, ssid=None):
        if ssid is None:
            ssid = self.debugger.get_stack_situation_id()
        if read_only:
            return self.frame_var_info.get(ssid, FrameVarInfo())
        else:
            return self.frame_var_info.setdefault(ssid, FrameVarInfo())

# }}}

# vim: foldmethod=marker
