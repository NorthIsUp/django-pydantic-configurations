import os
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from typing import List

from pydantic import BaseModel
from pydantic.errors import PydanticUserError

from configurations import Configuration
from configurations.base import PYDANTIC_IGNORES
from configurations.env import Env
from configurations.types import Databases, Email, Secret


class TypedSettingsTests(TestCase):
    """The typed settings of tests.settings.typed, loaded by the importer."""

    @patch.dict(os.environ, clear=True,
                DJANGO_CONFIGURATION='TypedConfiguration',
                DJANGO_SETTINGS_MODULE='tests.settings.typed',
                DJANGO_DEBUG='yes',
                DJANGO_SITE_ID='3',
                DJANGO_SECRET_KEY='s3cr3t',
                DJANGO_ALLOWED_HOSTS='example.com, www.example.com',
                DJANGO_INTERNAL_IPS='127.0.0.1,::1',
                DJANGO_ADMIN_EMAIL='spam@sp.am',
                DJANGO_OPTIONS='{"spam": 1}',
                DJANGO_PATHS='/usr/bin:/usr/sbin',
                DATABASE_URL='sqlite://',
                EMAIL_URL='smtp://user:password@smtp.example.com:587')
    def test_settings_module(self):
        from tests.settings import typed

        # scalars and sequences, deserialized from the environment
        self.assertIs(typed.DEBUG, True)
        self.assertEqual(typed.SITE_ID, 3)
        self.assertEqual(typed.SECRET_KEY, 's3cr3t')
        self.assertEqual(typed.ALLOWED_HOSTS, ['example.com', 'www.example.com'])
        self.assertEqual(typed.INTERNAL_IPS, ['127.0.0.1', '::1'])
        self.assertEqual(typed.ADMIN_EMAIL, 'spam@sp.am')
        self.assertEqual(typed.OPTIONS, {'spam': 1})
        self.assertEqual(typed.PATHS, ['/usr/bin', '/usr/sbin'])

        # defaults are validated as well
        self.assertEqual(typed.MIDDLEWARE,
                         ['django.middleware.common.CommonMiddleware'])

        # models are written as the plain structures Django expects
        self.assertIsInstance(typed.DATABASES, dict)
        self.assertEqual(typed.DATABASES['default']['ENGINE'],
                         'django.db.backends.sqlite3')
        self.assertEqual(typed.DATABASES['default']['NAME'], ':memory:')
        self.assertIsInstance(typed.CACHES, dict)
        self.assertEqual(typed.CACHES['default']['BACKEND'],
                         'django.core.cache.backends.locmem.LocMemCache')

        # the email URL expands into the individual EMAIL_* settings
        self.assertEqual(typed.EMAIL_HOST, 'smtp.example.com')
        self.assertEqual(typed.EMAIL_PORT, 587)
        self.assertEqual(typed.EMAIL_HOST_USER, 'user')
        self.assertEqual(typed.EMAIL_HOST_PASSWORD, 'password')
        self.assertEqual(typed.EMAIL_BACKEND,
                         'django.core.mail.backends.smtp.EmailBackend')
        self.assertIsInstance(typed.EMAIL, dict)

        # untyped settings are copied over untouched
        self.assertEqual(typed.ROOT_URLCONF, 'tests.urls')
        self.assertEqual(typed.CONFIGURATION,
                         'tests.settings.typed.TypedConfiguration')

    @patch.dict(os.environ, clear=True,
                DJANGO_CONFIGURATION='FutureConfiguration',
                DJANGO_SETTINGS_MODULE='tests.settings.typed_future',
                DJANGO_DEBUG='yes',
                DJANGO_SECRET_KEY='s3cr3t',
                DJANGO_ALLOWED_HOSTS='example.com')
    def test_postponed_annotations(self):
        from tests.settings import typed_future

        self.assertIs(typed_future.DEBUG, True)
        self.assertEqual(typed_future.SECRET_KEY, 's3cr3t')
        self.assertEqual(typed_future.ALLOWED_HOSTS, ['example.com'])
        self.assertEqual(typed_future.DATABASES['default']['NAME'], ':memory:')
        self.assertEqual(typed_future.NOT_A_SETTING, 3)
        self.assertNotIn('NOT_A_SETTING',
                         typed_future.FutureConfiguration.model_fields)

    @patch.dict(os.environ, clear=True,
                DJANGO_CONFIGURATION='TypedDotEnvConfiguration',
                DJANGO_SETTINGS_MODULE='tests.settings.typed_dot_env')
    def test_dotenv_fills_typed_settings(self):
        from tests.settings import typed_dot_env

        self.assertEqual(typed_dot_env.DOTENV_VALUE, 'is set')


