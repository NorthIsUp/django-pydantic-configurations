"""
The class based settings values.

This is the original interface of django-configurations and it keeps working
as it always has. It is implemented on top of pydantic now, every value is
deserialized and validated by a pydantic type adapter.

New code is better served by the :class:`~configurations.env.Env` type
wrapper, which lets pydantic do the same work from a plain type annotation::

    # instead of
    DEBUG = values.BooleanValue(False)
    ADMINS = values.ListValue(["admin@example.com"])

    # write
    DEBUG: Env[bool] = False
    ADMINS: Env[list[EmailAddress]] = ["admin@example.com"]
"""

import copy
import decimal
import os
import sys
import typing

from django.core import validators
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.utils.module_loading import import_string
from pydantic import PlainValidator, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from .env import parse_env_string
from .types import Cache, Database, Email, Search, as_settings
from .utils import getargspec


def setup_value(target, name, value):
    actual_value = value.setup(name)
    # overwriting the original Value class with the result
    setattr(target, name, value.value)
    if value.multiple:
        for multiple_name, multiple_value in actual_value.items():
            setattr(target, multiple_name, multiple_value)


def validate(annotation, value, message=None):
    """Deserialize and validate a value with pydantic."""
    try:
        return TypeAdapter(annotation).validate_python(value)
    except PydanticValidationError as err:
        if message is None:
            raise ValueError(str(err)) from err
        raise ValueError(message.format(value)) from err


class Value:
    """
    A single settings value that is able to interpret env variables
    and implements a simple validation scheme.
    """
    multiple = False
    late_binding = False
    environ_required = False

    #: The type the raw environment string is deserialized into by pydantic.
    python_type = str
    #: Separators used to split sequences, one per level of nesting.
    separators = (',', ';')
    message = 'Cannot interpret value {0!r}'

    @property
    def value(self):
        value = self.default
        if not hasattr(self, '_value') and self.environ_name:
            self.setup(self.environ_name)
        if hasattr(self, '_value'):
            value = self._value
        return value

    @value.setter
    def value(self, value):
        self._value = value

    def __new__(cls, *args, **kwargs):
        """
        checks if the creation can end up directly in the final value.
        That is the case whenever environ = False or environ_name is given.
        """
        instance = object.__new__(cls)
        if 'late_binding' in kwargs:
            instance.late_binding = kwargs.get('late_binding')
        if not instance.late_binding:
            instance.__init__(*args, **kwargs)
            if ((instance.environ and instance.environ_name)
                    or (not instance.environ and instance.default)):
                instance = instance.setup(instance.environ_name)
        return instance

    def __init__(self, default=None, environ=True, environ_name=None,
                 environ_prefix='DJANGO', environ_required=False,
                 *args, **kwargs):
        if isinstance(default, Value) and default.default is not None:
            self.default = copy.copy(default.default)
        else:
            self.default = default
        self.environ = environ
        if environ_prefix and environ_prefix.endswith('_'):
            environ_prefix = environ_prefix[:-1]
        self.environ_prefix = environ_prefix
        self.environ_name = environ_name
        self.environ_required = environ_required

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return repr(self.value)

    def __eq__(self, other):
        return self.value == other

    def __bool__(self):
        return bool(self.value)

    # Compatibility with python 2
    __nonzero__ = __bool__

    def full_environ_name(self, name):
        if self.environ_name:
            environ_name = self.environ_name
        else:
            environ_name = name.upper()
        if self.environ_prefix:
            environ_name = f'{self.environ_prefix}_{environ_name}'
        return environ_name

    def setup(self, name):
        value = self.default
        if self.environ:
            full_environ_name = self.full_environ_name(name)
            if full_environ_name in os.environ:
                value = self.to_python(os.environ[full_environ_name])
            elif self.environ_required:
                raise ValueError('Value {!r} is required to be set as the '
                                 'environment variable {!r}'
                                 .format(name, full_environ_name))
        self.value = value
        return value

    def to_python(self, value):
        """
        Convert the given value of a environment variable into an
        appropriate Python representation of the value, using pydantic.
        """
        return validate(
            self.python_type,
            parse_env_string(self.python_type, value, self.separators),
            self.message,
        )


class MultipleMixin:
    multiple = True


class BooleanValue(Value):
    python_type = bool
    message = 'Cannot interpret boolean value {0!r}'
    true_values = ('yes', 'y', 'true', '1')
    false_values = ('no', 'n', 'false', '0', '')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.default not in (True, False):
            raise ValueError('Default value {!r} is not a '
                             'boolean value'.format(self.default))

    def to_python(self, value):
        normalized_value = value.strip().lower()
        if normalized_value in self.true_values:
            return True
        elif normalized_value in self.false_values:
            return False
        return validate(bool, normalized_value, self.message)


