from configurations import Configuration
from configurations.env import Env


class TypedDotEnvConfiguration(Configuration):
    """Typed settings are filled from the .env file as well."""

    DOTENV = 'test_project/.env'

    DOTENV_VALUE: Env[str] = 'is not set'
