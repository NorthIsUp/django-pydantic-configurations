"""A configuration in a module with postponed annotation evaluation."""

from __future__ import annotations

from typing import ClassVar, List

from configurations import Configuration, Env
from configurations.types import Databases, Secret


class FutureConfiguration(Configuration):

    DEBUG: Env[bool] = False

    SECRET_KEY: Env[Secret]

    ALLOWED_HOSTS: Env[List[str]] = ['localhost']

    DATABASES: Env[Databases] = Env('sqlite://', name='DATABASE_URL', prefix=None)

    NOT_A_SETTING: ClassVar[int] = 3

    ROOT_URLCONF = 'tests.urls'
