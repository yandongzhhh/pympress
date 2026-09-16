# -*- coding: utf-8 -*-
"""Thread-safe page cache with a single background Poppler renderer."""
import collections
import logging
import threading

import cairo
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib

from pympress.renderworker import RenderWorker

logger = logging.getLogger(__name__)


class SurfaceCache(object):
    """Cache rendered pages without rendering Poppler pages on the GTK thread."""

    def __init__(self, doc, max_pages, redraw_callback=None):
        self.max_pages = max_pages
        self.doc = doc
        self.doc_uri = getattr(doc, 'uri', None)
        self.generation = 0
        self.redraw_callback = redraw_callback or (lambda *args: None)
        self.surface_cache = {}
        self.surface_size = {}
        self.surface_type = {}
        self.surface_scale = {}
        self.locks = {}
        self.unlimited = set()
        self.active_widgets = set()
        self.pending = {}
        self.fallbacks = {}
        self.last_surfaces = {}
        self.worker = RenderWorker(self._worker_result, self._is_current)
        self.worker.start()

    def set_redraw_callback(self, callback):
        self.redraw_callback = callback

    def add_widget(self, widget, wtype, prerender_enabled=True, zoomed=False, ignore_max=False):
        name = widget.get_name().rstrip('0123456789') + ('_zoomed' if zoomed else '')
        self.locks.setdefault(name, threading.Lock())
        with self.locks[name]:
            self.surface_cache[name] = collections.OrderedDict()
            self.surface_size[name] = (-1, -1)
            self.surface_scale[name] = 1
            self.surface_type[name] = wtype
        if prerender_enabled and not zoomed:
            self.enable_prerender(name)
        if ignore_max:
            self.unlimited.add(name)

    def swap_document(self, new_doc):
        self.doc = new_doc
        self.doc_uri = getattr(new_doc, 'uri', None)
        self.generation += 1
        self.worker.invalidate(self.generation)
        self.pending.clear()
        self.last_surfaces.clear()
        self.clear_cache()

    def disable_prerender(self, widget_name):
        self.active_widgets.discard(widget_name)

    def enable_prerender(self, widget_name):
        self.active_widgets.add(widget_name)

    def set_widget_type(self, widget_name, wtype):
        with self.locks[widget_name]:
            if self.surface_type[widget_name] != wtype:
                self.surface_type[widget_name] = wtype
                self.surface_cache[widget_name].clear()
                self.generation += 1
                self.worker.invalidate(self.generation)

    def get_widget_type(self, widget_name):
        return self.surface_type[widget_name]

    def clear_cache(self, widget_name=None):
        names = [widget_name] if widget_name is not None else list(self.locks)
        for name in names:
            with self.locks[name]:
                self.surface_cache[name].clear()
                self.fallbacks.pop(name, None)
                self.last_surfaces.pop(name, None)
        self.pending = {key: priority for key, priority in self.pending.items()
                        if key[0] not in names}

    def resize_widget(self, widget_name, width, height, scale=1):
        with self.locks[widget_name]:
            if (width, height) != self.surface_size[widget_name] or scale != self.surface_scale[widget_name]:
                self.surface_cache[widget_name].clear()
                self.fallbacks.pop(widget_name, None)
                self.last_surfaces.pop(widget_name, None)
                self.surface_size[widget_name] = (width, height)
                self.surface_scale[widget_name] = scale
        self.pending = {key: priority for key, priority in self.pending.items()
                        if key[0] != widget_name}

    def get(self, widget_name, page_nb):
        with self.locks[widget_name]:
            cache = self.surface_cache[widget_name]
            surface = cache.get(page_nb)
            if surface is not None:
                cache.move_to_end(page_nb)
            return surface

    def put(self, widget_name, page_nb, surface):
        with self.locks[widget_name]:
            cache = self.surface_cache[widget_name]
            cache[page_nb] = surface
            cache.move_to_end(page_nb)
            self.last_surfaces[widget_name] = ((surface.get_width(), surface.get_height()), surface)
            if widget_name not in self.unlimited:
                while len(cache) > self.max_pages:
                    cache.popitem(False)

    def get_fallback(self, widget_name, width, height, scale):
        """Return a cheap white frame while a page is rendered in the worker."""
        key = (width, height, scale)
        old = self.fallbacks.get(widget_name)
        if old is not None and old[0] == key:
            return old[1]
        previous = self.last_surfaces.get(widget_name)
        if previous is not None:
            expected = (int(width * scale), int(height * scale))
            if previous[0] == expected:
                return previous[1]
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24,
                                     max(1, int(width * scale)),
                                     max(1, int(height * scale)))
        surface.set_device_scale(scale, scale)
        context = cairo.Context(surface)
        context.set_source_rgb(1, 1, 1)
        context.paint()
        self.fallbacks[widget_name] = (key, surface)
        return surface

    @staticmethod
    def _matrix_values(matrix):
        if matrix is None:
            return None
        return (matrix.xx, matrix.yx, matrix.xy, matrix.yy, matrix.x0, matrix.y0)

    def request(self, widget_name, page_nb, width, height, scale, wtype, matrix=None, priority=0):
        if widget_name not in self.locks or width <= 0 or height <= 0 or self.doc_uri is None:
            return
        if self.get(widget_name, page_nb) is not None:
            return
        key = (widget_name, page_nb, width, height, scale)
        if key in self.pending and self.pending[key] <= priority:
            return
        self.pending[key] = priority
        self.worker.submit(self.doc_uri, self.generation, widget_name, page_nb,
                           width, height, scale, wtype, self._matrix_values(matrix), priority)

    def prerender(self, page_nb):
        for name in tuple(self.active_widgets):
            if name not in self.locks:
                continue
            width, height = self.surface_size[name]
            if width > 0 and height > 0:
                self.request(name, page_nb, width, height, self.surface_scale[name], self.surface_type[name], priority=10)

    def renderer(self, widget_name, page_nb):
        """Compatibility entry point used by deck overview."""
        if widget_name not in self.locks:
            return GLib.SOURCE_REMOVE
        width, height = self.surface_size[widget_name]
        if width > 0 and height > 0:
            self.request(widget_name, page_nb, width, height, 1, self.surface_type[widget_name], priority=10)
        return GLib.SOURCE_REMOVE

    def _is_current(self, generation, widget_name, page_nb, width, height, scale):
        key = (widget_name, page_nb, width, height, scale)
        return generation == self.generation and key in self.pending

    def _worker_result(self, generation, widget_name, page_nb, width, height, scale, surface):
        # Poppler work is complete in the worker; cache mutation and GTK redraw
        # happen on the main loop only.
        GLib.idle_add(self._accept_result, generation, widget_name, page_nb,
                      width, height, scale, surface)

    def _accept_result(self, generation, widget_name, page_nb, width, height, scale, surface):
        key = (widget_name, page_nb, width, height, scale)
        self.pending.pop(key, None)
        if generation != self.generation or widget_name not in self.locks:
            return GLib.SOURCE_REMOVE
        with self.locks[widget_name]:
            if self.surface_size[widget_name] != (width, height):
                return GLib.SOURCE_REMOVE
        self.put(widget_name, page_nb, surface)
        self.redraw_callback(widget_name, page_nb)
        return GLib.SOURCE_REMOVE

    def close(self):
        self.worker.stop()
        self.worker.join(timeout=2)

    def __del__(self):
        # GTK normally owns the lifetime, but avoid leaving a worker behind in
        # headless tests or failed application initialization.
        try:
            self.close()
        except Exception:
            pass
