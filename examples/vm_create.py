from os_api.nova import nova_client, NovaError, Timeout

nova = nova_client()

fl = nova.flavors.find(ram=512)
img = nova.images.find(name='TestVM')

# request for new vm
vm_future = nova.servers.create_async('koder-async', flavor=fl, image=img)

# print results from novaclient.client.servers.create
print vm_future.sync_result

# Request for new vm. Will retry 2 times in case if vm fails to start
# vm_future_2 future has no sync_result field, as vm migth be
# deleted and created up to retry_count times
vm_future_2 = nova.servers.create_async_r2('koder-async', flavor=fl, image=img,
                                           retry_count=2)

# DO_SOME_WORK_HERE

# block untill vm will became 'active'
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
