from threading import Timer


class Debounce(object):

    def __init__(self, period, f):
        self.period = period  # never call the wrapped function more often than this (in seconds)
        self.f = f
        self.t = None

    # force a reset of the timer, ie the next call will start new timer
    def reset(self):
        if self.t is not None:
            self.t.cancel()
            self.t = None

    # TODO: do we want to call w/ first or last args? wouldn't it make sense for
    # subsequent calls to reset the timer, and the f() to be called w/ last args?
    def __call__(self, *args, **kwargs):
        def invoke():
            self.t = None
            self.f(*args, **kwargs)

        if self.t is None:
            self.t = Timer(self.period, invoke)
            self.t.start()
