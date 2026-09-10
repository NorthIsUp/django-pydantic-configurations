import decimal
import os
import typing
from contextlib import contextmanager
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from pydantic import TypeAdapter, ValidationError

from configurations.env import (DEFAULT_PREFIX, Env, EnvConfig,
                                environ_name, parse_env_string)
from configurations.types import (AliasedSettings, Backend, Cache, Caches,
                                  Database, Databases, Email, EmailAddress,
                                  ExistingPath, IPAddress, Path, Regex, Search,
                                  Searches, Secret, URL, UrlModel, as_settings)


@contextmanager
def env(**kwargs):
    with patch.dict(os.environ, clear=True, **kwargs):
        yield


def read(annotation, value, **kwargs):
    return TypeAdapter(Env[annotation]).validate_python(value, **kwargs)


class EnvScalarTests(TestCase):

    def test_string(self):
        self.assertEqual(read(str, 'spam'), 'spam')

    def test_boolean(self):
        for truthy in ('yes', 'true', '1', 'on', 'True'):
            self.assertIs(read(bool, truthy), True)
        for falsy in ('no', 'false', '0', 'off', ''):
            self.assertIs(read(bool, falsy), False)
        with self.assertRaises(ValidationError):
            read(bool, 'nonboolean')

    def test_numbers(self):
        self.assertEqual(read(int, '2'), 2)
        self.assertEqual(read(float, '2.5'), 2.5)
        self.assertEqual(read(decimal.Decimal, '2.5'), decimal.Decimal('2.5'))
        with self.assertRaises(ValidationError):
            read(int, 'noninteger')

    def test_optional(self):
        self.assertEqual(read(typing.Optional[int], '2'), 2)
        self.assertIsNone(read(typing.Optional[int], None))

    def test_non_string_is_passed_through(self):
        self.assertEqual(read(typing.List[int], [1, 2]), [1, 2])


class EnvSequenceTests(TestCase):

    def test_list(self):
        self.assertEqual(read(typing.List[str], '2,2'), ['2', '2'])
        self.assertEqual(read(typing.List[str], '2, 2 ,'), ['2', '2'])
        self.assertEqual(read(typing.List[str], ''), [])

    def test_typed_items(self):
        self.assertEqual(read(typing.List[int], '1, 2, 3'), [1, 2, 3])
        self.assertEqual(read(typing.Set[int], '1,2,2'), {1, 2})
        self.assertEqual(read(typing.Tuple[int, ...], '1,2'), (1, 2))
        self.assertEqual(read(typing.Tuple[int, str], '1,spam'), (1, 'spam'))

    def test_nested(self):
        self.assertEqual(
            read(typing.List[typing.List[int]], '2,3;4,5'), [[2, 3], [4, 5]]
        )
        self.assertEqual(
            read(typing.List[typing.List[str]], '2, 3 , ; 4 , 5 ; '),
            [['2', '3'], ['4', '5']],
        )

    def test_json(self):
        self.assertEqual(read(typing.List[int], '[1, 2]'), [1, 2])

    def test_invalid_item(self):
        with self.assertRaises(ValidationError):
            read(typing.List[int], '1,spam')

    def test_custom_separator(self):
        self.assertEqual(
            parse_env_string(typing.List[str], '/usr/bin:/usr/sbin', (':',)),
            ['/usr/bin', '/usr/sbin'],
        )


class EnvMappingTests(TestCase):

    def test_json(self):
        self.assertEqual(read(typing.Dict[str, int], '{"a": 1}'), {'a': 1})

    def test_python_literal(self):
        self.assertEqual(read(typing.Dict[int, int], '{2: 2}'), {2: 2})

    def test_empty(self):
        self.assertEqual(read(typing.Dict[str, int], ''), {})

    def test_invalid(self):
        with self.assertRaises(ValueError):
            read(typing.Dict[str, int], 'spam')


class EnvConfigTests(TestCase):

    def test_default_name(self):
        config = EnvConfig()
        self.assertEqual(config.environ_name('DEBUG'), f'{DEFAULT_PREFIX}_DEBUG')

    def test_explicit_name(self):
        self.assertEqual(EnvConfig(name='SPAM').environ_name('DEBUG'),
                         'DJANGO_SPAM')

    def test_without_prefix(self):
        self.assertEqual(EnvConfig(name='DATABASE_URL', prefix=None)
                         .environ_name('DATABASES'), 'DATABASE_URL')

    def test_prefix_with_trailing_underscore(self):
        self.assertEqual(EnvConfig(prefix='ACME_').environ_name('DEBUG'),
                         'ACME_DEBUG')

    def test_merge(self):
        merged = EnvConfig(prefix='ACME').merge(EnvConfig(name='SPAM'))
        self.assertEqual(merged.environ_name('DEBUG'), 'ACME_SPAM')

    def test_field_config(self):
        from pydantic import BaseModel

        class Model(BaseModel):
            DATABASES: Env[str] = Env('spam', name='DATABASE_URL', prefix=None)

        field = Model.model_fields['DATABASES']
        self.assertEqual(environ_name('DATABASES', field), 'DATABASE_URL')

    def test_separator_and_separators_are_exclusive(self):
        with self.assertRaises(TypeError):
            Env(separator=':', separators=(':',))

    def test_multiple_types_are_rejected(self):
        with self.assertRaises(TypeError):
            Env[str, int]


