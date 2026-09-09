"""
Pydantic types for Django settings.

These are the types used together with the :class:`~configurations.env.Env`
wrapper. They deserialize the value of an environment variable into something
Django understands, for example a database URL into the nested dictionary
Django expects in ``DATABASES``::

    class Prod(Configuration):
        DATABASES: Env[Databases] = Env(name="DATABASE_URL", prefix=None)
        CACHES: Env[Caches] = Env(name="CACHE_URL", prefix=None)
        EMAIL: Env[Email] = Env(name="EMAIL_URL", prefix=None)

Every model in this module knows how to turn itself back into the plain
Python structure Django expects, which is what a configuration writes into
the settings module.
"""

from __future__ import annotations

import os
import re
import typing

from django.core import validators
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.utils.module_loading import import_string
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    model_validator,
)

__all__ = [
    'SettingsModel',
    'Database',
    'Databases',
    'Cache',
    'Caches',
    'Email',
    'Search',
    'Searches',
    'Secret',
    'Backend',
    'Path',
    'ExistingPath',
    'URL',
    'EmailAddress',
    'IPAddress',
    'Regex',
    'as_settings',
]


def _import_parser(module, name, extra):
    """Import the parser of one of the optional URL libraries."""
    try:
        return import_string(f'{module}.parse')
    except ImportError as err:
        raise ImproperlyConfigured(
            f'The {name} setting needs the {module!r} package, install it '
            f"with: pip install django-configurations[{extra}]"
        ) from err


class SettingsMixin:
    """Turns a pydantic model into something that can be written to settings."""

    #: When true the dumped mapping is written as individual settings instead
    #: of as the value of a single setting. ``EMAIL_URL`` for example expands
    #: into ``EMAIL_HOST``, ``EMAIL_PORT`` and friends.
    settings_expand: typing.ClassVar[bool] = False

    def as_settings(self):
        """The plain Python value Django is given for this setting."""
        return self.model_dump(mode='python', exclude_unset=True)


class SettingsModel(SettingsMixin, BaseModel):
    """Base class of the settings models, allows unknown keys to pass through."""

    model_config = ConfigDict(extra='allow')


