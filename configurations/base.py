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
import os
import re
import typing
import warnings

from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic._internal._model_construction import ModelMetaclass
from pydantic_core import PydanticUndefined

from .env import UNSET, env_config, environ_name, read_environ
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


def _is_pydantic_ignored(value):
    """Whether pydantic leaves an unannotated class attribute alone."""
    return isinstance(value, (property, classmethod, staticmethod, type)) or callable(value)


class ConfigurationBase(ModelMetaclass):

    def __new__(cls, name, bases, attrs, **kwargs):
        parents = [base for base in bases if isinstance(base, ConfigurationBase)]
        if parents:
            # if this is actually a subclass in a settings module
            # we better check if the importer was correctly installed
            from . import importer
            if not importer.installed:
                raise ImproperlyConfigured(install_failure)

        settings_vars = uppercase_attributes(global_settings)
        if parents:
            for base in bases[::-1]:
                settings_vars.update(uppercase_attributes(base))

        own_annotations = dict(attrs.get('__annotations__') or {})

        deprecated_settings = {
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
            "FORMS_URLFIELD_ASSUME_HTTPS"
        }
        # PASSWORD_RESET_TIMEOUT_DAYS is deprecated in favor of
        # PASSWORD_RESET_TIMEOUT in Django 3.1
        # https://github.com/django/django/commit/226ebb17290b604ef29e82fb5c1fbac3594ac163#diff-ec2bed07bb264cb95a80f08d71a47c06R163-R170
        if "PASSWORD_RESET_TIMEOUT" in settings_vars:
            deprecated_settings.add("PASSWORD_RESET_TIMEOUT_DAYS")
        # DEFAULT_FILE_STORAGE and STATICFILES_STORAGE are deprecated
        # in favor of STORAGES.
        # https://docs.djangoproject.com/en/dev/releases/4.2/#custom-file-storages
        if "STORAGES" in settings_vars:
            deprecated_settings.add("DEFAULT_FILE_STORAGE")
            deprecated_settings.add("STATICFILES_STORAGE")
        for deprecated_setting in deprecated_settings:
            settings_vars.pop(deprecated_setting, None)

        # a setting that a parent declared as a pydantic field is inherited as
        # a field, it must not be shadowed by the Django default of the same
        # name
        inherited_fields = {}
        for base in bases[::-1]:
            inherited_fields.update(getattr(base, 'model_fields', None) or {})
        for field_name in inherited_fields:
            settings_vars.pop(field_name, None)

        # a setting this class annotates itself is defined by this class
        # alone. The Django default of the same name must not become its
        # default, that would turn a required setting into an optional one
        for annotated_name, annotation in own_annotations.items():
            settings_vars.pop(annotated_name, None)

        attrs = {**settings_vars, **attrs}
        annotations = dict(attrs.get('__annotations__') or {})

        # the Django defaults also live on the parent classes, where pydantic
        # picks them up as the default of an annotated setting. The sentinel
        # marks the ones that are declared without a default as required.
        for annotated_name, annotation in own_annotations.items():
            if annotated_name not in attrs and not _is_class_var(annotation):
                attrs[annotated_name] = PydanticUndefined

        # every configuration carries the Django defaults as class attributes,
        # which is exactly what shadows an inherited field. Redeclaring the
        # fields of the parents keeps them out of reach.
        for field_name, field in inherited_fields.items():
            if field_name in annotations or field_name in attrs:
                continue
            annotations[field_name] = field.rebuild_annotation()
            redeclared = copy.copy(field)
            # the metadata is already carried by the rebuilt annotation
            redeclared.metadata = []
            attrs[field_name] = redeclared

        # pydantic refuses to build a model with attributes that have no
        # annotation, so the settings that are not typed are marked as class
        # variables, which leaves them exactly where they are
        for attr_name, value in attrs.items():
            if (isuppercase(attr_name)
                    and attr_name not in annotations
                    and not _is_pydantic_ignored(value)):
                annotations[attr_name] = typing.ClassVar[typing.Any]
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

    #: The configured instance, cached per configuration class.
    _instance: typing.ClassVar[typing.Any] = None

    @model_validator(mode='before')
    @classmethod
    def _read_environment(cls, data):
        """Fill the fields annotated with ``Env`` from the environment."""
        if not isinstance(data, dict):
            return data
        values = dict(data)
        for name, field in cls.model_fields.items():
            if name in values:
                continue
            found, value = read_environ(name, field)
            if found:
                values[name] = value
        return values

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
        The validated instance of this configuration.

        Building it reads the environment and lets pydantic deserialize and
        validate every typed setting.
        """
        instance = cls.__dict__.get('_instance')
        if instance is None:
            try:
                instance = cls()
            except PydanticValidationError as err:
                raise ImproperlyConfigured(_error_message(cls, err)) from err
            type.__setattr__(cls, '_instance', instance)
        return instance

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
            setting, expanded = _export(name, getattr(instance, name), field)
            for attr_name, value in {**setting, **expanded}.items():
                type.__setattr__(cls, attr_name, value)

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
        expanded = {}
        for name, value in attributes.items():
            if (name not in fields
                    and callable(value)
                    and not getattr(value, 'pristine', False)):
                value = value()
            if isinstance(value, Value):
                # a Value can also be returned by a method, it is set up the
                # same way as one that is a class attribute
                setting, extra = _expand_value(name, value)
            else:
                setting, extra = _export(name, value, fields.get(name))
            resolved.update(setting)
            expanded.update(extra)
        # a setting that expands into several settings wins over the Django
        # default of the same name, no matter the order they are resolved in
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


def _expand_value(name, value):
    """
    Resolve a legacy ``Value`` into the settings it stands for.

    Returns the setting itself and the settings it expands into.
    """
    resolved = value.setup(name)
    if value.multiple:
        return {name: value.value}, dict(resolved)
    return {name: value.value}, {}


def _export(name, value, field=None):
    """
    Turn a resolved setting into the settings it stands for.

    Returns the setting itself and the settings it expands into, pydantic
    models are dumped into the plain structures Django expects.
    """
    if isinstance(value, BaseModel):
        expand = getattr(value, 'settings_expand', False)
        config = env_config(field) if field is not None else None
        if config is not None and config.expand is not UNSET:
            expand = config.resolved_expand
        dumped = as_settings(value)
        if expand:
            if not isinstance(dumped, dict):
                raise ImproperlyConfigured(
                    f'The setting {name!r} cannot be expanded into several '
                    f'settings, it is not a mapping'
                )
            # the setting itself is kept next to the settings it expands into
            return {name: dumped}, dict(dumped)
        return {name: dumped}, {}
    return {name: as_settings(value)}, {}
