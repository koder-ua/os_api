# Copyright 2015 kdanilov aka koder. koder.mail@gmail.com
# https://github.com/koder-ua
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import time
import Queue
import heapq
import logging
import functools
import traceback
import threading

from concurrent.futures import Future


from novaclient.v1_1.servers import ServerManager


futures_logger = logging.getLogger('concurrent.futures')
futures_logger.addHandler(logging.StreamHandler())
del futures_logger


api_logger = logging.getLogger('nova.future.api')
api_logger.addHandler(logging.StreamHandler())
bg_thread_logger = logging.getLogger('nova.future.bg_thread')
bg_thread_logger.addHandler(logging.StreamHandler())


WAIT_FOR_TERMINAL_STATE = 'wait_for_active_or_error'
WAIT_FOR_DELETION = 'wait_for_deletion'


class InconsistentLogic(ValueError):
    pass


class NovaError(RuntimeError):
    default_message = "Nova fails to do something"

    def __init__(self, obj_id=None, message=None):
        if message is None:
            message = self.default_message

        super(NovaError, self).__init__(message)
        self.obj_id = obj_id


class Timeout(NovaError):
    default_message = 'Server fails to start in required time'


def get_server_state(server):
    return getattr(server, 'OS-EXT-STS:vm_state').lower()


