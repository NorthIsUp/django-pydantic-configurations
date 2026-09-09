from typing import Dict, List, Optional

from configurations import Configuration
from configurations.env import Env
from configurations.types import (Backend, Caches, Databases, Email,
                                  EmailAddress, IPAddress, Secret)


class TypedConfiguration(Configuration):
    """A configuration that gets its settings from the environment."""

    DEBUG: Env[bool] = False

    SITE_ID: Env[int] = 1

    SECRET_KEY: Env[Secret]

    ALLOWED_HOSTS: Env[List[str]] = ['localhost']

    INTERNAL_IPS: Env[List[IPAddress]] = []

    ADMIN_EMAIL: Env[Optional[EmailAddress]] = None

    MIDDLEWARE: Env[List[Backend]] = [
        'django.middleware.common.CommonMiddleware',
    ]

    OPTIONS: Env[Dict[str, int]] = {}

    PATHS: Env[List[str]] = Env(default=[], separator=':')

    DATABASES: Env[Databases] = Env('sqlite://', name='DATABASE_URL', prefix=None)

    CACHES: Env[Caches] = Env('locmem://', name='CACHE_URL', prefix=None)

    EMAIL: Env[Email] = Env('console://', name='EMAIL_URL', prefix=None)

    INSTALLED_APPS = [
        'django.contrib.contenttypes',
        'django.contrib.auth',
    ]

    ROOT_URLCONF = 'tests.urls'
