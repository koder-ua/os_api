try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

config = {
    'description': 'concurrent.future based openstack api',
    'author': 'kdanilov aka koder',
    'url': 'https://github.com/koder-ua/os_api',
    # 'download_url': 'Where to download it.',
    'author_email': 'kdanilov@mirantis.com',
    'version': '0.1',
    'install_requires': ['futures'],
    'packages': ['os_api'],
    'scripts': [],
    'name': 'os_api'
}

setup(**config)