class ServersMonitoredThread(threading.Thread):
    """
    Background thread, which monitore servers state and notify futures
    when action completes
    """

    terminal_states = ('active', 'error')

    def __init__(self, queue, nova, check_timeout=1):
        super(ServersMonitoredThread, self).__init__()
        self.input_q = queue

        # map server id to according future
        # weakref.WeakValueDictionary()
        self.monitored_servers = {}

        # id's of servers, waiting for particular event type
        self.creating_ids = set()
        self.deleting_ids = set()

        # heap of all timeouts
        self.timeout_queue = []

        self.prev_check_time = 0
        self.check_timeout = check_timeout
        self.nova = nova

    def process_new_servers(self):
        "get new monitoring request from queue"

        try:
            # cache current time value
            ctime = time.time()

            # get all new requests from queue. Exit when self.input_q.get
            # throws Queue.Empty exception
            while True:
                server_id, future, wait_for, wait_timeout = \
                    self.input_q.get(False, 0.0)

                msg_templ = "Get new vm for monitoring {0}, {1}, tout={2}"
                msg = msg_templ.format(server_id, wait_for, wait_timeout)
                bg_thread_logger.debug(msg)

                if wait_for == WAIT_FOR_TERMINAL_STATE:
                    self.creating_ids.add(server_id)
                elif wait_for == WAIT_FOR_DELETION:
                    self.deleting_ids.add(server_id)
                else:
                    msg_tmpl = "Unknown value for wait_for=={0!r} argument"
                    msg = msg_tmpl.format(wait_for)
                    future.set_exception(ValueError(msg))
                    bg_thread_logger.warning(msg)
                    # skip adding future to wait list and timeout heap
                    continue

                self.monitored_servers[server_id] = future

                if wait_timeout is not None:
                    # calculate operation finish time limit
                    timeout_queue_itm = (ctime + wait_timeout,
                                         server_id, wait_for)
                    heapq.heappush(self.timeout_queue, timeout_queue_itm)

        # no more requests left
        except Queue.Empty:
            pass

    def server_ready(self, server, state):
        "called when server creation complete"

        self.creating_ids.remove(server.id)
        future = self.monitored_servers.pop(server.id)

        if state == 'active':
            future.set_result(server)
        elif state == 'error':
            msg = "Server {} get into error state".format(server.id)
            bg_thread_logger.critical(msg)
            future.set_exception(NovaError(server.id, msg))
        else:
            msg = "Don't known how to process state {!r}".format(state)
            raise InconsistentLogic(msg)

    def run(self):
        "thread entry point"

        bg_thread_logger.info("Start monitoring")
        try:
            self.do_run()
        except:
            bg_thread_logger.exception()
            raise

    def process_timeouts(self):
        "find and process all timeouted actions"

        ctime = time.time()
        while self.timeout_queue != [] and self.timeout_queue[0][0] < ctime:
            _, server_id, wait_for = heapq.heappop(self.timeout_queue)
            try:
                future = self.monitored_servers.pop(server_id)
            except KeyError:
                # object already processed, skip timeout
                # this happened due to fact, that remove any particular item
                # from heap is long task, so in case if operation completed
                # successfully we don't remove timeout from self.timeout_queue
                # heap immidiatelly
                continue

            # notify caller about timeout
            future.set_exception(Timeout(server_id))

            if wait_for == WAIT_FOR_TERMINAL_STATE:
                self.creating_ids.remove(server_id)
            elif wait_for == WAIT_FOR_DELETION:
                self.deleting_ids.remove(server_id)
            else:
                msg = "Unknown wait_for value wait_for={0!r}".format(wait_for)
                raise InconsistentLogic(msg)

    def do_run(self):
        "periodicall request nova-api for server states"

        while True:
            # each cycle should takes self.check_timeout seconds
            # bug http requests takes a time, so calculate how much
            # we need to sleep
            dt = self.check_timeout - (time.time() - self.prev_check_time)
            if dt > 0:
                time.sleep(dt)

            # check for new monitored servers
            self.process_new_servers()

            # if list of in-progress servers is empty - stop monitoring and
            # wait for new request
            if len(self.monitored_servers) == 0:
                bg_thread_logger.debug("vm list ia empty. "
                                       "Will wait for new vm enternally")
                # wait till new request appears
                vl = self.input_q.get()

                # put it back - process_new_servers will process it
                # this may cause request reordering, but we don't care
                self.input_q.put(vl)

                # restart cycle with zero wait time
                self.prev_check_time = 0
                continue

            # check new server states
            servers = self.nova.servers.list()
            self.prev_check_time = time.time()

            # used to found disappeared servers
            all_servers = set()

            # process all ready servers
            for server in servers:
                all_servers.add(server.id)
                if server.id in self.creating_ids:
                    state = get_server_state(server)
                    if state in self.terminal_states:
                        msg = "server {0} is ready".format(server.id)
                        bg_thread_logger.debug(msg)
                        self.server_ready(server, state)

            # process deleted servers
            for server_id in (self.deleting_ids - all_servers):
                msg = "server {0} deleted".format(server_id)
                bg_thread_logger.debug(msg)
                self.deleting_ids.remove(server_id)
                future = self.monitored_servers.pop(server_id)

                # None means successfully deleted
                future.set_result(None)

            # find all timeouted operations
            self.process_timeouts()


def copy_future(dst_future, src_future):
    "copy results from one future to other"
    try:
        dst_future.set_result(src_future.result())
    except:
        dst_future.set_exception(src_future.exception())


def apply_async_func(dst_future, async_func, complited_future):
    "asyncroneusly apply future-based async function to future"
    try:
        intern_future = async_func(complited_future)
        cb = functools.partial(copy_future, dst_future)
        intern_future.add_done_callback(cb)
    except Exception as exc:
        traceback.print_exc()
        dst_future.set_exception(exc)


def compose_async(async_f1, async_f2):
    "function composition for future-based asyncroneous functions"
    @functools.wraps(async_f2)
    def composition(*args, **kwargs):
        fut1 = async_f1(*args, **kwargs)
        intern_future = Future()
        cb = functools.partial(apply_async_func, intern_future, async_f2)
        fut1.add_done_callback(cb)
        return intern_future
    return composition


def async_io_simple_cb(generator, the_result_future, temporar_future):
    """
    very simple asyncio(https://docs.python.org/3/library/asyncio.html)-like
    manager, used to compose async functions throw genertators. Core function
    """

    try:
        ready, res_or_future = next(generator)

        if ready:
            the_result_future.set_result(res_or_future)
        else:
            cb = functools.partial(async_io_simple_cb,
                                   generator,
                                   the_result_future)
            res_or_future.add_done_callback(cb)
    except Exception as exc:
        the_result_future.set_exception(exc)


