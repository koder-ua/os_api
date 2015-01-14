### future-based API for background operations
----------------------------------------------

#### Abstract
-------------

There a set of openstack api functions which starts background actions
and return preliminary results - like 'novaclient.create'. Those functions
requires periodically check results and handle timeouts/errors
(and often cleanup + restart helps to fix an error).

Check/retry/cleanup code duplicated over a lot of core projects.
As examples - heat, tempest, rally, etc and definitely in many third-party.

I propose to provide common higth-level API for such functions, which uses
'futures' (http://en.wikipedia.org/wiki/Futures_and_promises) as a way to
present background task.

Idea is to add to each background-task-starter function a complimentary call,
that returns 'future' object. E.g.

    create_future = novaclient.servers.create_async(....)
    .....
    vm = create_future.result()

This allows to unify(and optimize) monitoring cycles, retries, etc.
Please found complete BP at
https://github.com/koder-ua/os_api/blob/master/README.md

#### Problem description
------------------------

openstack api have some (~20-30) API calls, which starts background task
and returns preliminary results. Mainly this is functions, which not only
updates db, but also create/delete/update real objects. As examples:

    * server create/delete/backup/migrate
    * volume create/delete
    * volume attach/detach
    * and others

Openstack clients provides no convenient functions, which allows to
wait till background operations complete. Also there no way to setup
execution timeout.

Also any background operation may result en error. Often such errors
are caused by temporary reasons and retries fixes a problem. This is
important difference of openstack API among many others, where
error usually causes by some real reason and retries would not help
until problem would be fixed. In contrast to fixin error retries might
be automated and also provided by API.

As result of this factors code like this can be found in a lot of applications,
which uses openstack clients (novaclient/cinderclient):

```python
for i in range(TRY_COUNT):
    obj = client.create_object()

    for i in range(COUNTER):
        time.sleep(TIMEOUT)
        obj = client.get(obj)
        if obj.state in ('active', 'error'):
            break

    if obj.state == 'active':
        break

    novaclient.servers.delete(vm)
    # there might be same cycle for delete
    # as delete are usually also happened un background

obj = client.get(obj)
if obj.state != 'active':
    raise SomeError("Can't create ......")
```

#### Solution
-------------

Idea is to provide API, which would provide a common way to
handle background operations. It should provides a convenient
implementations for waiting cycles, cleanup and retries. In
couple lines of simple code. Also new API should be compatible with
current openstack client API and should allows to starts a set of
background operation and wait for results (should not block, unless
user specify so).

** While the work 'async' might used to describe this API some times -
it have nothing common with async http requests. All HTTP request are
remains synchronous as in current API. **

Goal is to add to each function, which starts background task, a
complimentary method, which returns a 'future' object
(http://en.wikipedia.org/wiki/Futures_and_promises).
This future objects represents a background task.

Future is a common way to handgle asyncroneous operations in modern APIs
(http://en.wikipedia.org/wiki/Futures_and_promises#List_of_implementations).
Important difference from lightweight thread (erlang, gevent,
stackless-python) and libraries like asyncio
(https://docs.python.org/3/library/asyncio.html)
is that future allows to handle a lot of background tasks from
single execution thread (but future-based code usually is more complicated).

Python 3.X provides standard module for future - concurrent.future
(https://docs.python.org/3/library/concurrent.futures.html)
and there is backport for 2.X - futures (https://pypi.python.org/pypi/futures).

Example above, written with future API might looks like
(no auto rstart in case of error):

```python
for i in range(TRY_COUNT):
    try:
        future_obj = client.create_object_async()
        obj = future_obj.result(TIMEOUT2)
        break
    except (TimeoutError, ObjectCreationError):
        client.delete_async(future_obj.sync_res.id).result()
```

Additional sync_res field contains result of complimentary non-future
call from openstack client. XXX_async has one additional parameter -
competition timeout.

Second set of methods - XXX_async_r provides retry in case of
timeout or errors. With such function example might fit in one line:

```python
res = client.create_object_async_r(try_count=TRY_COUNT, tout=TIMEOUT2).result()
```

Future API, as a current API allows to start a set of background tasks
from single thread and process results in any order:


```python
    vm1_future = client.create_object_async_r(...)
    vm2_future = client.create_object_async_r(...)

    while not vm1_future.done():
        # do some other work here

    vm1 = vm1_future.result()
    vm2 = vm2_future.result()
```

Current implementation is a POC and available at
https://github.com/koder-ua/os_api only create/delete
servers are implemented.

#### Implementation details
---------------------------

Background tasks monitored from supplementary thread, which
accepts monitored objects throw queue. Each async function
calls according non-future function, creates a future,
pass this objects to helpers thread and return to caller code.

In case if there tasks to monitor helper thread calls time
after time openstack api and check tasks status. In case of tasks
finished or takes to much time value or exception is passed to
future and task is removed from monitoring list,

#### Comments
-------------

  * Extended implementations of future allow "chain" async call to future
    (e.g. in scala -
    http://docs.scala-lang.org/overviews/core/futures.html#functional-composition-and-for-comprehensions)
    this is results in new futures:

    ```python
    vm_future = client.create_object_async_r(...)

    vm_future = vm_future.next(lambda vm: vm.associate_ip(ip))
    vm_future = vm_future.next(lambda vm: vm.attach_volume(vol))

    ```
    There no such functionality in python futures module (while
    it can be easily added).

  * API is depends on concurrent.future/futures. But really it needs
    only Future class and couple of complimentary functions,
    which can be picked from futures module.

  * It's possible to provide common way to handle background tasks by
    catch 202 code in http client

  * It's possible to execute current sync API from separated thread.
    This would leads to full async code and not a part of this BP.
