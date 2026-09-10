"""
The base configuration class.

A :class:`Configuration` is a pydantic model. Every annotated attribute is a
pydantic field and is validated when the configuration is set up, and the
ones annotated with :class:`~configurations.env.Env` are read from the
environment and deserialized by pydantic::

    class Prod(Configuration):
        DEBUG: Env[bool] = False
        SECRET_KEY: Env[Secret]
        ALLOWED_HOSTS: Env[list[str]] = ["localhost"]
        DATABASES: Env[Databases] = Env(name="DATABASE_URL", prefix=None)

Attributes without an annotation keep working the way they always have, they
are plain class attributes and are copied to the settings module untouched.
"""

from __future__ import annotations

import copy
import functools
import os
import re
import types
import typing
import warnings

from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic._internal._model_construction import ModelMetaclass
from pydantic_core import PydanticUndefined

from .env import UNSET, env_config, environ_name, resolve_env_fields
from .types import as_settings
from .utils import isuppercase, uppercase_attributes
from .values import Value, setup_value

__all__ = ['Configuration']


install_failure = ("django-configurations settings importer wasn't "
                   "correctly installed. Please use one of the starter "
                   "functions to install it as mentioned in the docs: "
                   "https://django-configurations.readthedocs.io/")


def _is_class_var(annotation):
    """Whether an annotation, which may be a string, is a ``ClassVar``."""
    if isinstance(annotation, str):
        return annotation.split('[')[0].rsplit('.', 1)[-1].strip() == 'ClassVar'
    return (annotation is typing.ClassVar
            or typing.get_origin(annotation) is typing.ClassVar)


#: What pydantic accepts in a model namespace without an annotation. Anything
#: else has to be marked, which is what :func:`_mark_class_variables` does. The
#: test suite pins this, so a change on their side fails loudly rather than
#: crashing a configuration that happens to hold one of these.
PYDANTIC_IGNORES = (
    property,
    functools.cached_property,
    classmethod,
    staticmethod,
    functools.partial,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
)


def _is_pydantic_ignored(value):
    """Whether pydantic leaves an unannotated class attribute alone."""
    return isinstance(value, PYDANTIC_IGNORES)


def _check_importer_installed():
    """A configuration is only usable once the settings importer is in place."""
    from . import importer
    if not importer.installed:
        raise ImproperlyConfigured(install_failure)


def _django_defaults(bases):
    """
    The settings a configuration starts from: the Django defaults, updated
    with what its parents carry, minus the ones Django deprecated.
    """
    defaults = uppercase_attributes(global_settings)
    for base in bases[::-1]:
        defaults.update(uppercase_attributes(base))

    deprecated = {
        # DEFAULT_HASHING_ALGORITHM is always deprecated, as it's a
        # transitional setting
        # https://docs.djangoproject.com/en/3.1/releases/3.1/#default-hashing-algorithm-settings
        "DEFAULT_HASHING_ALGORITHM",
        # DEFAULT_CONTENT_TYPE and FILE_CHARSET are deprecated in
        # Django 2.2 and are removed in Django 3.0
        "DEFAULT_CONTENT_TYPE",
        "FILE_CHARSET",
        # When DEFAULT_AUTO_FIELD is not explicitly set, Django's emits a
        # system check warning models.W042. This warning should not be
        # suppressed, as downstream users are expected to make a decision.
        # https://docs.djangoproject.com/en/3.2/releases/3.2/#customizing-type-of-auto-created-primary-keys
        "DEFAULT_AUTO_FIELD",
        # FORMS_URLFIELD_ASSUME_HTTPS is a transitional setting introduced
        # in Django 5.0.
        # https://docs.djangoproject.com/en/5.0/releases/5.0/#id2
        "FORMS_URLFIELD_ASSUME_HTTPS",
    }
    # PASSWORD_RESET_TIMEOUT_DAYS is deprecated in favor of
    # PASSWORD_RESET_TIMEOUT in Django 3.1
    # https://github.com/django/django/commit/226ebb17290b604ef29e82fb5c1fbac3594ac163#diff-ec2bed07bb264cb95a80f08d71a47c06R163-R170
    if "PASSWORD_RESET_TIMEOUT" in defaults:
        deprecated.add("PASSWORD_RESET_TIMEOUT_DAYS")
    # DEFAULT_FILE_STORAGE and STATICFILES_STORAGE are deprecated
    # in favor of STORAGES.
    # https://docs.djangoproject.com/en/dev/releases/4.2/#custom-file-storages
    if "STORAGES" in defaults:
        deprecated.add("DEFAULT_FILE_STORAGE")
        deprecated.add("STATICFILES_STORAGE")
    for setting in deprecated:
        defaults.pop(setting, None)
    return defaults


