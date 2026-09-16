# -*- coding: utf-8 -*-
"""Background PDF renderer.

Poppler documents are deliberately owned by this worker and are never shared
with the GTK thread or another renderer.
"""
import logging
import heapq
import itertools
import threading

import cairo

from pympress import document

logger = logging.getLogger(__name__)


class RenderWorker(threading.Thread):
    """Render one PDF page at a time and return Cairo image surfaces."""

    def __init__(self, result_callback, is_current=None):
        super(RenderWorker, self).__init__(name='pympress-renderer', daemon=True)
        self.result_callback = result_callback
        self.is_current = is_current or (lambda *args: True)
        self.jobs = []
        self.jobs_lock = threading.Condition()
        self.sequence = itertools.count()
        self.stop_requested = False
        self.worker_doc = None
        self.worker_uri = None
        self.worker_generation = None
        self.generation_lock = threading.Lock()
        self.latest_generation = 0

    def invalidate(self, generation):
        with self.generation_lock:
            self.latest_generation = generation

    def submit(self, uri, generation, widget_name, page_nb, width, height, scale,
               wtype, matrix=None, priority=10):
        job = (priority, next(self.sequence), uri, generation, widget_name,
               page_nb, width, height, scale, wtype, matrix)
        with self.jobs_lock:
            heapq.heappush(self.jobs, job)
            self.jobs_lock.notify()

    def stop(self):
        self.stop_requested = True
        with self.jobs_lock:
            self.jobs_lock.notify()

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
            with self.jobs_lock:
                while not self.jobs and not self.stop_requested:
                    self.jobs_lock.wait()
                if self.stop_requested:
                    break
                _, _, uri, generation, widget_name, page_nb, width, height, scale, wtype, matrix = heapq.heappop(self.jobs)
            if not self.is_current(generation, widget_name, page_nb, width, height, scale):
                continue
            with self.generation_lock:
                if generation < self.latest_generation:
                    continue
            try:
                doc = self._get_document(uri, generation)
                if doc is None:
                    continue
                # Deck thumbnails do not need links, media or annotations.
                # Avoid constructing the UI-facing Page wrapper for them;
                # current/notes pages retain the wrapper for annotation
                # semantics.
                raw_page = doc.doc.get_page(page_nb) if widget_name == 'deck' else None
                page = None if raw_page is not None else doc.page(page_nb)
                if raw_page is None and (page is None or not page.can_render()):
                    continue

                pixel_width = max(1, int(round(width * scale)))
                pixel_height = max(1, int(round(height * scale)))
                surface = cairo.ImageSurface(cairo.FORMAT_RGB24, pixel_width, pixel_height)
                surface.set_device_scale(scale, scale)
                context = cairo.Context(surface)
                if matrix is not None:
                    context.transform(cairo.Matrix(*matrix))
                if raw_page is None:
                    page.render_cairo(context, width, height, wtype)
                else:
                    pw, ph = raw_page.get_size()
                    render_width, render_height = wtype.scale().from_screen(pw, ph)
                    render_scale = min(width / render_width, height / render_height)
                    context.set_source_rgb(1, 1, 1)
                    context.scale(render_scale, render_scale)
                    context.rectangle(0, 0, render_width, render_height)
                    context.fill()
                    if wtype == document.PdfPage.RIGHT:
                        context.translate(-pw, 0)
                    elif wtype == document.PdfPage.BOTTOM:
                        context.translate(0, -ph)
                    raw_page.render(context)
                context = None
                surface.flush()
                self.result_callback(generation, widget_name, page_nb, width, height, scale, surface)
            except Exception:
                logger.exception('Background rendering failed for page %s', page_nb)