class CastingMixin:
    """
    Runs the raw value through a caster before pydantic validates it.

    The caster is either a callable or the dotted path to one.
    """
    exception = (TypeError, ValueError)
    message = 'Cannot interpret value {0!r}'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.caster, str):
            try:
                self._caster = import_string(self.caster)
            except ImportError as err:
                msg = f"Could not import {self.caster!r}"
                raise ImproperlyConfigured(msg) from err
        elif callable(self.caster):
            self._caster = self.caster
        else:
            error = 'Cannot use caster of {} ({!r})'.format(self,
                                                            self.caster)
            raise ValueError(error)
        try:
            arg_names = getargspec(self._caster)[0]
            self._params = {name: kwargs[name] for name in arg_names if name in kwargs}
        except TypeError:
            self._params = {}
        self._adapter = TypeAdapter(
            typing.Annotated[typing.Any, PlainValidator(self.cast)]
        )

    def cast(self, value):
        if self._params:
            return self._caster(value, **self._params)
        return self._caster(value)

    def to_python(self, value):
        exceptions = self.exception
        if not isinstance(exceptions, tuple):
            exceptions = (exceptions,)
        try:
            return self._adapter.validate_python(value)
        except exceptions:
            raise ValueError(self.message.format(value))
        except PydanticValidationError:
            raise ValueError(self.message.format(value))


class IntegerValue(Value):
    python_type = int


class PositiveIntegerValue(IntegerValue):

    def to_python(self, value):
        int_value = super().to_python(value)
        if int_value < 0:
            raise ValueError(self.message.format(value))
        return int_value


class FloatValue(Value):
    python_type = float


class DecimalValue(Value):
    python_type = decimal.Decimal


class SequenceValue(Value):
    """
    Common code for sequence-type values (lists and tuples).
    Do not use this class directly. Instead use a subclass.
    """

    # Specify this value in subclasses, e.g. with 'list' or 'tuple'
    sequence_type = None
    converter = None

    def __init__(self, *args, **kwargs):
        msg = 'Cannot interpret {0} item {{0!r}} in {0} {{1!r}}'
        self.message = msg.format(self.sequence_type.__name__)
        self.separator = kwargs.pop('separator', ',')
        converter = kwargs.pop('converter', None)
        if converter is not None:
            self.converter = converter
        self.separators = self._build_separators()
        self.python_type = self._build_python_type()
        super().__init__(*args, **kwargs)
        # make sure the default is the correct sequence type
        if self.default is None:
            self.default = self.sequence_type()
        else:
            self.default = self.sequence_type(self.default)
        # initial conversion
        if self.converter is not None:
            self.default = self._convert(self.default)

    @property
    def item_annotation(self):
        """The pydantic annotation a single item is deserialized with."""
        if self.converter is None:
            return str
        return typing.Annotated[typing.Any, PlainValidator(self.converter)]

    def _build_separators(self):
        return (self.separator,)

    def _build_python_type(self):
        return typing.List[self.item_annotation]

    def _validate(self, annotation, value):
        try:
            return validate(annotation, value)
        except ValueError as err:
            item = getattr(err, 'item', value)
            raise ValueError(self.message.format(item, item)) from err

    def _convert(self, sequence):
        return self.sequence_type(
            self._validate(typing.List[self.item_annotation], sequence)
        )

    def to_python(self, value):
        parsed = parse_env_string(self.python_type, value, self.separators)
        return self.sequence_type(self._validate(self.python_type, parsed))


class ListValue(SequenceValue):
    sequence_type = list


class TupleValue(SequenceValue):
    sequence_type = tuple


class SingleNestedSequenceValue(SequenceValue):
    """
    Common code for nested sequences (list of lists, or tuple of tuples).
    Do not use this class directly. Instead use a subclass.
    """

    def __init__(self, *args, **kwargs):
        self.seq_separator = kwargs.pop('seq_separator', ';')
        super().__init__(*args, **kwargs)

    def _build_separators(self):
        return (self.separator, self.seq_separator)

    def _build_python_type(self):
        return typing.List[typing.List[self.item_annotation]]

    def _convert(self, items):
        # This could receive either a bare or nested sequence
        if items and isinstance(items[0], self.sequence_type):
            return self.sequence_type(
                self.sequence_type(inner)
                for inner in self._validate(self.python_type, items)
            )
        return super()._convert(items)

    def to_python(self, value):
        parsed = parse_env_string(self.python_type, value, self.separators)
        return self.sequence_type(
            self.sequence_type(inner)
            for inner in self._validate(self.python_type, parsed)
        )


class SingleNestedListValue(SingleNestedSequenceValue):
    sequence_type = list


class SingleNestedTupleValue(SingleNestedSequenceValue):
    sequence_type = tuple


class BackendsValue(ListValue):

    def converter(self, value):
        try:
            import_string(value)
        except ImportError as err:
            raise ValueError(err).with_traceback(sys.exc_info()[2])
        return value


