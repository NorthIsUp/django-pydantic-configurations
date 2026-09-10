"""
Environment variable support for settings, backed by pydantic.

The public interface of this module is the :class:`Env` type wrapper. It is
used as an annotation in a :class:`~configurations.base.Configuration` and
marks a setting as being loaded from the process environment::

    from configurations import Configuration
    from configurations.env import Env
    from configurations.types import Databases, Secret

    class Prod(Configuration):
        DEBUG: Env[bool] = False
        SECRET_KEY: Env[Secret]
        ALLOWED_HOSTS: Env[list[str]] = ["localhost"]
        DATABASES: Env[Databases] = Env(name="DATABASE_URL", prefix=None)

``Env[T]`` is a plain ``typing.Annotated`` alias, so the deserialization of
the raw environment string into ``T`` is done by pydantic itself and works
anywhere pydantic does, including with a bare ``TypeAdapter``::

    >>> from pydantic import TypeAdapter
    >>> TypeAdapter(Env[list[int]]).validate_python("1, 2, 3")
    [1, 2, 3]
"""

from __future__ import annotations

import ast
import collections.abc
import functools
import json
import os
import types as _types
import typing
from dataclasses import dataclass, fields as dataclass_fields, replace

from pydantic import BaseModel, BeforeValidator, Field
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

__all__ = [
    'Env',
    'EnvConfig',
    'DEFAULT_PREFIX',
    'DEFAULT_SEPARATORS',
    'parse_env_string',
    'resolve_env_fields',
    'env_config',
    'environ_name',
]


#: Prefix prepended to the environment variable name of every ``Env`` setting.
DEFAULT_PREFIX = 'DJANGO'

#: Separators used to split sequences, one per level of nesting. A
#: ``list[str]`` is split on ``","``, a ``list[list[str]]`` on ``";"`` and
#: then on ``","``.
DEFAULT_SEPARATORS = (',', ';')