def as_settings(value):
    """
    Recursively turn pydantic models into the plain Python values Django wants.

    Anything that is not a model is returned unchanged.
    """
    if isinstance(value, SettingsMixin):
        return value.as_settings()
    if isinstance(value, BaseModel):
        return value.model_dump(mode='python')
    if isinstance(value, dict):
        return {key: as_settings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return type(value)(as_settings(item) for item in value)
    return value


class Database(SettingsModel):
    """
    A single entry of the ``DATABASES`` setting.

    Accepts a database URL, which is parsed by ``dj_database_url``, or the
    mapping Django itself uses.
    """

    ENGINE: str
    NAME: typing.Optional[str] = ''
    USER: typing.Optional[str] = ''
    PASSWORD: typing.Optional[str] = ''
    HOST: typing.Optional[str] = ''
    PORT: typing.Union[int, str, None] = ''
    CONN_MAX_AGE: typing.Optional[int] = 0
    CONN_HEALTH_CHECKS: bool = False
    ATOMIC_REQUESTS: bool = False
    AUTOCOMMIT: bool = True
    DISABLE_SERVER_SIDE_CURSORS: bool = False
    TIME_ZONE: typing.Optional[str] = None
    OPTIONS: typing.Dict[str, typing.Any] = Field(default_factory=dict)
    TEST: typing.Dict[str, typing.Any] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def _parse_url(cls, value):
        if isinstance(value, str):
            return _import_parser('dj_database_url', 'DATABASES', 'database')(value)
        return value


class Databases(SettingsMixin, RootModel):
    """
    The ``DATABASES`` setting, a mapping of alias to :class:`Database`.

    A bare database URL is read as the ``default`` database.
    """

    root: typing.Dict[str, Database] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def _default_alias(cls, value):
        if isinstance(value, str):
            return {'default': value}
        return value

    def __getitem__(self, alias):
        return self.root[alias]

    def __iter__(self):
        return iter(self.root)

    def __len__(self):
        return len(self.root)


class Cache(SettingsModel):
    """
    A single entry of the ``CACHES`` setting.

    Accepts a cache URL, which is parsed by ``django_cache_url``, or the
    mapping Django itself uses.
    """

    BACKEND: str
    LOCATION: typing.Union[str, typing.List[str], None] = ''
    KEY_PREFIX: str = ''
    VERSION: int = 1
    TIMEOUT: typing.Optional[int] = 300
    OPTIONS: typing.Dict[str, typing.Any] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def _parse_url(cls, value):
        if isinstance(value, str):
            return _import_parser('django_cache_url', 'CACHES', 'cache')(value)
        return value


class Caches(SettingsMixin, RootModel):
    """
    The ``CACHES`` setting, a mapping of alias to :class:`Cache`.

    A bare cache URL is read as the ``default`` cache.
    """

    root: typing.Dict[str, Cache] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def _default_alias(cls, value):
        if isinstance(value, str):
            return {'default': value}
        return value

    def __getitem__(self, alias):
        return self.root[alias]

    def __iter__(self):
        return iter(self.root)

    def __len__(self):
        return len(self.root)


class Email(SettingsModel):
    """
    The email settings, parsed from an email URL by ``dj_email_url``.

    This one setting expands into the individual ``EMAIL_*`` settings Django
    reads, so ``EMAIL: Env[Email]`` sets ``EMAIL_HOST``, ``EMAIL_PORT`` and
    the rest of them.
    """

    settings_expand = True

    EMAIL_BACKEND: str
    EMAIL_HOST: typing.Optional[str] = None
    EMAIL_PORT: typing.Optional[int] = None
    EMAIL_HOST_USER: typing.Optional[str] = None
    EMAIL_HOST_PASSWORD: typing.Optional[str] = None
    EMAIL_USE_TLS: bool = False
    EMAIL_USE_SSL: bool = False
    EMAIL_TIMEOUT: typing.Optional[int] = None
    EMAIL_FILE_PATH: typing.Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def _parse_url(cls, value):
        if isinstance(value, str):
            return _import_parser('dj_email_url', 'EMAIL', 'email')(value)
        return value


class Search(SettingsModel):
    """A single entry of the haystack ``HAYSTACK_CONNECTIONS`` setting."""

    ENGINE: str
    URL: typing.Optional[str] = None
    INDEX_NAME: typing.Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def _parse_url(cls, value):
        if isinstance(value, str):
            return _import_parser(
                'dj_search_url', 'HAYSTACK_CONNECTIONS', 'search'
            )(value)
        return value


class Searches(SettingsMixin, RootModel):
    """
    The ``HAYSTACK_CONNECTIONS`` setting, a mapping of alias to :class:`Search`.

    A bare search URL is read as the ``default`` connection.
    """

    root: typing.Dict[str, Search] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def _default_alias(cls, value):
        if isinstance(value, str):
            return {'default': value}
        return value

    def __getitem__(self, alias):
        return self.root[alias]

    def __iter__(self):
        return iter(self.root)

    def __len__(self):
        return len(self.root)


def _django_validator(validator, message):
    """Wrap a Django validator so pydantic can run it."""

    def validate(value):
        try:
            validator(value)
        except ValidationError:
            raise ValueError(message.format(value))
        return value

    return AfterValidator(validate)


def _validate_backend(value):
    try:
        import_string(value)
    except ImportError as err:
        raise ValueError(f'Cannot import backend {value!r}: {err}')
    return value


def _expand_path(value):
    return os.path.abspath(os.path.expanduser(value))


def _existing_path(value):
    path = _expand_path(value)
    if not os.path.exists(path):
        raise ValueError(f'Path {path!r} does not exist.')
    return path


#: A value that has to be set in the environment and may not be empty, for
#: example ``SECRET_KEY: Env[Secret]``.
Secret = typing.Annotated[str, StringConstraints(min_length=1)]

#: An importable dotted path, e.g. ``MIDDLEWARE: Env[list[Backend]]``.
Backend = typing.Annotated[str, AfterValidator(_validate_backend)]

#: A filesystem path, with ``~`` expanded and made absolute.
Path = typing.Annotated[str, AfterValidator(_expand_path)]

#: A filesystem path that has to exist.
ExistingPath = typing.Annotated[str, AfterValidator(_existing_path)]

#: A URL, validated by Django.
URL = typing.Annotated[
    str,
    _django_validator(
        validators.URLValidator(), 'Cannot interpret URL value {0!r}'
    ),
]

#: An email address, validated by Django.
EmailAddress = typing.Annotated[
    str,
    _django_validator(
        validators.validate_email, 'Cannot interpret email value {0!r}'
    ),
]

#: An IPv4 or IPv6 address, validated by Django.
IPAddress = typing.Annotated[
    str,
    _django_validator(
        validators.validate_ipv46_address, 'Cannot interpret IP value {0!r}'
    ),
]


def Regex(pattern):
    """
    A string that has to match ``pattern``::

        VERSION: Env[Regex(r"\\d+\\.\\d+")]
    """
    if isinstance(pattern, re.Pattern):
        pattern = pattern.pattern

    def validate(value):
        if re.search(pattern, value) is None:
            raise ValueError(f"Regex doesn't match value {value!r}")
        return value

    return typing.Annotated[str, AfterValidator(validate)]