class ConfigurationTests(TestCase):
    """The pydantic model behind a configuration."""

    def test_typed_settings_are_fields(self):
        class Sub(Configuration):
            DEBUG: Env[bool] = False
            UNTYPED = 'spam'

        self.assertEqual(list(Sub.model_fields), ['DEBUG'])
        self.assertEqual(Sub.UNTYPED, 'spam')

    def test_django_defaults_are_inherited(self):
        class Sub(Configuration):
            pass

        self.assertEqual(Sub.ALLOWED_HOSTS, [])
        self.assertIn('dictConfig', Sub.LOGGING_CONFIG)

    def test_setup_writes_settings_to_the_class(self):
        class Sub(Configuration):
            DEBUG: Env[bool] = False
            NAME: Env[str] = Env('spam', prefix='ACME')

        with patch.dict(os.environ, clear=True, DJANGO_DEBUG='yes',
                        ACME_NAME='eggs'):
            Sub.setup()

        self.assertIs(Sub.DEBUG, True)
        self.assertEqual(Sub.NAME, 'eggs')

    def test_settings_returns_plain_values(self):
        class Sub(Configuration):
            DATABASES: Env[Databases] = Env('sqlite://', name='DATABASE_URL',
                                            prefix=None)

        with patch.dict(os.environ, clear=True):
            settings = Sub.settings()

        self.assertEqual(settings['DATABASES']['default']['NAME'], ':memory:')

    def test_expanded_settings(self):
        class Sub(Configuration):
            EMAIL: Env[Email] = Env('console://', name='EMAIL_URL', prefix=None)

        with patch.dict(os.environ, clear=True):
            settings = Sub.settings()

        self.assertEqual(settings['EMAIL_BACKEND'],
                         'django.core.mail.backends.console.EmailBackend')
        self.assertIsInstance(settings['EMAIL'], dict)

    def test_expansion_can_be_turned_off(self):
        class Sub(Configuration):
            EMAIL: Env[Email] = Env('console://', name='EMAIL_URL',
                                    prefix=None, expand=False)

        with patch.dict(os.environ, clear=True):
            settings = Sub.settings()

        # the Django default is left alone
        self.assertEqual(settings['EMAIL_BACKEND'],
                         'django.core.mail.backends.smtp.EmailBackend')
        self.assertEqual(settings['EMAIL']['EMAIL_BACKEND'],
                         'django.core.mail.backends.console.EmailBackend')

    def test_typed_setting_without_default_is_required(self):
        class Sub(Configuration):
            # DEBUG has a Django default, which must not be picked up here
            DEBUG: Env[bool]

        with patch.dict(os.environ, clear=True):
            with self.assertRaises(ImproperlyConfigured) as cm:
                Sub.settings()

        self.assertIn('DEBUG: Field required', str(cm.exception))

        with patch.dict(os.environ, clear=True, DJANGO_DEBUG='yes'):
            self.assertIs(Sub.settings()['DEBUG'], True)

    def test_missing_required_setting(self):
        class Sub(Configuration):
            SECRET_KEY: Env[Secret]

        with patch.dict(os.environ, clear=True):
            with self.assertRaises(ImproperlyConfigured) as cm:
                Sub.setup()

        self.assertIn('SECRET_KEY', str(cm.exception))
        self.assertIn('DJANGO_SECRET_KEY', str(cm.exception))

    def test_invalid_setting(self):
        class Sub(Configuration):
            SITE_ID: Env[int] = 1

        with patch.dict(os.environ, clear=True, DJANGO_SITE_ID='spam'):
            with self.assertRaises(ImproperlyConfigured) as cm:
                Sub.setup()

        self.assertIn('SITE_ID', str(cm.exception))

    def test_environ_required(self):
        class Sub(Configuration):
            NAME: Env[str] = Env('spam', required=True)

        with patch.dict(os.environ, clear=True):
            with self.assertRaises(ImproperlyConfigured) as cm:
                Sub.setup()

        self.assertIn('DJANGO_NAME', str(cm.exception))

    def test_typed_setting_is_inherited(self):
        class Parent(Configuration):
            DEBUG: Env[bool] = False

        class Child(Parent):
            SITE_ID: Env[int] = 1

        self.assertEqual(sorted(Child.model_fields), ['DEBUG', 'SITE_ID'])
        with patch.dict(os.environ, clear=True, DJANGO_DEBUG='yes'):
            self.assertIs(Child.settings()['DEBUG'], True)

    def test_configuration_is_deserialized_the_same_way_from_every_source(self):
        """The field configuration applies wherever the value comes from."""
        class Sub(Configuration):
            PATHS: Env[List[str]] = Env(default='/usr/bin:/usr/sbin',
                                        separator=':')

        with patch.dict(os.environ, clear=True, DJANGO_PATHS='/spam:/eggs'):
            self.assertEqual(Sub.settings()['PATHS'], ['/spam', '/eggs'])
        with patch.dict(os.environ, clear=True):
            self.assertEqual(Sub.settings()['PATHS'], ['/usr/bin', '/usr/sbin'])
        self.assertEqual(Sub(PATHS='/spam:/eggs').PATHS, ['/spam', '/eggs'])

    def test_setup_reads_the_environment_every_time(self):
        class Sub(Configuration):
            NAME: Env[str] = 'default'

        with patch.dict(os.environ, clear=True, DJANGO_NAME='first'):
            Sub.setup()
            self.assertEqual(Sub.NAME, 'first')
        with patch.dict(os.environ, clear=True, DJANGO_NAME='second'):
            Sub.setup()
            self.assertEqual(Sub.NAME, 'second')

    def test_settings_that_hold_a_class_or_an_instance(self):
        class Handler:
            def __call__(self):
                return 'called'

        class Sub(Configuration):
            STORAGE_CLASS = dict
            HANDLER = Handler()

        settings = Sub.settings()
        self.assertEqual(settings['STORAGE_CLASS'], {})
        self.assertEqual(settings['HANDLER'], 'called')

    def test_untyped_settings_keep_working(self):
        class Sub(Configuration):
            SPAM = 'eggs'

            @property
            def PROPERTY(self):
                return 1

            def METHOD(self):
                return 2

        settings = Sub.settings()
        self.assertEqual(settings['SPAM'], 'eggs')
        self.assertEqual(settings['PROPERTY'], 1)
        self.assertEqual(settings['METHOD'], 2)


class PydanticNamespaceRuleTests(TestCase):
    """
    Pins what pydantic accepts in a model namespace without an annotation.

    A configuration marks everything else as a class variable. If pydantic
    changes its mind, these fail here rather than crashing a settings module
    that happens to hold one of these values.
    """

    def test_pydantic_rejects_a_plain_unannotated_value(self):
        with self.assertRaises(PydanticUserError):
            class Model(BaseModel):
                SETTING = 'spam'

    def test_pydantic_rejects_an_unannotated_class(self):
        with self.assertRaises(PydanticUserError):
            class Model(BaseModel):
                SETTING = dict

    def test_pydantic_accepts_what_we_leave_unannotated(self):
        class Model(BaseModel):
            LAMBDA = lambda self: 1  # noqa: E731
            STATIC = staticmethod(lambda: 2)
            KLASS = classmethod(lambda cls: 3)

            @property
            def PROP(self):
                return 4

        self.assertEqual(dict(Model.model_fields), {})
        for value in (Model.__dict__['LAMBDA'], Model.__dict__['STATIC'],
                      Model.__dict__['KLASS'], Model.__dict__['PROP']):
            self.assertIsInstance(value, PYDANTIC_IGNORES)