class _Unset:
    """Sentinel for :class:`EnvConfig` options that were not given."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'UNSET'

    def __bool__(self):
        return False

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


#: Marker for options that were not explicitly configured.
UNSET = _Unset()


@dataclass(frozen=True)
class EnvConfig:
    """
    Configuration of a single environment backed setting.

    Instances double as the marker that tells a
    :class:`~configurations.base.Configuration` that a field is to be loaded
    from the environment. They are carried in the pydantic field metadata,
    either through the ``Env[...]`` annotation or through ``Env(...)``.
    """

    name: typing.Any = UNSET
    prefix: typing.Any = UNSET
    required: typing.Any = UNSET
    separators: typing.Any = UNSET
    expand: typing.Any = UNSET

    def merge(self, other: 'EnvConfig') -> 'EnvConfig':
        """Return a copy of this config updated with the set options of ``other``."""
        overrides = {
            field.name: getattr(other, field.name)
            for field in dataclass_fields(other)
            if getattr(other, field.name) is not UNSET
        }
        if not overrides:
            return self
        return replace(self, **overrides)

    @property
    def resolved_prefix(self):
        return DEFAULT_PREFIX if self.prefix is UNSET else self.prefix

    @property
    def resolved_required(self):
        return False if self.required is UNSET else bool(self.required)

    @property
    def resolved_separators(self):
        if self.separators is UNSET:
            return DEFAULT_SEPARATORS
        return tuple(self.separators)

    @property
    def resolved_expand(self):
        return False if self.expand is UNSET else bool(self.expand)

    def environ_name(self, field_name):
        """The name of the environment variable to read for ``field_name``."""
        name = field_name.upper() if self.name is UNSET or self.name is None else self.name
        prefix = self.resolved_prefix
        if prefix:
            prefix = prefix[:-1] if prefix.endswith('_') else prefix
            name = f'{prefix}_{name}'
        return name

    def __get_pydantic_core_schema__(self, source_type, handler):
        # This object is only metadata, it does not change the schema.
        return handler(source_type)

    def __hash__(self):
        return hash(
            tuple(
                _hashable(getattr(self, field.name))
                for field in dataclass_fields(self)
            )
        )


def _hashable(value):
    return tuple(value) if isinstance(value, list) else value


class Env:
    """
    Type wrapper marking a setting as loaded from the environment.

    Used as an annotation, ``Env[T]`` deserializes the environment variable
    belonging to the setting into ``T`` using pydantic::

        DEBUG: Env[bool] = False
        ADMINS: Env[list[str]] = []
        DATABASES: Env[Databases]

    Called, ``Env(...)`` returns a pydantic field with additional
    configuration of the environment lookup, comparable to ``pydantic.Field``::

        DATABASES: Env[Databases] = Env(name="DATABASE_URL", prefix=None)
        PATHS: Env[list[str]] = Env(default=[], separator=":")
    """

    def __class_getitem__(cls, item):
        if isinstance(item, tuple):
            raise TypeError('Env[...] takes a single type, e.g. Env[list[str]]')
        try:
            return _annotate(item)
        except TypeError:
            # an annotation that cannot be cached, e.g. an unhashable one
            return _build_annotation(item)

    def __new__(
        cls,
        default=PydanticUndefined,
        *,
        name=UNSET,
        prefix=UNSET,
        required=UNSET,
        separator=UNSET,
        separators=UNSET,
        expand=UNSET,
        **kwargs,
    ):
        if separator is not UNSET:
            if separators is not UNSET:
                raise TypeError("Pass either 'separator' or 'separators', not both")
            separators = (separator,)
        field = Field(default=default, **kwargs)
        field.metadata.append(
            EnvConfig(
                name=name,
                prefix=prefix,
                required=required,
                separators=separators,
                expand=expand,
            )
        )
        return field


def _build_annotation(item):
    return typing.Annotated[
        item, EnvConfig(), BeforeValidator(_standalone_parser(item))
    ]


@functools.lru_cache(maxsize=None)
def _annotate(item):
    return _build_annotation(item)


def env_config(field: FieldInfo) -> typing.Optional[EnvConfig]:
    """
    Return the merged :class:`EnvConfig` of a pydantic field, if any.

    Fields without an ``Env`` annotation are not loaded from the environment
    and return ``None``.
    """
    config = None
    for metadata in field.metadata:
        if isinstance(metadata, EnvConfig):
            config = metadata if config is None else config.merge(metadata)
    return config


def environ_name(field_name, field: FieldInfo):
    """The environment variable name a field is read from, or ``None``."""
    config = env_config(field)
    if config is None:
        return None
    return config.environ_name(field_name)


def _unwrap_annotated(annotation):
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _unwrap_optional(annotation):
    """Reduce ``Optional[X]``/``Union[X, None]`` to ``X`` where unambiguous."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is getattr(_types, 'UnionType', None):
        args = [
            _unwrap_annotated(arg)
            for arg in typing.get_args(annotation)
            if arg is not type(None)  # noqa: E721
        ]
        if len(args) == 1:
            return args[0]
        # An ambiguous union is left to pydantic's smart union handling.
        for arg in args:
            if _is_collection(arg) or _is_model(arg):
                return arg
    return annotation


_SEQUENCE_TYPES = (list, tuple, set, frozenset)
_SEQUENCE_ORIGINS = (
    collections.abc.Sequence,
    collections.abc.MutableSequence,
    collections.abc.Set,
    collections.abc.MutableSet,
)
_MAPPING_ORIGINS = (
    collections.abc.Mapping,
    collections.abc.MutableMapping,
)


def _is_sequence(annotation):
    origin = typing.get_origin(annotation) or annotation
    if origin in _SEQUENCE_TYPES or origin in _SEQUENCE_ORIGINS:
        return True
    return isinstance(origin, type) and issubclass(origin, _SEQUENCE_TYPES)


def _is_mapping(annotation):
    origin = typing.get_origin(annotation) or annotation
    if origin is dict or origin in _MAPPING_ORIGINS:
        return True
    return isinstance(origin, type) and issubclass(origin, dict)


def _is_model(annotation):
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _is_collection(annotation):
    return _is_sequence(annotation) or _is_mapping(annotation)


