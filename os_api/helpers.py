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

import os

from novaclient.client import Client as n_client

from nova import update_nova_with_async


def ostack_get_creds():
    env = os.environ.get
    name = env('OS_USERNAME')
    passwd = env('OS_PASSWORD')
    tenant = env('OS_TENANT_NAME')
    auth_url = env('OS_AUTH_URL')

    if name is None:
        raise RuntimeError("You should setup "
                           "openstack ENV variables first")

    return name, passwd, tenant, auth_url


def nova_client():
    return update_nova_with_async(n_client('1.1', *ostack_get_creds()))
