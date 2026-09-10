Typed settings
==============

.. module:: configurations.env
   :synopsis: Environment backed settings, deserialized by pydantic.

A ``Configuration`` is a pydantic model. Annotate a setting and pydantic
validates it; annotate it with ``Env`` and pydantic reads it from the process
environment and deserializes it into the type you asked for.

.. code-block:: python

    from configurations import Configuration, Env
    from configurations.types import Databases, EmailAddress, Secret

    class Prod(Configuration):
        DEBUG: Env[bool] = False
        SITE_ID: Env[int] = 1
        SECRET_KEY: Env[Secret]
        ALLOWED_HOSTS: Env[list[str]] = ["localhost"]
        SUPPORT_EMAIL: Env[EmailAddress] = "support@example.com"
        DATABASES: Env[Databases] = Env(name="DATABASE_URL", prefix=None)

Running this with

.. code-block:: console

    $ export DJANGO_DEBUG=no
    $ export DJANGO_SECRET_KEY=s3cr3t
    $ export DJANGO_ALLOWED_HOSTS="example.com, www.example.com"
    $ export DATABASE_URL=postgres://user:password@localhost/mydb

gives Django a ``False`` boolean, a string, a list of two strings and the
nested dictionary it expects in ``DATABASES``.

A setting that has no default, like ``SECRET_KEY`` above, is required. Starting
the project without it fails right away with a message naming the setting and
the environment variable it is read from, instead of failing later somewhere
deep inside Django.

Settings without an annotation keep working exactly as before, they are plain
class attributes and are copied to the settings module untouched.

Environment variable names
--------------------------

.. class:: Env

   The type wrapper. Used as an annotation, ``Env[T]`` reads the environment
   variable belonging to the setting and deserializes it into ``T``. Called,
   ``Env(...)`` returns a pydantic field that configures the lookup.

   :param default: the value to use when the variable is not set, positional.
      Without one the setting is required.
   :param name: the name of the environment variable, by default the name of
      the setting.
   :param prefix: the prefix of the environment variable, ``DJANGO`` by
      default, ``None`` for no prefix.
   :param required: fail when the variable is not set, even when there is a
      default.
   :param separator: the string sequences are split on, ``,`` by default.
   :param separators: one separator per level of nesting, ``(",", ";")`` by
      default.
   :param expand: whether the setting is written as the several settings it
      dumps into, as ``Email`` is.

   Any other keyword argument is passed on to ``pydantic.Field``.

The name of the environment variable is the name of the setting, prefixed with
``DJANGO_``. ``DEBUG`` is read from ``DJANGO_DEBUG``.

Call ``Env`` to change that, and to pass any of the arguments
``pydantic.Field`` takes:

.. code-block:: python

    class Prod(Configuration):
        # read DATABASE_URL, without the prefix
        DATABASES: Env[Databases] = Env(name="DATABASE_URL", prefix=None)

        # read ACME_TOKEN
        TOKEN: Env[str] = Env(prefix="ACME")

        # split on ":" instead of ","
        PATHS: Env[list[str]] = Env(default=[], separator=":")

        # fail if the variable is not set, even though there is a default
        RELEASE: Env[str] = Env("dev", required=True)

Deserializing
-------------

Pydantic does the deserialization, so anything pydantic understands can be a
setting: ``int``, ``float``, ``Decimal``, ``bool``, ``datetime``, ``Path``,
``Literal``, enums, unions, your own models.

The string in the environment is prepared for pydantic first:

============================== ===================================== ==========================
Annotation                     Environment variable                  Setting
============================== ===================================== ==========================
``Env[bool]``                  ``yes``, ``true``, ``1``, ``on``      ``True``
``Env[int]``                   ``42``                                ``42``
``Env[list[str]]``             ``a, b, c``                           ``["a", "b", "c"]``
``Env[list[int]]``             ``1,2,3``                             ``[1, 2, 3]``
``Env[set[str]]``              ``a,b,a``                             ``{"a", "b"}``
``Env[tuple[int, str]]``       ``1,spam``                            ``(1, "spam")``
``Env[list[list[int]]]``       ``1,2;3,4``                           ``[[1, 2], [3, 4]]``
``Env[dict[str, int]]``        ``{"a": 1}``                          ``{"a": 1}``
``Env[Databases]``             ``postgres://localhost/mydb``         ``{"default": {...}}``
============================== ===================================== ==========================