def _sequence_depth(annotation):
    """Levels of nested sequences in ``annotation``, e.g. 2 for ``list[list[str]]``."""
    annotation = _unwrap_optional(_unwrap_annotated(annotation))
    if not _is_sequence(annotation):
        return 0
    depths = [
        _sequence_depth(arg)
        for arg in typing.get_args(annotation)
        if arg is not Ellipsis
    ]
    return 1 + (max(depths) if depths else 0)


def _element_annotations(annotation, count):
    """The annotations of ``count`` items of a sequence annotation."""
    args = [arg for arg in typing.get_args(annotation) if arg is not Ellipsis]
    if not args:
        return [typing.Any] * count
    if len(args) == 1 or typing.get_origin(annotation) is not tuple:
        return [args[0]] * count
    # A fixed length tuple annotation, e.g. tuple[int, str].
    return [args[index] if index < len(args) else args[-1] for index in range(count)]


def _loads(text, message=None):
    """Parse JSON, falling back to a Python literal."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        raise ValueError(message or f'Cannot interpret value {text!r}')


def parse_env_string(annotation, value, separators=DEFAULT_SEPARATORS):
    """
    Turn the raw string of an environment variable into a structure that
    pydantic can validate against ``annotation``.

    Scalars are handed to pydantic untouched, it already knows how to read
    ``"1"`` as an ``int`` or ``"yes"`` as a ``bool``. Sequences are split on
    the separator matching their nesting level and mappings are read as JSON
    or as a Python literal.
    """
    if not isinstance(value, str):
        return value

    annotation = _unwrap_optional(_unwrap_annotated(annotation))

    if _is_model(annotation):
        # Models are given the raw string, they know how to read their own
        # serialized form (a URL for example). JSON is decoded for them.
        text = value.strip()
        if text[:1] in ('{', '['):
            return _loads(text)
        return value

    if _is_mapping(annotation):
        text = value.strip()
        if not text:
            return {}
        return _loads(text)

    if _is_sequence(annotation):
        text = value.strip()
        if text[:1] == '[':
            return _loads(text)
        depth = _sequence_depth(annotation)
        separator = separators[min(depth, len(separators)) - 1] if separators else ','
        items = [item.strip() for item in text.split(separator)]
        items = [item for item in items if item]
        elements = _element_annotations(annotation, len(items))
        return [
            parse_env_string(element, item, separators)
            for element, item in zip(elements, items)
        ]

    if annotation is bool and not value.strip():
        # An empty environment variable has always been read as False.
        return False

    return value


def _standalone_parser(annotation):
    """
    Build the validator that lets ``Env[T]`` work on its own, outside a
    configuration, as in ``TypeAdapter(Env[list[int]])``.

    Inside a configuration :func:`resolve_env_fields` has already parsed the
    value with the settings of its field, so this sees a parsed value and
    passes it through. Only a standalone adapter, which has no field and so
    no configuration to read, reaches the parser here.
    """

    def parse(value):
        return parse_env_string(annotation, value)

    parse.__qualname__ = f'parse_env_string[{annotation!r}]'
    return parse


def _raw_value(field_name, field: FieldInfo, config: EnvConfig, values, environ):
    """
    The unparsed value of an environment backed field.

    A setting can be given explicitly, come from the environment or fall back
    to its default. Returns ``UNSET`` when there is nothing to parse.
    """
    if field_name in values:
        return values[field_name]
    name = config.environ_name(field_name)
    if name in environ:
        return environ[name]
    if config.resolved_required:
        raise ValueError(
            f'Setting {field_name!r} is required to be set as the '
            f'environment variable {name!r}'
        )
    if isinstance(field.default, str):
        # a default written the way the environment would write it
        return field.default
    return UNSET


def resolve_env_fields(fields, values, environ=None):
    """
    Deserialize every environment backed field of a model, in one pass.

    This is the only place a setting is parsed, which is what keeps the
    configuration of a field, a custom separator for example, from applying
    to the value in the environment but not to the default beside it.
    """
    environ = os.environ if environ is None else environ
    resolved = dict(values)
    for field_name, field in fields.items():
        config = env_config(field)
        if config is None:
            continue
        raw = _raw_value(field_name, field, config, resolved, environ)
        if raw is UNSET:
            continue
        resolved[field_name] = parse_env_string(
            field.annotation, raw, config.resolved_separators
        )
    return resolved