def _inherited_fields(bases):
    """The pydantic fields this configuration inherits from its parents."""
    fields = {}
    for base in bases[::-1]:
        fields.update(getattr(base, 'model_fields', None) or {})
    return fields


def _redeclare(attrs, annotations, inherited_fields):
    """
    Redeclare the fields of the parents in this namespace.

    Every configuration carries the Django defaults as class attributes, and
    an attribute of the same name on a parent is what pydantic would take as
    the default of an inherited field. Redeclaring them keeps that out of
    reach.
    """
    for field_name, field in inherited_fields.items():
        if field_name in annotations or field_name in attrs:
            continue
        annotations[field_name] = field.rebuild_annotation()
        redeclared = copy.copy(field)
        # the metadata is already carried by the rebuilt annotation
        redeclared.metadata = []
        attrs[field_name] = redeclared


def _mark_required(attrs, annotations, own_annotations):
    """
    Mark the settings this class annotates without a default as required.

    The Django default of the same name lives on a parent class, where
    pydantic would find it and turn a required setting into an optional one.
    The sentinel is what pydantic itself uses for "no default given".
    """
    for name, annotation in own_annotations.items():
        if name not in attrs and not _is_class_var(annotation):
            attrs[name] = PydanticUndefined


def _mark_class_variables(attrs, annotations):
    """
    Annotate the settings that are not typed as class variables.

    Pydantic refuses to build a model with attributes that have no
    annotation, and marking them leaves them exactly where they are.
    """
    for name, value in attrs.items():
        if (isuppercase(name)
                and name not in annotations
                and not _is_pydantic_ignored(value)):
            annotations[name] = typing.ClassVar[typing.Any]


class ConfigurationBase(ModelMetaclass):

    def __new__(cls, name, bases, attrs, **kwargs):
        parents = [base for base in bases if isinstance(base, ConfigurationBase)]
        if parents:
            _check_importer_installed()

        own_annotations = dict(attrs.get('__annotations__') or {})
        defaults = _django_defaults(bases) if parents else _django_defaults(())
        inherited_fields = _inherited_fields(bases)

        # a setting this class annotates itself, and one a parent declared as
        # a field, are owned by the configuration rather than by Django
        for field_name in inherited_fields:
            defaults.pop(field_name, None)
        for field_name in own_annotations:
            defaults.pop(field_name, None)

        attrs = {**defaults, **attrs}
        annotations = dict(attrs.get('__annotations__') or {})

        _redeclare(attrs, annotations, inherited_fields)
        _mark_required(attrs, annotations, own_annotations)
        _mark_class_variables(attrs, annotations)
        attrs['__annotations__'] = annotations

        with warnings.catch_warnings():
            # a typed setting deliberately shadows the Django default of the
            # same name that was copied onto the parent class
            warnings.filterwarnings(
                'ignore',
                message=r'^Field name .* shadows an attribute in parent',
                category=UserWarning,
            )
            return super().__new__(cls, name, bases, attrs, **kwargs)

    def __repr__(self):
        return "<Configuration '{}.{}'>".format(self.__module__,
                                                self.__name__)