Sequences are split on ``,``, and nested sequences on ``;`` and then ``,``.
Empty items are dropped, so ``a, b ,`` is a list of two items. A value that
starts with ``[`` or ``{`` is read as JSON, with a Python literal as the
fallback, which is handy for a mapping that has to be written on one line.

A setting is deserialized the same way wherever it comes from, so a
``separator`` applies to the value in the environment, to a default written
as a string, and to a value passed to the configuration directly.

Because ``Env[T]`` is a plain ``typing.Annotated`` alias, it works anywhere
pydantic does:

.. code-block:: pycon

    >>> from pydantic import TypeAdapter
    >>> from configurations import Env
    >>> TypeAdapter(Env[list[int]]).validate_python("1, 2, 3")
    [1, 2, 3]

Types
-----

.. module:: configurations.types
   :synopsis: Pydantic types for Django settings.

``configurations.types`` has the types that Django settings tend to need.

URL based settings
^^^^^^^^^^^^^^^^^^

These parse a URL into the structure Django expects. They need the matching
optional dependency, install them with
``pip install django-configurations[cache,database,email,search]``.

They are built from two bases you can use for your own settings:

.. class:: UrlModel

   A setting written as a URL. A subclass names the module whose ``parse``
   function reads the URL (``url_parser``), the setting it stands for
   (``url_setting``) and the packaging extra that provides the parser
   (``url_extra``).

.. class:: AliasedSettings

   A mapping of alias to backend, the shape Django uses for ``DATABASES``
   and friends. Subscript it with the model of one entry, as in
   ``class Queues(AliasedSettings[Queue])``. A bare URL is read as the
   ``default`` alias.

.. class:: Databases

   The ``DATABASES`` setting, a mapping of alias to :class:`Database`. A bare
   URL is read as the ``default`` database. Parsed by ``dj-database-url``.

.. class:: Database

   A single database, an entry of the ``DATABASES`` mapping.

.. class:: Caches

   The ``CACHES`` setting, a mapping of alias to :class:`Cache`. A bare URL is
   read as the ``default`` cache. Parsed by ``django-cache-url``.

.. class:: Cache

   A single cache, an entry of the ``CACHES`` mapping.

.. class:: Email

   The email settings. This one expands into the individual ``EMAIL_*``
   settings Django reads, so ``EMAIL: Env[Email]`` sets ``EMAIL_HOST``,
   ``EMAIL_PORT``, ``EMAIL_BACKEND`` and the rest of them. Parsed by
   ``dj-email-url``. Pass ``Env(expand=False)`` to keep it in one setting.

.. class:: Searches

   The ``HAYSTACK_CONNECTIONS`` setting, a mapping of alias to
   :class:`Search`. Parsed by ``dj-search-url``.

.. class:: Search

   A single search connection.

Validated strings
^^^^^^^^^^^^^^^^^

.. class:: Secret

   A string that may not be empty. Combined with the absence of a default it
   is the replacement for ``values.SecretValue``.

.. class:: Backend

   An importable dotted path, e.g. ``MIDDLEWARE: Env[list[Backend]]``.

.. class:: URL

   A URL, validated by Django's ``URLValidator``.

.. class:: EmailAddress

   An email address, validated by Django.

.. class:: IPAddress

   An IPv4 or IPv6 address, validated by Django.

.. class:: Path

   A filesystem path, with ``~`` expanded and made absolute.

.. class:: ExistingPath

   A filesystem path that has to exist.

.. function:: Regex(pattern)

   A string that has to match ``pattern``, e.g.
   ``VERSION: Env[Regex(r"\d+\.\d+")]``.

Using the model
---------------

The configuration is a pydantic model, so it can be used as one. This is
useful in tests, or to check the environment without starting Django:

.. code-block:: pycon

    >>> Prod.configured()          # the validated instance
    >>> Prod.settings()["DATABASES"]  # what the settings module gets
    >>> Prod(DEBUG=True)           # override a setting, skipping the environment

Coming from the values classes
------------------------------

The :doc:`values classes<values>` keep working, they are implemented on top of
pydantic now. The typed equivalents are:

===================================== ===============================================
Value class                           Annotation
===================================== ===============================================
``values.Value("spam")``              ``Env[str] = "spam"``
``values.BooleanValue(False)``        ``Env[bool] = False``
``values.IntegerValue(1)``            ``Env[int] = 1``
``values.PositiveIntegerValue(1)``    ``Env[NonNegativeInt] = 1``
``values.FloatValue(1.0)``            ``Env[float] = 1.0``
``values.DecimalValue(1)``            ``Env[Decimal] = 1``
``values.ListValue([])``              ``Env[list[str]] = []``
``values.TupleValue(())``             ``Env[tuple[str, ...]] = ()``
``values.SetValue(set())``            ``Env[set[str]] = set()``
``values.SingleNestedListValue([])``  ``Env[list[list[str]]] = []``
``values.DictValue({})``              ``Env[dict[str, Any]] = {}``
``values.EmailValue()``               ``Env[EmailAddress]``
``values.URLValue()``                 ``Env[URL]``
``values.IPValue()``                  ``Env[IPAddress]``
``values.RegexValue(regex=r"\d+")``   ``Env[Regex(r"\d+")]``
``values.PathValue()``                ``Env[ExistingPath]``
``values.SecretValue()``              ``Env[Secret]``
``values.BackendsValue([])``          ``Env[list[Backend]] = []``
``values.DatabaseURLValue()``         ``Env[Databases] = Env(name="DATABASE_URL", prefix=None)``
``values.CacheURLValue()``            ``Env[Caches] = Env(name="CACHE_URL", prefix=None)``
``values.EmailURLValue()``            ``Env[Email] = Env(name="EMAIL_URL", prefix=None)``
``values.SearchURLValue()``           ``Env[Searches] = Env(name="SEARCH_URL", prefix=None)``
===================================== ===============================================

``NonNegativeInt`` and ``Decimal`` come from ``pydantic`` and ``decimal``, the
rest from ``configurations.types``.