class SetValue(ListValue):
    message = 'Cannot interpret set item {0!r} in set {1!r}'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = set()
        else:
            self.default = set(self.default)

    def to_python(self, value):
        return set(super().to_python(value))


class DictValue(Value):
    python_type = dict
    message = 'Cannot interpret dict value {0!r}'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = {}
        else:
            self.default = dict(self.default)

    def to_python(self, value):
        try:
            parsed = parse_env_string(dict, value, self.separators)
        except ValueError as err:
            raise ValueError(self.message.format(value)) from err
        return validate(dict, parsed, self.message)


class ValidationMixin:

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.validator, str):
            try:
                self._validator = import_string(self.validator)
            except ImportError as err:
                msg = f"Could not import {self.validator!r}"
                raise ImproperlyConfigured(msg) from err
        elif callable(self.validator):
            self._validator = self.validator
        else:
            raise ValueError('Cannot use validator of '
                             '{} ({!r})'.format(self, self.validator))
        self._adapter = TypeAdapter(
            typing.Annotated[str, PlainValidator(self.run_validator)]
        )
        if self.default:
            self.to_python(self.default)

    def run_validator(self, value):
        self._validator(value)
        return value

    def to_python(self, value):
        try:
            return self._adapter.validate_python(value)
        except (ValidationError, PydanticValidationError):
            raise ValueError(self.message.format(value))


class EmailValue(ValidationMixin, Value):
    message = 'Cannot interpret email value {0!r}'
    validator = 'django.core.validators.validate_email'


class URLValue(ValidationMixin, Value):
    message = 'Cannot interpret URL value {0!r}'
    validator = validators.URLValidator()


class IPValue(ValidationMixin, Value):
    message = 'Cannot interpret IP value {0!r}'
    validator = 'django.core.validators.validate_ipv46_address'


class RegexValue(ValidationMixin, Value):
    message = "Regex doesn't match value {0!r}"

    def __init__(self, *args, **kwargs):
        regex = kwargs.pop('regex', None)
        self.validator = validators.RegexValidator(regex=regex)
        super().__init__(*args, **kwargs)


class PathValue(Value):
    def __init__(self, *args, **kwargs):
        self.check_exists = kwargs.pop('check_exists', True)
        super().__init__(*args, **kwargs)

    def setup(self, name):
        value = super().setup(name)
        value = os.path.expanduser(value)
        if self.check_exists and not os.path.exists(value):
            raise ValueError(f'Path {value!r} does not exist.')
        return os.path.abspath(value)


class SecretValue(Value):

    def __init__(self, *args, **kwargs):
        kwargs['environ'] = True
        kwargs['environ_required'] = True
        super().__init__(*args, **kwargs)
        if self.default is not None:
            raise ValueError('Secret values are only allowed to '
                             'be set as environment variables')

    def setup(self, name):
        value = super().setup(name)
        if not value:
            raise ValueError(f'Secret value {name!r} is not set')
        return value


class ModelMixin:
    """Validates the value the caster returned with a pydantic model."""

    #: The pydantic model the parsed value is validated with.
    model = None

    def validate_model(self, value):
        if self.model is None:
            return value
        return as_settings(validate(self.model, value, self.message))


class EmailURLValue(ModelMixin, CastingMixin, MultipleMixin, Value):
    caster = 'dj_email_url.parse'
    model = Email
    message = 'Cannot interpret email URL value {0!r}'
    late_binding = True

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('environ', True)
        kwargs.setdefault('environ_prefix', None)
        kwargs.setdefault('environ_name', 'EMAIL_URL')
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = {}
        else:
            self.default = self.to_python(self.default)

    def to_python(self, value):
        return self.validate_model(super().to_python(value))


class DictBackendMixin(ModelMixin, Value):
    default_alias = 'default'

    def __init__(self, *args, **kwargs):
        self.alias = kwargs.pop('alias', self.default_alias)
        kwargs.setdefault('environ', True)
        kwargs.setdefault('environ_prefix', None)
        kwargs.setdefault('environ_name', self.environ_name)
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = {}
        else:
            self.default = self.to_python(self.default)

    def to_python(self, value):
        value = super().to_python(value)
        return {self.alias: self.validate_model(value)}


class DatabaseURLValue(DictBackendMixin, CastingMixin, Value):
    caster = 'dj_database_url.parse'
    model = Database
    message = 'Cannot interpret database URL value {0!r}'
    environ_name = 'DATABASE_URL'
    late_binding = True


class CacheURLValue(DictBackendMixin, CastingMixin, Value):
    caster = 'django_cache_url.parse'
    model = Cache
    message = 'Cannot interpret cache URL value {0!r}'
    environ_name = 'CACHE_URL'
    late_binding = True


class SearchURLValue(DictBackendMixin, CastingMixin, Value):
    caster = 'dj_search_url.parse'
    model = Search
    message = 'Cannot interpret Search URL value {0!r}'
    environ_name = 'SEARCH_URL'
    late_binding = True
