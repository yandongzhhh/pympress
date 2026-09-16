# -*- coding: utf-8 -*-
"""Background PDF renderer.

Poppler documents are deliberately owned by this worker and are never shared
with the GTK thread or another renderer.
"""
import logging
import queue
import threading

import cairo

from pympress import document

logger = logging.getLogger(__name__)


class RenderWorker(threading.Thread):
    """Render one PDF page at a time and return Cairo image surfaces."""

    def __init__(self, result_callback):
        super(RenderWorker, self).__init__(name='pympress-renderer', daemon=True)
        self.result_callback = result_callback
        self.jobs = queue.Queue()
        self.stop_requested = False
        self.worker_doc = None
        self.worker_uri = None
        self.worker_generation = None
        self.generation_lock = threading.Lock()
        self.latest_generation = 0

    def invalidate(self, generation):
        with self.generation_lock:
            self.latest_generation = generation

    def submit(self, uri, generation, widget_name, page_nb, width, height, scale, wtype, matrix=None):
        self.jobs.put((uri, generation, widget_name, page_nb, width, height,
                       scale, wtype, matrix))

    def stop(self):
        self.stop_requested = True
        self.jobs.put(None)

    def _get_document(self, uri, generation):
        if uri is None:
            return None
        if uri != self.worker_uri or generation != self.worker_generation:
            # This Poppler.Document is private to this worker.
            poppler_doc = document.Poppler.Document.new_from_file(uri, None)
            self.worker_doc = document.Document(None, poppler_doc, uri)
            # Page construction resolves links through UI callbacks.  The
            # worker only renders, so provide harmless callbacks instead of
            # sharing the UI builder or the main Document.
            self.worker_doc.goto_page = lambda *args, **kwargs: None
            self.worker_doc.goto_next_hist = lambda *args, **kwargs: None
            self.worker_doc.goto_prev_hist = lambda *args, **kwargs: None
            self.worker_uri = uri
            self.worker_generation = generation
        return self.worker_doc

    def run(self):
        while not self.stop_requested:
            job = self.jobs.get()
            if job is None:
                break
            uri, generation, widget_name, page_nb, width, height, scale, wtype, matrix = job
            with self.generation_lock:
                if generation < self.latest_generation:
                    continue
            try:
                doc = self._get_document(uri, generation)
                if doc is None:
                    continue
                page = doc.page(page_nb)
                if page is None or not page.can_render():
                    continue

                pixel_width = max(1, int(round(width * scale)))
                pixel_height = max(1, int(round(height * scale)))
                surface = cairo.ImageSurface(cairo.FORMAT_RGB24, pixel_width, pixel_height)
                surface.set_device_scale(scale, scale)
                context = cairo.Context(surface)
                if matrix is not None:
                    context.transform(cairo.Matrix(*matrix))
                page.render_cairo(context, width, height, wtype)
                context = None
                surface.flush()
                self.result_callback(generation, widget_name, page_nb, width, height, scale, surface)
            except Exception:
                logger.exception('Background rendering failed for page %s', page_nb)