class TypeTests(TestCase):

    def test_secret(self):
        self.assertEqual(read(Secret, 'spam'), 'spam')
        with self.assertRaises(ValidationError):
            read(Secret, '')

    def test_backend(self):
        path = 'django.middleware.common.CommonMiddleware'
        self.assertEqual(read(Backend, path), path)
        with self.assertRaises(ValidationError):
            read(Backend, 'non.existing.Backend')

    def test_url(self):
        self.assertEqual(read(URL, 'http://spam.eggs'), 'http://spam.eggs')
        with self.assertRaises(ValidationError):
            read(URL, 'httb://spam.eggs')

    def test_email_address(self):
        self.assertEqual(read(EmailAddress, 'spam@sp.am'), 'spam@sp.am')
        with self.assertRaises(ValidationError):
            read(EmailAddress, 'spam')

    def test_ip_address(self):
        self.assertEqual(read(IPAddress, '127.0.0.1'), '127.0.0.1')
        self.assertEqual(read(IPAddress, '::1'), '::1')
        with self.assertRaises(ValidationError):
            read(IPAddress, 'spam.eggs')

    def test_regex(self):
        self.assertEqual(read(Regex(r'\d+--\d+'), '123--456'), '123--456')
        with self.assertRaises(ValidationError):
            read(Regex(r'\d+--\d+'), '123456')

    def test_path(self):
        self.assertEqual(read(Path, '~/spam'),
                         os.path.join(os.path.expanduser('~'), 'spam'))

    def test_existing_path(self):
        self.assertEqual(read(ExistingPath, '/'), '/')
        with self.assertRaises(ValidationError):
            read(ExistingPath, '/does/not/exist')


class SettingsModelTests(TestCase):

    def test_database_url(self):
        databases = read(Databases, 'sqlite://')
        self.assertEqual(databases.as_settings()['default'], {
            'CONN_HEALTH_CHECKS': False,
            'CONN_MAX_AGE': 0,
            'DISABLE_SERVER_SIDE_CURSORS': False,
            'ENGINE': 'django.db.backends.sqlite3',
            'HOST': '',
            'NAME': ':memory:',
            'PASSWORD': '',
            'PORT': '',
            'USER': '',
        })

    def test_single_database(self):
        database = read(Database, 'sqlite://')
        self.assertEqual(database.ENGINE, 'django.db.backends.sqlite3')
        self.assertEqual(database.NAME, ':memory:')

    def test_databases_mapping(self):
        databases = read(Databases, '{"default": "sqlite://"}')
        self.assertEqual(len(databases), 1)
        self.assertEqual(databases['default'].ENGINE,
                         'django.db.backends.sqlite3')

    def test_database_dict(self):
        database = Database.model_validate({
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'spam',
        })
        self.assertEqual(database.as_settings(), {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'spam',
        })

    def test_cache_url(self):
        caches = read(Caches, 'redis://user@host:6379/1')
        self.assertEqual(caches.as_settings()['default']['LOCATION'],
                         'redis://host:6379/1')
        self.assertIsInstance(caches['default'], Cache)

    def test_email_url(self):
        email = read(Email, 'console://')
        self.assertTrue(Email.settings_expand)
        self.assertEqual(
            email.as_settings()['EMAIL_BACKEND'],
            'django.core.mail.backends.console.EmailBackend',
        )

    def test_search_url(self):
        searches = read(Searches, 'elasticsearch://127.0.0.1:9200/index')
        self.assertEqual(searches.as_settings(), {'default': {
            'ENGINE': 'haystack.backends.elasticsearch_backend.ElasticsearchSearchEngine',  # noqa: E501
            'URL': 'http://127.0.0.1:9200',
            'INDEX_NAME': 'index',
        }})
        self.assertIsInstance(searches['default'], Search)

    def test_as_settings_of_plain_values(self):
        self.assertEqual(as_settings({'a': [read(Database, 'sqlite://')]}),
                         {'a': [{
                             'CONN_HEALTH_CHECKS': False,
                             'CONN_MAX_AGE': 0,
                             'DISABLE_SERVER_SIDE_CURSORS': False,
                             'ENGINE': 'django.db.backends.sqlite3',
                             'HOST': '',
                             'NAME': ':memory:',
                             'PASSWORD': '',
                             'PORT': '',
                             'USER': '',
                         }]})
        self.assertEqual(as_settings('spam'), 'spam')


class SharedBaseTests(TestCase):
    """The bases the built-in settings models are built from are reusable."""

    def test_url_model(self):
        class Queue(UrlModel):
            url_parser = 'dj_database_url'
            url_setting = 'QUEUES'
            url_extra = 'database'

            ENGINE: str

        self.assertEqual(read(Queue, 'sqlite://').ENGINE,
                         'django.db.backends.sqlite3')

    def test_url_model_names_the_missing_package(self):
        class Queue(UrlModel):
            url_parser = 'not_a_real_url_package'
            url_setting = 'QUEUES'
            url_extra = 'queue'

            ENGINE: str = ''

        with self.assertRaises(ImproperlyConfigured) as cm:
            read(Queue, 'amqp://')
        self.assertIn('not_a_real_url_package', str(cm.exception))

    def test_aliased_settings(self):
        class Queue(UrlModel):
            url_parser = 'dj_database_url'
            url_setting = 'QUEUES'
            url_extra = 'database'

            ENGINE: str

        class Queues(AliasedSettings[Queue]):
            """A mapping of alias to queue."""

        queues = read(Queues, 'sqlite://')
        self.assertEqual(list(queues), ['default'])
        self.assertIsInstance(queues['default'], Queue)
        self.assertEqual(len(queues), 1)

    def test_the_built_in_mappings_share_that_base(self):
        for model in (Databases, Caches, Searches):
            self.assertTrue(issubclass(model, AliasedSettings))
        for model in (Database, Cache, Email, Search):
            self.assertTrue(issubclass(model, UrlModel))
