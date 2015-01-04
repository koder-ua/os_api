import os
import time
import Queue
import heapq
import pprint
import logging
import functools
import traceback
import threading

from concurrent.futures import Future

from novaclient.client import Client as n_client
from novaclient.v1_1.servers import ServerManager


futures_logger = logging.getLogger('concurrent.futures')
futures_logger.addHandler(logging.StreamHandler())
del futures_logger


def ostack_get_creds():
    env = os.environ.get
    name = env('OS_USERNAME')
    passwd = env('OS_PASSWORD')
    tenant = env('OS_TENANT_NAME')
    auth_url = env('OS_AUTH_URL')
    return name, passwd, tenant, auth_url


def nova_client():
    return update_nova_with_async(n_client('1.1', *ostack_get_creds()))


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


class ServersMonitoredThread(threading.Thread):
    terminal_states = ('active', 'error')

    def __init__(self, queue, nova, check_timeout=1):
        super(ServersMonitoredThread, self).__init__()
        self.input_q = queue

        # map server id to according future
        # weakref.WeakValueDictionary()
        self.monitored_servers = {}

        self.creating_ids = set()
        self.deleting_ids = set()
        self.timeout_queue = []

        self.prev_check_time = 0
        self.check_timeout = check_timeout
        self.nova = nova

    def process_new_servers(self):
        try:
            ctime = time.time()
            while True:
                server_id, future, wait_for, wait_timeout = \
                    self.input_q.get(False, 0.0)

                timeout_queue_itm = (ctime + wait_timeout, server_id, wait_for)
                heapq.heappush(self.timeout_queue, timeout_queue_itm)

                print "Get new vm for monitoring",
                print server_id, future, wait_for, wait_timeout
                if wait_for == WAIT_FOR_TERMINAL_STATE:
                    print "Will wait till creation complete"
                    self.creating_ids.add(server_id)
                elif wait_for == WAIT_FOR_DELETION:
                    print "Will wait till deletion complete"
                    self.deleting_ids.add(server_id)
                else:
                    msg_tmpl = "Unknown value for wait_for=={0!r} argument"
                    future.set_exception(ValueError(msg_tmpl.format(wait_for)))
                    continue
                self.monitored_servers[server_id] = future
        except Queue.Empty:
            pass

    def server_ready(self, server, state):
        self.creating_ids.remove(server.id)
        future = self.monitored_servers.pop(server.id)

        if state == 'active':
            future.set_result(server)
        elif state == 'error':
            future.set_exception(NovaError(server.id,
                                           "Server creation failed"))
        else:
            msg = "Don't known how to process state {!r}".format(state)
            raise InconsistentLogic(msg)

    def run(self):
        print "Start monitoring"
        try:
            self.do_run()
        except:
            import traceback
            traceback.print_exc()
            raise

    def process_timeouts(self):
        ctime = time.time()
        while self.timeout_queue != [] and self.timeout_queue[0][0] < ctime:
            _, server_id, wait_for = heapq.heappop(self.timeout_queue)
            try:
                future = self.monitored_servers.pop(server_id)
            except KeyError:
                # object already processed, skip timeout
                continue

            future.set_exception(Timeout(server_id))

            if wait_for == WAIT_FOR_TERMINAL_STATE:
                self.creating_ids.remove(server_id)
            elif wait_for == WAIT_FOR_DELETION:
                self.deleting_ids.remove(server_id)
            else:
                print "Unknown wait_for value {0!r}".format(wait_for)
                traceback.print_stack()

    def do_run(self):
        while True:
            dt = self.check_timeout - (time.time() - self.prev_check_time)
            if dt > 0:
                time.sleep(dt)

            # check for new monitored servers
            self.process_new_servers()

            if len(self.monitored_servers) == 0:
                print "Wait for new vm enternally"
                # wait till new data appears
                vl = self.input_q.get()
                # put it back
                self.input_q.put(vl)
                continue

            # check new server states
            servers = self.nova.servers.list()
            print "Get new server list. Monitored dict =",
            print pprint.pformat(self.monitored_servers)
            self.prev_check_time = time.time()

            all_servers = set()

            # process all ready servers
            for server in servers:
                all_servers.add(server.id)
                if server.id in self.creating_ids:
                    state = getattr(server, 'OS-EXT-STS:vm_state').lower()
                    if state in self.terminal_states:
                        print "server {0} is ready".format(server)
                        self.server_ready(server, state)

            # found removed states
            for server_id in (self.deleting_ids - all_servers):
                self.deleting_ids.remove(server_id)
                future = self.monitored_servers.pop(server_id)
                future.set_result(None)

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
    the_result = Future()
    async_io_simple_cb(gen, the_result, None)
    return the_result


class AsyncServerManager(ServerManager):
    DEFAULT_CREATE_TIMEOUT = 2
    DEFAULT_DELETE_TIMEOUT = 120

    def create_async(self, *args, **kwargs):
        res = Future()
        server = self.create(*args, **kwargs)
        res.sync_result = server
        request = (server.id, res, WAIT_FOR_TERMINAL_STATE,
                   self.DEFAULT_CREATE_TIMEOUT)
        self._future_q__.put(request)
        return res

    def delete_async(self, server=None, server_id=None):
        res = Future()
        if server_id is None:
            server_id = server.id
        res.sync_result = self.delete(server_id)
        request = (server_id, res, WAIT_FOR_DELETION,
                   self.DEFAULT_DELETE_TIMEOUT)
        self._future_q__.put(request)
        return res

    def create_async_r2(self, *args, **kwargs):
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

    def create_async_r(self, *args, **kwargs):
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

        func = self.create_async
        for _ in range(retry_count):
            func = compose_async(func, drop_if_err)
            func = compose_async(func, recreate_if_dropped)
        return func(*args, **kwargs)


def update_nova_with_async(nova):
    q = Queue.Queue()
    th = ServersMonitoredThread(q, nova)
    th.daemon = True
    th.start()
    nova.servers.__class__ = AsyncServerManager
    nova.servers._future_q__ = q
    nova.servers._future_th__ = th
    return nova