class Configuration(BaseModel, metaclass=ConfigurationBase):
    """
    The base configuration class to inherit from.

    ::

        class Develop(Configuration):
            EXTRA_AWESOME = True

            DEBUG: Env[bool] = True

            @property
            def SOMETHING(self):
                return completely.different()

            def OTHER(self):
                if whatever:
                    return (1, 2, 3)
                return (4, 5, 6)

    The module this configuration class is located in will
    automatically get the class and instance level attributes
    with upper characters if the ``DJANGO_CONFIGURATION`` is set
    to the name of the class.

    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_default=True,
        extra='ignore',
        ignored_types=(Value,),
    )

    DOTENV_LOADED = None

    @model_validator(mode='before')
    @classmethod
    def _read_environment(cls, data):
        """Deserialize the settings annotated with ``Env``."""
        if not isinstance(data, dict):
            return data
        return resolve_env_fields(cls.model_fields, data)

    @classmethod
    def load_dotenv(cls):
        """
        Pulled from Honcho code with minor updates, reads local default
        environment variables from a .env file located in the project root
        or provided directory.

        https://wellfire.co/learn/easier-12-factor-django/
        https://gist.github.com/bennylope/2999704
        """
        # check if the class has DOTENV set whether with a path or None
        dotenv = getattr(cls, 'DOTENV', None)

        # if DOTENV is falsy we want to disable it
        if not dotenv:
            return

        # now check if we can access the file since we know we really want to
        try:
            with open(dotenv) as f:
                content = f.read()
        except OSError as e:
            raise ImproperlyConfigured("Couldn't read .env file "
                                       "with the path {}. Error: "
                                       "{}".format(dotenv, e)) from e
        else:
            for line in content.splitlines():
                m1 = re.match(r'\A([A-Za-z_0-9]+)=(.*)\Z', line)
                if not m1:
                    continue
                key, val = m1.group(1), m1.group(2)
                m2 = re.match(r"\A'(.*)'\Z", val)
                if m2:
                    val = m2.group(1)
                m3 = re.match(r'\A"(.*)"\Z', val)
                if m3:
                    val = re.sub(r'\\(.)', r'\1', m3.group(1))
                os.environ.setdefault(key, val)

            cls.DOTENV_LOADED = dotenv

    @classmethod
    def pre_setup(cls):
        if cls.DOTENV_LOADED is None:
            cls.load_dotenv()

    @classmethod
    def post_setup(cls):
        pass

    @classmethod
    def configured(cls):
        """
        Build and validate an instance of this configuration.

        Building it reads the environment and lets pydantic deserialize and
        validate every typed setting.
        """
        try:
            return cls()
        except PydanticValidationError as err:
            raise ImproperlyConfigured(_error_message(cls, err)) from err

    @classmethod
    def setup(cls):
        """
        Resolve the typed settings and write the result back onto the class.

        Settings loaded from the environment are available as plain class
        attributes afterwards, e.g. ``Develop.DEBUG``.
        """
        for name, value in uppercase_attributes(cls).items():
            if isinstance(value, Value):
                setup_value(cls, name, value)
        instance = cls.configured()
        fields = type(instance).model_fields
        for name, field in fields.items():
            for setting, value in _contributions(name, getattr(instance, name),
                                                 field).items():
                setattr(cls, setting, value)

    @classmethod
    def settings(cls):
        """
        The settings this configuration resolves to, as a plain dictionary.

        This is what ends up in the settings module: callables are called,
        pydantic models are turned back into the structures Django expects
        and the settings that expand into several settings are unpacked.
        """
        instance = cls.configured()
        fields = type(instance).model_fields
        attributes = uppercase_attributes(instance)
        for name in fields:
            attributes[name] = getattr(instance, name)

        resolved = {}
        # a setting that stands for several settings wins over the Django
        # default of the same name, no matter the order they are resolved in
        expanded = {}
        for name, value in attributes.items():
            if (name not in fields
                    and callable(value)
                    and not getattr(value, 'pristine', False)):
                value = value()
            contributions = _contributions(name, value, fields.get(name))
            resolved[name] = contributions.pop(name)
            expanded.update(contributions)
        resolved.update(expanded)
        return resolved


def _error_message(cls, error):
    """Turn a pydantic validation error into a settings error message."""
    lines = [f'{len(error.errors())} invalid setting(s) in {cls.__name__}:']
    for detail in error.errors():
        location = detail['loc'][0] if detail['loc'] else '<configuration>'
        line = f"  {location}: {detail['msg']}"
        field = cls.model_fields.get(location)
        if field is not None:
            name = environ_name(location, field)
            if name is not None:
                line += f' (environment variable {name})'
        lines.append(line)
    return '\n'.join(lines)


def _expands(value, field=None):
    """Whether a setting is written as the several settings it stands for."""
    config = env_config(field) if field is not None else None
    if config is not None and config.expand is not UNSET:
        return config.resolved_expand
    if isinstance(value, Value):
        return value.multiple
    return getattr(value, 'settings_expand', False)


def _contributions(name, value, field=None):
    """
    Every setting a resolved value contributes, keyed by name.

    The setting itself always comes first, and a value that stands for
    several settings, an email URL for example, adds them beside it. Pydantic
    models are dumped into the plain structures Django expects.
    """
    if isinstance(value, Value):
        # a Value can also be returned by a method, it is set up the same way
        # as one that is a class attribute
        expansion = value.setup(name)
        settings = {name: value.value}
    else:
        settings = {name: as_settings(value)}
        expansion = settings[name]
    if not _expands(value, field):
        return settings
    if not isinstance(expansion, dict):
        raise ImproperlyConfigured(
            f'The setting {name!r} cannot be written as several settings, '
            f'it is not a mapping'
        )
    return {**settings, **expansion}
