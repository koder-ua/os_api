import functools
from concurrent.futures import Future


def copy_future(dst_future, src_future):
    "copy results from one future to other"
    try:
        dst_future.set_result(src_future.result())
    except:
        dst_future.set_exception(src_future.exception())


class BindToFuture(object):
    def __init__(self, future, cls):
        self.future = future
        self.cls = cls

    def __getattr__(self, name):
        cb = lambda x: getattr(x.result(), name)
        return self.future.next(cb)


class FutureEx(Future):
    def next(self, async_func):
        result_future = self.__class__()

        def closure(future):
            interm_future = async_func(future)
            cb = functools.partial(copy_future, result_future)
            interm_future.add_done_callback(cb)

        self.add_done_callback(closure)
        return result_future

    def get_chain(self):
        return BindToFuture(self, self.__class__)

    chain = property(get_chain)