def async_io_simple(gen):
    """
    very simple asyncio(https://docs.python.org/3/library/asyncio.html)-like
    manager, used to compose async functions throw genertators.
    Front-end function
    """
    the_result = Future()
    async_io_simple_cb(gen, the_result, None)
    return the_result


class AsyncServerManager(ServerManager):
    """
    Container for all _async functions, targeted to replace ServerManager
    """

    DEFAULT_CREATE_TIMEOUT = 120
    DEFAULT_DELETE_TIMEOUT = 120

    def create_async(self, *args, **kwargs):
        "create new vm. returns a future, which allows to monitor creation"

        res = Future()
        server = self.create(*args, **kwargs)
        res.sync_result = server
        request = (server.id, res, WAIT_FOR_TERMINAL_STATE,
                   self.DEFAULT_CREATE_TIMEOUT)
        self._future_q__.put(request)
        return res

    def delete_async(self, server=None, server_id=None):
        "delete vm. returns a future, which allows to monitor deletion"

        res = Future()
        if server_id is None:
            server_id = server.id
        res.sync_result = self.delete(server_id)
        request = (server_id, res, WAIT_FOR_DELETION,
                   self.DEFAULT_DELETE_TIMEOUT)
        self._future_q__.put(request)
        return res

    def create_async_r(self, *args, **kwargs):
        """ create new vm with retry. returns a future,
        which allows to monitor creation. Function composition base version
        """

        # args and kwargs shoud not be modified
        # until future completes
        retry_count = kwargs.pop('retry_count', 3)

        def recreate_if_dropped(future_after_delete):
            # Nova exceptions were catched and supressed by drop_if_err
            # function. Any other exception should be passed through
            res = future_after_delete.result()

            # not none means, that server had created alredy
            # and code should just pass results through
            # so returns already completed future
            if res is not None:
                return future_after_delete

            # None means server had deleted successfully
            # and need to be recreated
            return self.create_async(*args, **kwargs)

        def drop_if_err(creation_future):
            # drop_if_err should:
            #   in case of NovaError - suppress it, drop current VM
            #      and returns Future(None)
            #   In case of any other error - pass it through
            #   In case of no error - bypass future
            try:
                creation_future.result()
            except NovaError as nova_exc:
                return self.delete_async(server_id=nova_exc.obj_id)
            return creation_future

        # compose create/drop funtions into one large monster
        func = self.create_async
        for _ in range(retry_count):
            func = compose_async(func, drop_if_err)
            func = compose_async(func, recreate_if_dropped)
        return func(*args, **kwargs)

    def create_async_r2(self, *args, **kwargs):
        """ create new vm with retry generator-based version. returns a future,
        which allows to monitor creation
        """
        rc = kwargs.pop('retry_count', 5)
        if rc < 0:
            raise ValueError()
        elif rc == 0:
            return self.create_async(*args, **kwargs)

        def closure():
            create_future = self.create_async(*args, **kwargs)
            yield False, create_future

            for counter in range(rc):
                try:
                    result = create_future.result()
                except NovaError as nova_exc:
                    yield False, self.delete_async(server_id=nova_exc.obj_id)
                else:
                    yield True, result

                create_future = self.create_async(*args, **kwargs)
                yield False, create_future

            yield True, create_future.result()

        return async_io_simple(closure())


def update_nova_with_async(nova):
    "monkey-patch nova.servers instance to add async functions"

    q = Queue.Queue()
    th = ServersMonitoredThread(q, nova)
    th.daemon = True
    th.start()
    nova.servers.__class__ = AsyncServerManager
    nova.servers._future_q__ = q
    nova.servers._future_th__ = th
    return nova
