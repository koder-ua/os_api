## concurrent.futures based api for some novaclient functions (POC)

The goal for this package is to provide a POC for convenient API for
openstack api function's, which done actual work in background after
returning preliminary result. Such as - create server, delete server,
create volume, etc. This action may fails in background or hangs, 
and novaclient library provides no common way to handle such problems.

Common pattern to create vm looks like this:

```python
for i in range(try_count):
    vm = novaclient.servers.create(...)

    for i in range(counter):
        time.sleep(SOME_SMALL_TIMEOUT)
        vm = novaclient.servers.get(vm)
        if vm.state in ('active', 'error'):
            break

    if vm.state == 'active':
        break

    novaclient.servers.delete(vm)
    # here might be a same check cycle for delete,
    # as delete also happened in background
```

API provides no way to automate retry, waiting for results, etc.
Common way to deal with such problems is futures
(http://en.wikipedia.org/wiki/Futures_and_promises, 
https://pypi.python.org/pypi/futures, 
https://docs.python.org/3/library/concurrent.futures.html).

Python 2.X don't have futures in standard library, but back port module
available (https://pypi.python.org/pypi/futures).

The idea is to add to novaclient module functions, which returns future
for all background operations provides a common way for

 * check whenever action complete at the moment
 * waiting for action to complete
 * set complete timeout
 * retrying in case of background failures

Only create and delete functions are implemented at the moment.

### Usage examples

Create and block till ready in one line

```python
server = nova.servers.create_async('koder-async', flavor=fl, image=img).result()
```

Create with retry and check periodically
```python
future = nova.servers.create_async('koder-async', flavor=fl, image=img, retry_count=3)
# do some work
if future.ready():
    ...
# do some work
server = future.result()
```


Full example
```python

from os_api.nova import nova_client, NovaError, Timeout

# takes parameters from os.env
nova = nova_client()

fl = nova.flavors.find(ram=512)
img = nova.images.find(name='TestVM')

# request for new vm
vm_future = nova.servers.create_async('koder-async', flavor=fl, image=img)

# print results from novaclient.client.servers.create
print vm_future.sync_result

# Request for new vm. Will retry 2 times in case if vm fails to start
# vm_future_2 future has no sync_result field, as vm might be
# deleted and created up to retry_count times
vm_future_2 = nova.servers.create_async_r2('koder-async', flavor=fl, image=img,
                                           retry_count=2)

# DO_SOME_WORK_HERE

# block until vm will became 'active'
# will raise an exception in case of creation fails
# or takes to long
try:
    vm = vm_future.result()
    print vm

    # request for delete and wait till done
    nova.servers.delete_async(vm).result()
except Timeout:
    print "VM start timeout"
except NovaError as exc:
    print "VM fails to start due to openstack error", str(exc)

print vm_future_2.result()

```

### List of background api

	* server create/delete
	* volume create/delete
	* volume attach/detach
	* vm image create
	* vm backup
	* vm migrate

### TODO

	* server might be deleted duting creation
